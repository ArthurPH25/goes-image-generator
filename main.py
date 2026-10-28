import configparser
import gc
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import time
import warnings
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from multiprocessing import Manager

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import cartopy.feature as cfeature
import cartopy.io.shapereader as shpreader
from matplotlib.backends.backend_agg import FigureCanvasAgg
from PIL import Image
import imageio.v2 as imageio
import numpy as np
import s3fs

from products import core, registry, pipeline
from products import glm as GLM
from utils import (
    get_satellite_info,
    log_error,
    log_warning,
    log_info,
    log_success,
    acquire_file_lock,
    release_file_lock,
    LOG_COLLECTOR_STATE,
    floor_to_abi_step,
    read_map_geometry_config,
    read_style_config,
    BASE_DIR,
    TEMP_DIR,
    ABI_STEP_MINUTES,
    GLM_STEP_SECONDS,
)
from S3_downloader import download_batch, download_sources, build_hour_path, build_file_prefix

warnings.filterwarnings("ignore")

INSTANCE_LOCK_STALE_TIMEOUT_S = 24 * 60 * 60


def configure_log_collector(shared_list):
    LOG_COLLECTOR_STATE["collector"] = shared_list


def exit_fatal(message):
    print(f"☠️ FATAL ERROR: {message}")
    sys.exit(1)


def format_elapsed_time(seconds):
    if seconds < 1:
        return f"{seconds:.2f}s"
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f"{hours}h {minutes}m {secs}s"
    if minutes > 0:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def parse_datetime(date_str, time_str, require_seconds):
    full_str = f"{date_str} {time_str}"
    has_seconds = time_str.count(":") == 2
    if require_seconds and not has_seconds:
        raise ValueError(f"time '{time_str}' has no seconds: the GLM product requires the HH:MM:SS format")
    if not require_seconds and has_seconds:
        raise ValueError(f"time '{time_str}' has seconds: this product requires the HH:MM format, without seconds")
    time_format = "%H:%M:%S" if has_seconds else "%H:%M"
    return datetime.strptime(full_str, f"%Y-%m-%d {time_format}").replace(tzinfo=timezone.utc)


def get_unique_path(desired_path):
    if not os.path.exists(desired_path):
        return desired_path
    base, ext = os.path.splitext(desired_path)
    counter = 2
    while True:
        candidate = f"{base} ({counter}){ext}"
        if not os.path.exists(candidate):
            return candidate
        counter += 1


def _remove_files_in_temp(should_remove, label):
    removed = 0
    for name in os.listdir(TEMP_DIR):
        if not should_remove(name):
            continue
        try:
            os.remove(os.path.join(TEMP_DIR, name))
            removed += 1
        except OSError:
            pass
    if removed:
        log_info(f"Cleaning {removed} {label} file(s) in '{TEMP_DIR}'...")


def _remove_dir_if_empty(dir_path):
    try:
        os.rmdir(dir_path)
    except OSError:
        pass


def _get_positive_int(config, section, key):
    value = config.getint(section, key)
    if value < 1:
        raise ValueError(f"{key} in [{section}] must be an integer greater than or equal to 1, got: {value}")
    return value


def prefetch_map_features(resolution="10m"):
    for feature in (cfeature.STATES, cfeature.BORDERS, cfeature.COASTLINE):
        try:
            shpreader.natural_earth(resolution=resolution, category=feature.category, name=feature.name)
        except Exception as e:
            log_warning(
                f"Could not prepare map data '{feature.name}' ({resolution}): {e}. "
                "The first run needs an internet connection so Cartopy can download it."
            )


def compute_render_signature(config, colors_config, product_key, dpi):
    map_geo = read_map_geometry_config(config)
    style = read_style_config(config)

    glm_signature_extra = None

    if product_key == "GLM":
        glm_cfg = GLM.read_glm_config(config)
        palette_section = "PALETTE_GLM" if glm_cfg["render_mode"] == "POINTS" else "PALETTE_GLM_DENSITY"
        palette_data = {palette_section: sorted(colors_config.items(palette_section))}
        if glm_cfg["background"]:
            palette_data.update(registry.palette_snapshot(glm_cfg["background"], colors_config))
        glm_signature_extra = {
            "render_mode": glm_cfg["render_mode"],
            "flash_age": glm_cfg["flash_age"],
            "density_grid_resolution_km": glm_cfg["density_grid_resolution_km"],
            "background": glm_cfg["background"],
            "flash_marker_min": glm_cfg["flash_marker_min"],
            "flash_marker_max": glm_cfg["flash_marker_max"],
            "glm_history_lookback_steps": (
                config.getint("PROCESSING", "glm_history_lookback_steps") if glm_cfg["flash_age"] else None
            ),
        }
    else:
        palette_data = registry.palette_snapshot(product_key, colors_config)

    signature_payload = {
        "product": product_key,
        "glm": glm_signature_extra,
        "dpi": dpi,
        "map_geo": map_geo,
        "style": style,
        "palette_data": palette_data,
    }
    payload_json = json.dumps(signature_payload, sort_keys=True, default=str)
    return hashlib.sha1(payload_json.encode("utf-8")).hexdigest()[:12]


SUPPORTED_IMAGE_FORMATS = {"png": ".png", "webp": ".webp", "jpg": ".jpg", "jpeg": ".jpg", "tiff": ".tiff", "tif": ".tiff"}
SUPPORTED_IMAGE_FORMATS_ALPHA = {"png": True, "webp": True, "jpg": False, "jpeg": False, "tiff": True, "tif": True}

CONFIG_PATH = os.path.join(BASE_DIR, "config.ini")
COLORS_PATH = os.path.join(BASE_DIR, "colors.ini")

OUTPUT_DIR = os.path.join(BASE_DIR, "output")
INSTANCE_LOCK_NAME = "instance.lock"
INSTANCE_LOCK_PATH = os.path.join(TEMP_DIR, INSTANCE_LOCK_NAME)
FRAME_FILE_PREFIX = "frame_"

config = configparser.ConfigParser(interpolation=None)
colors_config = configparser.ConfigParser(interpolation=None)
try:
    _config_files_read = config.read([CONFIG_PATH], encoding="utf-8")
    _colors_files_read = colors_config.read([COLORS_PATH], encoding="utf-8")
except (configparser.Error, UnicodeDecodeError) as e:
    exit_fatal(
        f"Could not read 'colors.ini'/'config.ini': {e}\n"
        "Check that: each key appears only once per section, every section has a [NAME] header, "
        "comments are on their own lines (starting with #), and the files are saved as UTF-8."
    )
if not _config_files_read or not _colors_files_read:
    exit_fatal("File 'colors.ini' and/or 'config.ini' not found in the script directory.")

try:
    product_key = config.get("GENERAL", "product").strip().upper()
    if not registry.is_valid_product_key(product_key):
        exit_fatal(
            f"invalid product '{product_key}'. See the list of accepted values in the comments of "
            "[GENERAL] in config.ini."
        )
    product_spec = registry.get_product(product_key)
    is_glm = product_key == "GLM"

    gen_type = config.get("GENERAL", "generation_type").strip().upper()
    if gen_type not in ["I", "V", "S"]:
        exit_fatal(
            f"invalid generation_type '{gen_type}'. Valid values are "
            "'I' (single image), 'V' (video) or 'S' (image sequence)."
        )

    num_workers = _get_positive_int(config, "PROCESSING", "num_workers")
    if config.has_option("PROCESSING", "delete_temp_images"):
        raise ValueError(
            "the delete_temp_images key was removed from [PROCESSING]: video frames are now always deleted after "
            "the video is assembled. Delete that line from config.ini. To keep the frames, use generation_type = S."
        )
    open_video = config.getboolean("PROCESSING", "open_video_when_done")
    dpi = _get_positive_int(config, "PROCESSING", "dpi")

    image_format = config.get("PROCESSING", "image_format").strip().lower()
    if image_format not in SUPPORTED_IMAGE_FORMATS:
        raise ValueError(
            f"invalid image_format '{image_format}'. Accepted values: {sorted(SUPPORTED_IMAGE_FORMATS)}"
        )
    image_ext = SUPPORTED_IMAGE_FORMATS[image_format]
    if image_ext.lstrip(".") not in FigureCanvasAgg.get_supported_filetypes():
        raise ValueError(
            f"image_format '{image_format}' is not supported by the matplotlib/Pillow installed in this environment. "
            "Update with: pip install -U matplotlib Pillow"
        )

    if is_glm:
        _get_positive_int(config, "PROCESSING", "glm_history_lookback_steps")
        _get_positive_int(config, "PROCESSING", "glm_history_max_concurrent_downloads")

    video_scale = config.get("FFMPEG", "video_scale")
    crf_value = config.get("FFMPEG", "crf")
    ffmpeg_preset = config.get("FFMPEG", "preset")

    if gen_type == "V":
        VALID_FFMPEG_PRESETS = {
            "ultrafast", "superfast", "veryfast", "faster", "fast",
            "medium", "slow", "slower", "veryslow",
        }
        if ffmpeg_preset.strip().lower() not in VALID_FFMPEG_PRESETS:
            raise ValueError(
                f"invalid preset '{ffmpeg_preset}'. Values accepted by FFmpeg (libx264): "
                f"{sorted(VALID_FFMPEG_PRESETS)}"
            )
        try:
            crf_int = int(crf_value)
        except ValueError:
            raise ValueError(f"crf '{crf_value}' must be an integer") from None
        if not (0 <= crf_int <= 51):
            raise ValueError(f"crf '{crf_value}' is outside the valid FFmpeg range (0 to 51)")
        if not re.match(r"^scale=-?\d+:-?\d+$", video_scale.strip()):
            raise ValueError(
                f"video_scale '{video_scale}' does not match the expected format 'scale=WIDTH:HEIGHT' "
                "(use -2 for the dimension that should keep the aspect ratio, e.g. scale=1920:-2)"
            )

    map_geo = read_map_geometry_config(config)
    core.visible_boundary_angles(
        map_geo["target_coordinates"], core.GOES_EAST_SATELLITE_LON_DEG, core.GOES_EAST_SATELLITE_HEIGHT_M,
        core.GRS80_SEMI_MAJOR_AXIS_M, core.GRS80_SEMI_MINOR_AXIS_M,
    )
    style_cfg = read_style_config(config)

    glm_cfg = None
    if is_glm:
        glm_cfg = GLM.validate_glm_config(config, colors_config)
        if (gen_type in ("I", "S") and not glm_cfg["background"]
                and not SUPPORTED_IMAGE_FORMATS_ALPHA[image_format]):
            raise ValueError(
                f"image_format '{image_format}' has no alpha channel, but [GLM] background is empty "
                "(transparent background). Use png, webp or tiff, or set a background."
            )
    else:
        registry.validate_product_colors(product_key, colors_config)

    render_signature = compute_render_signature(config, colors_config, product_key, dpi)
    frame_prefix = f"{FRAME_FILE_PREFIX}{render_signature}_"
    output_dir = OUTPUT_DIR if gen_type in ("I", "S") else TEMP_DIR

    target_dates = []

    if gen_type == "I":
        date_str = config.get("IMAGE_TIME", "image_date")
        time_str = config.get("IMAGE_TIME", "image_time")
        target_dt = parse_datetime(date_str, time_str, require_seconds=is_glm)

        if is_glm:
            if target_dt.second % GLM_STEP_SECONDS != 0:
                raise ValueError("The GLM product requires seconds that are multiples of 20 (00, 20 or 40).")
        else:
            if target_dt.minute % ABI_STEP_MINUTES != 0:
                raise ValueError("This product requires minutes that are multiples of 10 (one image every 10 minutes).")

        target_dates.append(target_dt)
    else:
        if not config.has_section("TIME_RANGE") and config.has_section("VIDEO_TIME"):
            raise ValueError(
                "the [VIDEO_TIME] section of config.ini was renamed to [TIME_RANGE] "
                "(it applies to both video and image sequence). Just rename the section, "
                "the keys inside it are unchanged."
            )
        start_date_str = config.get("TIME_RANGE", "start_date")
        start_time_str = config.get("TIME_RANGE", "start_time")
        end_date_str = config.get("TIME_RANGE", "end_date")
        end_time_str = config.get("TIME_RANGE", "end_time")
        video_fps = _get_positive_int(config, "TIME_RANGE", "fps") if gen_type == "V" else None

        start_dt = parse_datetime(start_date_str, start_time_str, require_seconds=is_glm)
        end_dt = parse_datetime(end_date_str, end_time_str, require_seconds=is_glm)

        if is_glm:
            if start_dt.second % GLM_STEP_SECONDS != 0 or end_dt.second % GLM_STEP_SECONDS != 0:
                raise ValueError("The start and end of a GLM period require seconds that are multiples of 20 (00, 20 or 40).")
        else:
            if start_dt.minute % ABI_STEP_MINUTES != 0 or end_dt.minute % ABI_STEP_MINUTES != 0:
                raise ValueError("The start and end of the period require minutes that are multiples of 10.")

        if end_dt <= start_dt:
            raise ValueError(
                "Invalid period: end_date/end_time "
                f"({end_dt.strftime('%Y-%m-%d %H:%M:%S')}) must be later than "
                f"start_date/start_time ({start_dt.strftime('%Y-%m-%d %H:%M:%S')})."
            )

        current_dt = start_dt
        step = timedelta(seconds=GLM_STEP_SECONDS) if is_glm else timedelta(minutes=ABI_STEP_MINUTES)
        while current_dt <= end_dt:
            target_dates.append(current_dt)
            current_dt += step

except Exception as e:
    exit_fatal(
        f"Failed to validate 'config.ini'/'colors.ini': {e}\n"
        "Tip: if the value quoted in the message has a '#' in the middle, it is a comment on the same line "
        "as the value. Move the comment to its own line."
    )


def build_image_name(product_key, pretty_time, ext):
    return f"{product_key}_{pretty_time}{ext}"


def build_frame_name(product_key, pretty_time):
    return f"{frame_prefix}{build_image_name(product_key, pretty_time, '.png')}"


def build_sequence_image_name(product_key, target_dt, ext):
    stamp = target_dt.strftime("%Y%m%d_%H%M%S") if is_glm else target_dt.strftime("%Y%m%d_%H%M")
    return f"{product_key}_{stamp}{ext}"


def build_sequence_dir_path(first_dt, last_dt):
    stamp_fmt = "%Y%m%d_%H%M%S" if is_glm else "%Y%m%d_%H%M"
    folder_name = f"{product_key}_{first_dt.strftime(stamp_fmt)}_to_{last_dt.strftime(stamp_fmt)}"
    return get_unique_path(os.path.join(OUTPUT_DIR, folder_name))


def _is_valid_image(path):
    try:
        with Image.open(path) as img:
            img.verify()
        return True
    except Exception:
        return False


def _find_source_file(s3_fs, sat_bucket, dt, source):
    bucket_path = build_hour_path(sat_bucket, dt, source.product_code)
    prefix = build_file_prefix(dt, source.product_code)
    token = f"M6C{source.band:02d}" if source.band is not None else source.product_code
    try:
        files_list = s3_fs.ls(bucket_path)
    except Exception as e:
        log_error(f"Failed to list {bucket_path}: {e}")
        return None
    return next((f for f in files_list if token in f and prefix in f), None)


def process_file_worker(task):
    target_dt = datetime.fromtimestamp(task["target_ts"], tz=timezone.utc)
    current_gen_type = task["current_gen_type"]
    output_path = task["output_path"]
    sat_name = task["sat_name"]

    pretty_time = target_dt.strftime("%Y%j%H%M%S") if is_glm else target_dt.strftime("%Y%j%H%M")
    log_time = target_dt.strftime("%Y-%m-%d %H:%M:%S") if is_glm else target_dt.strftime("%Y-%m-%d %H:%M")
    if current_gen_type == "S":
        image_name = build_sequence_image_name(product_key, target_dt, image_ext)
    elif current_gen_type == "V":
        image_name = build_frame_name(product_key, pretty_time)
    else:
        image_name = build_image_name(product_key, pretty_time, image_ext)
    image_path = os.path.join(output_path, image_name)

    if current_gen_type == "I":
        image_path = get_unique_path(image_path)

    if current_gen_type == "V" and os.path.exists(image_path):
        if _is_valid_image(image_path):
            log_info(f"[{log_time} UTC] Frame already exists in the cache. Skipping.")
            return True
        log_warning(f"[{log_time} UTC] Cached frame was corrupted (previous run interrupted?). Regenerating.")
        os.remove(image_path)

    local_paths = None
    try:
        if is_glm:
            glm_source_id = "GLM"
            remote_glm_path = task["main_remote_paths"][glm_source_id]
            cache_path, glm_cache_lock_path = GLM.glm_nc_cache_paths(TEMP_DIR, remote_glm_path)
            if os.path.exists(cache_path):
                log_info(f"[{log_time} UTC] Main NetCDF already cached (reused from another frame).")
            else:
                got_glm_lock = acquire_file_lock(glm_cache_lock_path)
                try:
                    if not os.path.exists(cache_path):
                        if not got_glm_lock:
                            log_warning(
                                f"[{log_time} UTC] Timed out waiting for the GLM main NetCDF lock; "
                                "downloading anyway (protected by an atomic temporary file)."
                            )
                        tmp_download_path = f"{cache_path}.part_{os.getpid()}"
                        log_info(f"[{log_time} UTC] Downloading the main NetCDF.")
                        results = download_batch([{
                            "remote_path": remote_glm_path,
                            "local_path": tmp_download_path,
                            "required_variables": GLM.GLM_REQUIRED_VARS,
                            "label": f"{log_time} main",
                        }])
                        main_ok, main_msg = results[remote_glm_path]
                        if not main_ok:
                            raise Exception(f"Failed to download the main NetCDF: {main_msg}")
                        if os.path.exists(cache_path):
                            os.remove(tmp_download_path)
                        else:
                            os.replace(tmp_download_path, cache_path)
                finally:
                    if got_glm_lock:
                        release_file_lock(glm_cache_lock_path)

            log_info(f"[{log_time} UTC] Processing data and saving the image.")
            GLM.generate_image(
                cache_path, image_path, sat_name, log_time, config, colors_config,
                target_dt=target_dt, remote_background_files=task["background_remote_paths"],
                current_gen_type="I" if current_gen_type == "I" else "V",
            )
        else:
            local_paths = download_sources(task["main_remote_paths"], TEMP_DIR, "temp_", f"[{log_time} UTC] main")
            if local_paths is None:
                raise Exception("Failed to download one or more main files for this product.")

            log_info(f"[{log_time} UTC] Processing data and saving the image.")
            pipeline.generate_standalone_image(
                product_spec, local_paths, sat_name, config, colors_config, image_path, target_dt
            )

        if not os.path.exists(image_path):
            raise Exception("The product module did not generate the expected image (silent failure).")

        gc.collect()
        log_success(f"[{log_time} UTC] Frame completed.")
        return True
    except Exception as e:
        log_error(f"[WORKER] Failed to generate the image for timestamp {task['target_ts']}: {e}")
        return False
    finally:
        if not is_glm and local_paths:
            for path in local_paths.values():
                if os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass


def process_satellite():
    t_start_total = time.time()
    t_start_search, t_end_search = None, None
    t_start_images, t_end_images = None, None
    t_start_video, t_end_video = None, None

    def calc_time(start, end):
        if start is None or end is None:
            return "not measured (run interrupted before this step)"
        return format_elapsed_time(end - start)

    log_manager = Manager()
    shared_log_list = log_manager.list()
    configure_log_collector(shared_log_list)
    got_instance_lock = False

    try:
        os.makedirs(TEMP_DIR, exist_ok=True)

        got_instance_lock = acquire_file_lock(
            INSTANCE_LOCK_PATH, stale_timeout_s=INSTANCE_LOCK_STALE_TIMEOUT_S, max_wait_s=0
        )
        if not got_instance_lock:
            exit_fatal(
                "Another run of this script is already in progress in this folder "
                f"(active lock at '{INSTANCE_LOCK_PATH}'). Running two instances at the same time in the "
                f"same directory corrupts the shared download caches in '{TEMP_DIR}', "
                "which are wiped at the end of each run. Wait for the other run to finish or, if "
                "you are sure none is running, delete the lock file manually."
            )

        os.makedirs(OUTPUT_DIR, exist_ok=True)

        frames_dir = output_dir
        if gen_type == "S":
            frames_dir = build_sequence_dir_path(target_dates[0], target_dates[-1])

        if is_glm and glm_cfg["background"]:
            product_label = f"GLM (background {glm_cfg['background']}, {glm_cfg['render_mode']})"
        elif is_glm:
            product_label = f"GLM ({glm_cfg['render_mode']})"
        else:
            product_label = product_key

        print("=" * 70)
        print("--- SATELLITE IMAGE GENERATOR ---")
        mode_label = {"I": "Single Image", "V": "Video", "S": "Image Sequence"}[gen_type]
        print(f"Mode: {mode_label} | Active Product: {product_label}")
        print(f"Target: {config.get('MAP', 'target_coordinates')}")
        if gen_type == "V":
            print(f"Frames for this run: {TEMP_DIR} ({frame_prefix}*)")
        elif gen_type == "S":
            print(f"Image output folder: {frames_dir}")
        time_fmt = "%Y-%m-%d %H:%M:%S" if is_glm else "%Y-%m-%d %H:%M"
        if gen_type == "I":
            print(f"Timestamp: {target_dates[0].strftime(time_fmt)} UTC")
        else:
            print(f"Period: {target_dates[0].strftime(time_fmt)} to {target_dates[-1].strftime(time_fmt)} UTC")
        print("=" * 70)
        print()

        if gen_type == "V" and is_glm and not glm_cfg["background"]:
            log_info(
                "GLM without a background in video mode: the PNG frames are transparent, but the MP4 has no alpha channel "
                "and shows a black background. To keep the transparency, use generation_type = I or S."
            )
            print()

        S3_fs = s3fs.S3FileSystem(anon=True)
        tasks = []

        log_info("Searching for files in the NOAA S3 bucket...")
        print()
        t_start_search = time.time()

        background_spec = None
        background_search_cache = {}
        if is_glm and glm_cfg["background"]:
            background_spec = registry.get_product(glm_cfg["background"])

        for target in target_dates:
            sat_bucket, sat_name = get_satellite_info(target)

            sources = product_spec.sources

            main_remote_paths = {}
            all_found = True
            for source in sources:
                found = _find_source_file(S3_fs, sat_bucket, target, source)
                main_remote_paths[source.source_id] = found
                if not found:
                    all_found = False

            background_remote_paths = {}
            if background_spec is not None:
                background_dt = floor_to_abi_step(target)
                if background_dt not in background_search_cache:
                    bg_sat_bucket, _ = get_satellite_info(background_dt)
                    bg_found = {}
                    for source in background_spec.sources:
                        bg_found[source.source_id] = _find_source_file(S3_fs, bg_sat_bucket, background_dt, source)
                    background_search_cache[background_dt] = bg_found
                    if all(bg_found.values()):
                        log_success(
                            f"Found: {background_dt.strftime('%H:%M')} UTC "
                            f"({', '.join(bg_found)}, GLM background)"
                        )
                    else:
                        log_warning(
                            f"Background {glm_cfg['background']} for {background_dt.strftime('%H:%M')} UTC is incomplete in the "
                            "bucket; the GLM frames of this 10-minute block will have no background."
                        )
                background_remote_paths = background_search_cache[background_dt]

            if not all_found:
                missing = [s.source_id for s in sources if not main_remote_paths[s.source_id]]
                log_warning(f"File(s) {', '.join(missing)} for {target.strftime('%H:%M:%S')} not found in the bucket.")
                continue

            tasks.append({
                "target_ts": target.replace(tzinfo=timezone.utc).timestamp(),
                "main_remote_paths": main_remote_paths,
                "background_remote_paths": background_remote_paths,
                "sat_name": sat_name,
                "output_path": frames_dir,
                "current_gen_type": gen_type,
            })
            log_success(f"Found: {target.strftime('%H:%M:%S')} UTC ({', '.join(main_remote_paths)})")
        t_end_search = time.time()

        if not tasks:
            exit_fatal("No files found for the configured dates. Review 'config.ini'.")

        print()
        search_summary = f"Search finished: {len(tasks)} of {len(target_dates)} timestamp(s) found"
        if background_spec is not None:
            bg_ok = sum(1 for found in background_search_cache.values() if all(found.values()))
            search_summary += f" and {bg_ok} of {len(background_search_cache)} {glm_cfg['background']} background(s)"
        log_info(f"{search_summary}.")

        if gen_type == "S":
            os.makedirs(frames_dir, exist_ok=True)

        if not style_cfg["hide_map_lines"]:
            print()
            log_info("Checking the Cartopy map data...")
            prefetch_map_features()

        log_info(f"Starting {num_workers} worker(s)...")
        print()

        success_count = 0
        fail_count = 0
        total_tasks = len(tasks)

        t_start_images = time.time()
        with ProcessPoolExecutor(
            max_workers=num_workers,
            initializer=configure_log_collector,
            initargs=(shared_log_list,),
        ) as executor:
            futures = {executor.submit(process_file_worker, t): t for t in tasks}

            for future in as_completed(futures):
                is_success = future.result()
                if is_success:
                    success_count += 1
                else:
                    fail_count += 1

                tasks_done = success_count + fail_count
                if tasks_done % 5 == 0 or tasks_done == total_tasks:
                    print()
                    log_info(f"{tasks_done}/{total_tasks} images processed ({success_count} OK | {fail_count} failure(s)).")
                    print()
        t_end_images = time.time()

        if fail_count > 0:
            log_warning(f"{fail_count} image(s) failed during processing. Review the errors above.")
        else:
            log_success("Image step completed without failures.")

        if gen_type == "V":
            if success_count > 0:
                t_start_video = time.time()
                log_info("Starting video assembly.")

                try:
                    video_frames = []
                    skipped_corrupt = 0
                    for task in tasks:
                        target_dt = datetime.fromtimestamp(task["target_ts"], tz=timezone.utc)
                        pretty_time = target_dt.strftime("%Y%j%H%M%S") if is_glm else target_dt.strftime("%Y%j%H%M")
                        frame_path = os.path.join(output_dir, build_frame_name(product_key, pretty_time))
                        if not os.path.exists(frame_path):
                            continue
                        if _is_valid_image(frame_path):
                            video_frames.append(frame_path)
                        else:
                            skipped_corrupt += 1

                    if skipped_corrupt:
                        log_warning(f"{skipped_corrupt} frame(s) on disk were corrupted and were excluded from the video.")

                    if video_frames:
                        time_fmt_out = "%Y%m%d_%H%M%S" if is_glm else "%Y%m%d_%H%M"
                        first_dt = datetime.fromtimestamp(tasks[0]["target_ts"], tz=timezone.utc)
                        last_dt = datetime.fromtimestamp(tasks[-1]["target_ts"], tz=timezone.utc)
                        video_filename = os.path.join(
                            OUTPUT_DIR,
                            f"{product_key}_{first_dt.strftime(time_fmt_out)}_to_{last_dt.strftime(time_fmt_out)}.mp4",
                        )
                        video_filename = get_unique_path(video_filename)
                        ffmpeg_args = ['-crf', str(crf_value), '-preset', ffmpeg_preset, '-vf', video_scale, '-loglevel', 'error']

                        log_info(f"Encoding with FFmpeg (CRF {crf_value} and {video_scale})...")

                        first_frame_img = Image.open(video_frames[0])
                        target_dimensions = first_frame_img.size
                        first_frame_img.close()

                        with imageio.get_writer(video_filename, format='FFMPEG', fps=video_fps, macro_block_size=None, ffmpeg_params=ffmpeg_args) as video_writer:
                            total_frames = len(video_frames)
                            for idx, frame_path in enumerate(video_frames):
                                current_frame = idx + 1

                                if current_frame == 1 or current_frame % 10 == 0 or current_frame == total_frames:
                                    log_info(f"Adding frame {current_frame}/{total_frames} to the video.")

                                img_object = Image.open(frame_path)
                                if img_object.size != target_dimensions:
                                    img_object = img_object.resize(target_dimensions, Image.Resampling.LANCZOS)
                                if img_object.mode == "RGBA":
                                    black_bg = Image.new("RGBA", img_object.size, (0, 0, 0, 255))
                                    img_object = Image.alpha_composite(black_bg, img_object).convert("RGB")
                                video_writer.append_data(np.array(img_object))
                                img_object.close()

                        log_success(f"Video exported to: {video_filename}")

                        _remove_files_in_temp(lambda name: name.startswith(frame_prefix), "frame")

                        if open_video:
                            try:
                                if platform.system() == 'Windows':
                                    os.startfile(video_filename)
                                elif platform.system() == 'Darwin':
                                    subprocess.run(['open', video_filename], check=True,
                                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                                else:
                                    if not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
                                        raise RuntimeError('no graphical environment')
                                    subprocess.run(['xdg-open', video_filename], check=True,
                                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                            except Exception:
                                log_warning(f"Could not open the video player automatically (environment without graphical support?). File saved at: {video_filename}")
                    else:
                        log_error("No valid PNG frame was found on disk to assemble the video, despite reported successes. Check the frames folder.")
                except Exception as e:
                    log_error(f"Failed to assemble the final video: {e}")
                finally:
                    t_end_video = time.time()
            else:
                exit_fatal("The configured mode is video, but no image was generated. Cannot assemble the video.")
        elif gen_type == "S":
            if success_count > 0:
                log_success(f"Sequence ready: {success_count} image(s) saved to: {frames_dir}")
            else:
                _remove_dir_if_empty(frames_dir)
                exit_fatal("The configured mode is image sequence, but no image was generated.")
        elif success_count == 0:
            exit_fatal("The image was not generated. Review the errors above.")

    except Exception as e:
        exit_fatal(f"Run aborted by an unhandled error: {e}")
    finally:
        if got_instance_lock:
            _remove_files_in_temp(
                lambda name: name != INSTANCE_LOCK_NAME and not name.startswith(FRAME_FILE_PREFIX), "temporary"
            )

        t_end_total = time.time()
        print()
        print("=" * 70)
        print("EXECUTION TIME REPORT")
        print("=" * 70)

        if gen_type in ("V", "S"):
            print(f"Time searching for files on NOAA: {calc_time(t_start_search, t_end_search)}")
            print(f"Time downloading and generating images: {calc_time(t_start_images, t_end_images)}")
            if gen_type == "V":
                print(f"Time assembling the video: {calc_time(t_start_video, t_end_video)}")
            print("-" * 70)

        print(f"TOTAL EXECUTION TIME: {calc_time(t_start_total, t_end_total)}")
        print("=" * 70)

        try:
            collected_logs = list(shared_log_list)
        except Exception:
            collected_logs = []

        print()
        print("=" * 70)
        print("WARNINGS AND ERRORS REPORT")
        print("=" * 70)
        if not collected_logs:
            print("No warnings or errors recorded during the run.")
        else:
            warning_count = sum(1 for level, _ in collected_logs if level == "WARNING")
            error_count = sum(1 for level, _ in collected_logs if level == "ERROR")
            print(f"Total: {warning_count} warning(s), {error_count} error(s).")
            print("-" * 70)
            for level, message in collected_logs:
                icon = "⚠️" if level == "WARNING" else "❌"
                print(f"{icon} [{level}] {message}")
        print("=" * 70)

        try:
            log_manager.shutdown()
        except Exception:
            pass

        if got_instance_lock:
            release_file_lock(INSTANCE_LOCK_PATH)
            _remove_dir_if_empty(TEMP_DIR)
            _remove_dir_if_empty(OUTPUT_DIR)


if __name__ == "__main__":
    process_satellite()