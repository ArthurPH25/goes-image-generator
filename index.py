import configparser
import gc
import hashlib
import json
import os
import platform
import re
import shutil
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
    acquire_file_lock,
    release_file_lock,
    LOG_COLLECTOR_STATE,
    floor_to_abi_step,
    read_map_geometry_config,
    read_style_config,
    BASE_DIR,
    TEMP_NC_DIR,
    ABI_STEP_MINUTES,
    GLM_STEP_SECONDS,
)
from S3_downloader import download_batch, download_sources, build_hour_path, build_file_prefix

warnings.filterwarnings("ignore")

INSTANCE_LOCK_STALE_TIMEOUT_S = 24 * 60 * 60


def configure_log_collector(shared_list):
    LOG_COLLECTOR_STATE["collector"] = shared_list


def exit_fatal(message):
    print(f"☠️ ERRO FATAL: {message}")
    sys.exit(1)


def log_success(message):
    print(f"✅ {message}")


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
        raise ValueError(f"horário '{time_str}' sem segundos: o produto GLM exige o formato HH:MM:SS")
    if not require_seconds and has_seconds:
        raise ValueError(f"horário '{time_str}' com segundos: este produto exige o formato HH:MM, sem segundos")
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


def _purge_temp_files(dir_path, skip_names=frozenset(), label=""):
    if not os.path.isdir(dir_path):
        return
    removed = 0
    for name in os.listdir(dir_path):
        full_path = os.path.join(dir_path, name)
        if name in skip_names or os.path.isdir(full_path):
            continue
        try:
            os.remove(full_path)
            removed += 1
        except OSError:
            pass
    if removed:
        log_info(f"Limpando {removed} arquivo(s) {label} em '{dir_path}'...")


def _get_positive_int(config, section, key):
    value = config.getint(section, key)
    if value < 1:
        raise ValueError(f"{key} em [{section}] deve ser um número inteiro maior ou igual a 1, recebido: {value}")
    return value


def prefetch_map_features(resolution="10m"):
    for feature in (cfeature.STATES, cfeature.BORDERS, cfeature.COASTLINE):
        try:
            shpreader.natural_earth(resolution=resolution, category=feature.category, name=feature.name)
        except Exception as e:
            log_warning(
                f"Não foi possível preparar o dado de mapa '{feature.name}' ({resolution}): {e}. "
                "Na primeira execução é preciso ter internet para o Cartopy baixá-lo."
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


CONFIG_PATH = os.path.join(BASE_DIR, "config.ini")
COLORS_PATH = os.path.join(BASE_DIR, "colors.ini")

IMAGES_DIR = os.path.join(BASE_DIR, "satelite_images")
VIDEOS_DIR = os.path.join(BASE_DIR, "satelite_videos")
TEMP_IMAGES_DIR = os.path.join(BASE_DIR, "satelite_temp_images")
INSTANCE_LOCK_PATH = os.path.join(TEMP_NC_DIR, "instance.lock")

config = configparser.ConfigParser(interpolation=None)
colors_config = configparser.ConfigParser(interpolation=None)
try:
    _config_files_read = config.read([CONFIG_PATH], encoding="utf-8")
    _colors_files_read = colors_config.read([COLORS_PATH], encoding="utf-8")
except (configparser.Error, UnicodeDecodeError) as e:
    exit_fatal(
        f"Não foi possível ler 'colors.ini'/'config.ini': {e}\n"
        "Confira: cada chave aparece só uma vez por seção, toda seção tem cabeçalho [NOME], "
        "comentários ficam em linhas próprias (começando com #) e os arquivos estão salvos em UTF-8."
    )
if not _config_files_read or not _colors_files_read:
    exit_fatal("Arquivo 'colors.ini' e/ou 'config.ini' não encontrado no diretório do script.")

try:
    product_key = config.get("GENERAL", "product").strip().upper()
    if not registry.is_valid_product_key(product_key):
        exit_fatal(
            f"product '{product_key}' inválido. Veja a lista de valores aceitos nos comentários de "
            "[GENERAL] em config.ini."
        )
    product_spec = registry.get_product(product_key)
    is_glm = product_key == "GLM"

    gen_type = config.get("GENERAL", "generation_type").strip().upper()
    if gen_type not in ["I", "V"]:
        exit_fatal(f"generation_type '{gen_type}' inválido. Os valores válidos são 'I' ou 'V'.")

    num_workers = _get_positive_int(config, "PROCESSING", "num_workers")
    delete_temp = config.getboolean("PROCESSING", "delete_temp_images")
    open_video = config.getboolean("PROCESSING", "open_video_when_done")
    dpi = _get_positive_int(config, "PROCESSING", "dpi")

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
                f"preset '{ffmpeg_preset}' inválido. Valores aceitos pelo FFmpeg (libx264): "
                f"{sorted(VALID_FFMPEG_PRESETS)}"
            )
        try:
            crf_int = int(crf_value)
        except ValueError:
            raise ValueError(f"crf '{crf_value}' deve ser um número inteiro") from None
        if not (0 <= crf_int <= 51):
            raise ValueError(f"crf '{crf_value}' fora do intervalo válido do FFmpeg (0 a 51)")
        if not re.match(r"^scale=-?\d+:-?\d+$", video_scale.strip()):
            raise ValueError(
                f"video_scale '{video_scale}' fora do formato esperado 'scale=LARGURA:ALTURA' "
                "(use -2 na dimensão que deve manter a proporção, ex: scale=1920:-2)"
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
    else:
        registry.validate_product_colors(product_key, colors_config)

    render_signature = compute_render_signature(config, colors_config, product_key, dpi)
    run_temp_dir = os.path.join(TEMP_IMAGES_DIR, render_signature)
    output_dir = IMAGES_DIR if gen_type == "I" else run_temp_dir

    target_dates = []

    if gen_type == "I":
        date_str = config.get("IMAGE_TIME", "image_date")
        time_str = config.get("IMAGE_TIME", "image_time")
        target_dt = parse_datetime(date_str, time_str, require_seconds=is_glm)

        if is_glm:
            if target_dt.second % GLM_STEP_SECONDS != 0:
                raise ValueError("O produto GLM exige segundos múltiplos de 20 (00, 20 ou 40).")
        else:
            if target_dt.minute % ABI_STEP_MINUTES != 0:
                raise ValueError("Este produto exige minutos múltiplos de 10 (imagem a cada 10 minutos).")

        target_dates.append(target_dt)
    else:
        start_date_str = config.get("VIDEO_TIME", "start_date")
        start_time_str = config.get("VIDEO_TIME", "start_time")
        end_date_str = config.get("VIDEO_TIME", "end_date")
        end_time_str = config.get("VIDEO_TIME", "end_time")
        video_fps = _get_positive_int(config, "VIDEO_TIME", "fps")

        start_dt = parse_datetime(start_date_str, start_time_str, require_seconds=is_glm)
        end_dt = parse_datetime(end_date_str, end_time_str, require_seconds=is_glm)

        if is_glm:
            if start_dt.second % GLM_STEP_SECONDS != 0 or end_dt.second % GLM_STEP_SECONDS != 0:
                raise ValueError("Início e fim do vídeo GLM exigem segundos múltiplos de 20 (00, 20 ou 40).")
        else:
            if start_dt.minute % ABI_STEP_MINUTES != 0 or end_dt.minute % ABI_STEP_MINUTES != 0:
                raise ValueError("Início e fim do vídeo exigem minutos múltiplos de 10.")

        if end_dt <= start_dt:
            raise ValueError(
                "O período do vídeo é inválido: end_date/end_time "
                f"({end_dt.strftime('%Y-%m-%d %H:%M:%S')}) deve ser posterior a "
                f"start_date/start_time ({start_dt.strftime('%Y-%m-%d %H:%M:%S')})."
            )

        current_dt = start_dt
        step = timedelta(seconds=GLM_STEP_SECONDS) if is_glm else timedelta(minutes=ABI_STEP_MINUTES)
        while current_dt <= end_dt:
            target_dates.append(current_dt)
            current_dt += step

except Exception as e:
    exit_fatal(
        f"Falha ao validar 'config.ini'/'colors.ini': {e}\n"
        "Dica: se o valor citado na mensagem tiver um '#' no meio, é um comentário na mesma linha "
        "do valor. Mova o comentário para uma linha própria."
    )


def build_png_name(product_key, pretty_time):
    return f"{product_key}_{pretty_time}.png"


def _is_valid_png(path):
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
        log_error(f"Falha ao listar {bucket_path}: {e}")
        return None
    return next((f for f in files_list if token in f and prefix in f), None)


def process_file_worker(task):
    target_dt = datetime.fromtimestamp(task["target_ts"], tz=timezone.utc)
    current_gen_type = task["current_gen_type"]
    output_path = task["output_path"]
    sat_name = task["sat_name"]

    pretty_time = target_dt.strftime("%Y%j%H%M%S") if is_glm else target_dt.strftime("%Y%j%H%M")
    log_time = pretty_time
    png_name = build_png_name(product_key, pretty_time)
    png_path = os.path.join(output_path, png_name)

    if current_gen_type == "I":
        png_path = get_unique_path(png_path)

    if current_gen_type == "V" and os.path.exists(png_path):
        if _is_valid_png(png_path):
            log_info(f"[{log_time} UTC] Frame já existe no cache. Pulando.")
            return True
        log_warning(f"[{log_time} UTC] Frame em cache estava corrompido (execução anterior interrompida?). Regenerando.")
        os.remove(png_path)

    local_paths = None
    try:
        if is_glm:
            glm_source_id = "GLM"
            remote_glm_path = task["main_remote_paths"][glm_source_id]
            cache_path, glm_cache_lock_path = GLM.glm_nc_cache_paths(GLM.GLM_NC_CACHE_DIR, remote_glm_path)
            os.makedirs(GLM.GLM_NC_CACHE_DIR, exist_ok=True)
            if os.path.exists(cache_path):
                log_info(f"[{log_time} UTC] NetCDF principal já em cache (reaproveitado de outro frame).")
            else:
                got_glm_lock = acquire_file_lock(glm_cache_lock_path)
                try:
                    if not os.path.exists(cache_path):
                        if not got_glm_lock:
                            log_warning(
                                f"[{log_time} UTC] Timeout esperando lock do NetCDF principal do GLM; "
                                "baixando mesmo assim (protegido por arquivo temporário atômico)."
                            )
                        tmp_download_path = f"{cache_path}.part_{os.getpid()}"
                        log_info(f"[{log_time} UTC] Baixando o NetCDF principal.")
                        results = download_batch([{
                            "remote_path": remote_glm_path,
                            "local_path": tmp_download_path,
                            "required_variables": GLM.GLM_REQUIRED_VARS,
                            "label": f"{log_time} principal",
                        }])
                        main_ok, main_msg = results[remote_glm_path]
                        if not main_ok:
                            raise Exception(f"Falha ao baixar o NetCDF principal: {main_msg}")
                        if os.path.exists(cache_path):
                            os.remove(tmp_download_path)
                        else:
                            os.replace(tmp_download_path, cache_path)
                finally:
                    if got_glm_lock:
                        release_file_lock(glm_cache_lock_path)

            log_info(f"[{log_time} UTC] Processando dados e salvando PNG.")
            GLM.generate_image(
                cache_path, png_path, sat_name, pretty_time, config, colors_config,
                target_dt=target_dt, remote_background_files=task["background_remote_paths"],
                current_gen_type=current_gen_type,
            )
        else:
            local_paths = download_sources(task["main_remote_paths"], TEMP_NC_DIR, "temp_", f"[{log_time} UTC] principal")
            if local_paths is None:
                raise Exception("Falha ao baixar um ou mais arquivos principais deste produto.")

            log_info(f"[{log_time} UTC] Processando dados e salvando PNG.")
            pipeline.generate_standalone_image(
                product_spec, local_paths, sat_name, config, colors_config, png_path, target_dt
            )

        if not os.path.exists(png_path):
            raise Exception("O módulo do produto não gerou o PNG esperado (falha silenciosa).")

        gc.collect()
        log_success(f"[{log_time} UTC] Frame concluído.")
        return True
    except Exception as e:
        log_error(f"[WORKER] Falha ao gerar a imagem do timestamp {task['target_ts']}: {e}")
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
            return "não medido (execução interrompida antes desta etapa)"
        return format_elapsed_time(end - start)

    log_manager = Manager()
    shared_log_list = log_manager.list()
    configure_log_collector(shared_log_list)
    got_instance_lock = False

    try:
        os.makedirs(output_dir, exist_ok=True)
        os.makedirs(TEMP_NC_DIR, exist_ok=True)
        if gen_type == "V":
            os.makedirs(VIDEOS_DIR, exist_ok=True)

        got_instance_lock = acquire_file_lock(
            INSTANCE_LOCK_PATH, stale_timeout_s=INSTANCE_LOCK_STALE_TIMEOUT_S, max_wait_s=0
        )
        if not got_instance_lock:
            exit_fatal(
                "Já existe outra execução deste script em andamento nesta pasta "
                f"(lock ativo em '{INSTANCE_LOCK_PATH}'). Rodar duas instâncias ao mesmo tempo no "
                "mesmo diretório corrompe os caches compartilhados de download (TEMP_NC_DIR/GLM_NC_CACHE_DIR), "
                "que são apagados por inteiro ao final de cada execução. Espere a outra terminar ou, se "
                "tiver certeza de que não há nenhuma rodando, apague o arquivo de lock manualmente."
            )

        if is_glm and glm_cfg["background"]:
            product_label = f"GLM (fundo {glm_cfg['background']}, {glm_cfg['render_mode']})"
        elif is_glm:
            product_label = f"GLM ({glm_cfg['render_mode']})"
        else:
            product_label = product_key

        print("=" * 70)
        print("🛰️ GERADOR DE IMAGENS DE SATÉLITE 🛰️")
        print(f"Modo: {'Vídeo' if gen_type == 'V' else 'Imagem Única'} | Produto Ativo: {product_label}")
        print(f"Alvo: {config.get('MAP', 'target_coordinates')}")
        if gen_type == "V":
            print(f"Pasta de frames desta execução: {run_temp_dir}")
        time_fmt = "%d/%m/%Y %H:%M:%S" if is_glm else "%d/%m/%Y %H:%M"
        if gen_type == "I":
            print(f"Instante: {target_dates[0].strftime(time_fmt)} UTC")
        else:
            print(f"Período: {target_dates[0].strftime(time_fmt)} a {target_dates[-1].strftime(time_fmt)} UTC")
        print("=" * 70)
        print()

        if gen_type == "V" and is_glm and not glm_cfg["background"]:
            log_info(
                "GLM sem fundo em vídeo: os frames PNG saem com fundo transparente, mas o MP4 não tem canal alpha "
                "e mostra o fundo preto. Para ficar com a transparência, use generation_type = I ou mantenha os "
                f"frames com delete_temp_images = False (pasta '{run_temp_dir}')."
            )
            print()

        S3_fs = s3fs.S3FileSystem(anon=True)
        tasks = []

        log_info("Buscando os arquivos no bucket S3 da NOAA...")
        print()
        t_start_search = time.time()

        background_spec = None
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
                bg_sat_bucket, _ = get_satellite_info(background_dt)
                for source in background_spec.sources:
                    found = _find_source_file(S3_fs, bg_sat_bucket, background_dt, source)
                    background_remote_paths[source.source_id] = found
                if not all(background_remote_paths.values()):
                    log_warning(
                        f"Fundo {glm_cfg['background']} das {background_dt.strftime('%H:%M')} UTC incompleto no "
                        "bucket; frame sairá sem fundo."
                    )

            if not all_found:
                missing = [s.source_id for s in sources if not main_remote_paths[s.source_id]]
                log_warning(f"Arquivo(s) {', '.join(missing)} de {target.strftime('%H:%M:%S')} não encontrado(s) no bucket.")
                continue

            tasks.append({
                "target_ts": target.replace(tzinfo=timezone.utc).timestamp(),
                "main_remote_paths": main_remote_paths,
                "background_remote_paths": background_remote_paths,
                "sat_name": sat_name,
                "output_path": output_dir,
                "current_gen_type": gen_type,
            })
            log_success(f"Encontrado: {target.strftime('%H:%M:%S')} UTC ({', '.join(main_remote_paths)})")
        t_end_search = time.time()

        if not tasks:
            exit_fatal("Nenhum arquivo encontrado para as datas configuradas. Revise o 'config.ini'.")

        if not style_cfg["hide_map_lines"]:
            print()
            log_info("Verificando os dados de mapa do Cartopy...")
            prefetch_map_features()

        log_info(f"Iniciando {num_workers} worker(s)...")
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
                    log_info(f"{tasks_done}/{total_tasks} imagens processadas ({success_count} OK | {fail_count} falha(s)).")
                    print()
        t_end_images = time.time()

        if fail_count > 0:
            log_warning(f"{fail_count} imagem(ns) falharam durante o processamento. Revise os erros acima.")
        else:
            log_success("Etapa de imagens concluída sem falhas.")

        if gen_type == "V":
            if success_count > 0:
                t_start_video = time.time()
                log_info("Iniciando a montagem do vídeo.")

                try:
                    video_frames = []
                    skipped_corrupt = 0
                    for task in tasks:
                        target_dt = datetime.fromtimestamp(task["target_ts"], tz=timezone.utc)
                        pretty_time = target_dt.strftime("%Y%j%H%M%S") if is_glm else target_dt.strftime("%Y%j%H%M")
                        frame_path = os.path.join(output_dir, build_png_name(product_key, pretty_time))
                        if not os.path.exists(frame_path):
                            continue
                        if _is_valid_png(frame_path):
                            video_frames.append(frame_path)
                        else:
                            skipped_corrupt += 1

                    if skipped_corrupt:
                        log_warning(f"{skipped_corrupt} frame(s) em disco estavam corrompidos e foram excluídos do vídeo.")

                    if video_frames:
                        time_fmt_out = "%Y%m%d_%H%M%S" if is_glm else "%Y%m%d_%H%M"
                        first_dt = datetime.fromtimestamp(tasks[0]["target_ts"], tz=timezone.utc)
                        last_dt = datetime.fromtimestamp(tasks[-1]["target_ts"], tz=timezone.utc)
                        video_filename = os.path.join(
                            VIDEOS_DIR,
                            f"satelite_{product_key}_{first_dt.strftime(time_fmt_out)}_a_{last_dt.strftime(time_fmt_out)}.mp4",
                        )
                        video_filename = get_unique_path(video_filename)
                        ffmpeg_args = ['-crf', str(crf_value), '-preset', ffmpeg_preset, '-vf', video_scale, '-loglevel', 'error']

                        log_info(f"Codificando com FFmpeg (CRF {crf_value} e {video_scale})...")

                        first_frame_img = Image.open(video_frames[0])
                        target_dimensions = first_frame_img.size
                        first_frame_img.close()

                        with imageio.get_writer(video_filename, format='FFMPEG', fps=video_fps, macro_block_size=None, ffmpeg_params=ffmpeg_args) as video_writer:
                            total_frames = len(video_frames)
                            for idx, frame_path in enumerate(video_frames):
                                current_frame = idx + 1

                                if current_frame == 1 or current_frame % 10 == 0 or current_frame == total_frames:
                                    log_info(f"Adicionando frame {current_frame}/{total_frames} ao vídeo.")

                                img_object = Image.open(frame_path)
                                if img_object.size != target_dimensions:
                                    img_object = img_object.resize(target_dimensions, Image.Resampling.LANCZOS)
                                if img_object.mode == "RGBA":
                                    black_bg = Image.new("RGBA", img_object.size, (0, 0, 0, 255))
                                    img_object = Image.alpha_composite(black_bg, img_object).convert("RGB")
                                video_writer.append_data(np.array(img_object))
                                img_object.close()

                        log_success(f"Vídeo exportado em: {video_filename}")

                        if delete_temp:
                            log_info(f"Removendo a pasta temporária desta execução '{run_temp_dir}'.")
                            shutil.rmtree(run_temp_dir, ignore_errors=True)
                        else:
                            log_info(f"Os frames usados foram mantidos em '{run_temp_dir}'.")

                        if open_video:
                            try:
                                if platform.system() == 'Windows':
                                    os.startfile(video_filename)
                                elif platform.system() == 'Darwin':
                                    subprocess.run(['open', video_filename], check=True,
                                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                                else:
                                    if not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
                                        raise RuntimeError('sem ambiente gráfico')
                                    subprocess.run(['xdg-open', video_filename], check=True,
                                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                            except Exception:
                                log_warning(f"Não foi possível abrir o player de vídeo automaticamente (ambiente sem suporte gráfico?). Arquivo salvo em: {video_filename}")
                    else:
                        log_error("Nenhum frame PNG válido foi encontrado em disco para montar o vídeo, apesar de sucessos reportados. Verifique a pasta de frames.")
                except Exception as e:
                    log_error(f"Falha ao montar o vídeo final: {e}")
                finally:
                    t_end_video = time.time()
            else:
                exit_fatal("O modo configurado é de vídeo, mas nenhuma imagem foi gerada. Não é possível montar o vídeo.")
        elif success_count == 0:
            exit_fatal("A imagem não foi gerada. Revise os erros acima.")

    except Exception as e:
        exit_fatal(f"Execução interrompida por um erro não tratado: {e}")
    finally:
        if got_instance_lock:
            _purge_temp_files(TEMP_NC_DIR, skip_names={"instance.lock"}, label="temporários desta execução")
            if is_glm:
                _purge_temp_files(GLM.GLM_NC_CACHE_DIR, label="do cache compartilhado do GLM")

        t_end_total = time.time()
        print()
        print("=" * 70)
        print("RELATÓRIO DE TEMPO DE EXECUÇÃO")
        print("=" * 70)

        if gen_type == "V":
            print(f"Tempo buscando arquivos na NOAA: {calc_time(t_start_search, t_end_search)}")
            print(f"Tempo baixando e gerando as imagens: {calc_time(t_start_images, t_end_images)}")
            print(f"Tempo montando o vídeo: {calc_time(t_start_video, t_end_video)}")
            print("-" * 70)

        print(f"TEMPO TOTAL DE EXECUÇÃO: {calc_time(t_start_total, t_end_total)}")
        print("=" * 70)

        try:
            collected_logs = list(shared_log_list)
        except Exception:
            collected_logs = []

        print()
        print("=" * 70)
        print("RELATÓRIO DE AVISOS E ERROS")
        print("=" * 70)
        if not collected_logs:
            print("Nenhum aviso ou erro registrado durante a execução.")
        else:
            warning_count = sum(1 for level, _ in collected_logs if level == "AVISO")
            error_count = sum(1 for level, _ in collected_logs if level == "ERRO")
            print(f"Total: {warning_count} aviso(s), {error_count} erro(s).")
            print("-" * 70)
            for level, message in collected_logs:
                icon = "⚠️" if level == "AVISO" else "❌"
                print(f"{icon} [{level}] {message}")
        print("=" * 70)

        try:
            log_manager.shutdown()
        except Exception:
            pass

        if got_instance_lock:
            release_file_lock(INSTANCE_LOCK_PATH)


if __name__ == "__main__":
    process_satellite()