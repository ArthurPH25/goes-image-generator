import asyncio
import os
from datetime import timedelta

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import cartopy.crs as ccrs
import matplotlib.patches as mpatches
from netCDF4 import Dataset
import s3fs

import goes_bands
from utils import (
    read_map_geometry_config,
    read_style_config,
    create_map_axes,
    add_geo_features,
    add_watermark,
    add_map_title,
    save_figure,
    get_satellite_info,
    get_projection_params,
    log_error,
    log_warning,
    log_info,
    background_cache_paths,
    save_background_data,
    load_background_data,
    acquire_background_lock,
    release_background_lock,
    glm_nc_cache_paths,
)
from S3_downloader import download_batch, download_batch_async, CONNECT_TIMEOUT_S, READ_TIMEOUT_S

BACKGROUND_CACHE_DIR = "satelite_temp_downloads"
GLM_NC_CACHE_DIR = os.path.join(BACKGROUND_CACHE_DIR, "glm_nc_cache")
ABI_REQUIRED_VARS = ["CMI"]

TITLE_FONTSIZE_PT = 16

def _get_color_for_age(age_seconds, sorted_colors):
    chosen_color = sorted_colors[0][1]
    for limit, color_hex in sorted_colors:
        if age_seconds >= limit:
            chosen_color = color_hex
    return chosen_color

def _get_size_for_age(age_seconds, max_age_seconds, marker_min, marker_max):
    if max_age_seconds <= 0:
        return marker_max
    fraction = min(max(age_seconds / max_age_seconds, 0.0), 1.0)
    return marker_max - (marker_max - marker_min) * fraction

def _parse_age_palette(config, show_flash_age):
    if not config.has_section("PALETTE_GLM"):
        raise ValueError("seção [PALETTE_GLM] ausente em colors.ini, obrigatória para o canal GLM")

    default_color = config.get("PALETTE_GLM", "default").strip()
    sorted_colors = []
    if show_flash_age:
        for key, value in config.items("PALETTE_GLM"):
            if key.isdigit():
                sorted_colors.append((int(key), value.strip().upper()))
        sorted_colors.sort(key=lambda item: item[0])
        if not sorted_colors:
            raise ValueError(
                "glm_flash_age = True exige ao menos uma chave numérica de idade em [PALETTE_GLM]"
            )
    return default_color, sorted_colors

async def _list_and_download_history(target_dt, sat_bucket, cache_dir, max_lookback_steps, max_concurrent_history):
    fs = s3fs.S3FileSystem(
        anon=True,
        asynchronous=True,
        config_kwargs={
            "connect_timeout": CONNECT_TIMEOUT_S,
            "read_timeout": READ_TIMEOUT_S,
            "retries": {"max_attempts": 0},
        },
    )
    session = await fs.set_session()
    listing_cache = {}

    async def list_hour(bucket_path):
        if bucket_path not in listing_cache:
            try:
                listing_cache[bucket_path] = await fs._ls(bucket_path)
            except Exception:
                listing_cache[bucket_path] = []
        return listing_cache[bucket_path]

    candidates = []
    lookup_tasks = []

    async def resolve_step(i):
        past_dt = target_dt - timedelta(seconds=20 * i)
        age_seconds = 20 * i
        year, day_of_year = past_dt.year, past_dt.timetuple().tm_yday
        hour, minute, second = past_dt.hour, past_dt.minute, past_dt.second
        bucket_path = f"{sat_bucket}/GLM-L2-LCFA/{year}/{day_of_year:03d}/{hour:02d}/"
        prefix = f"s{year:04d}{day_of_year:03d}{hour:02d}{minute:02d}{second:02d}"
        files_list = await list_hour(bucket_path)
        found_file = next((f for f in files_list if prefix in f), None)
        if found_file:
            return age_seconds, found_file
        return None

    lookup_tasks = [resolve_step(i) for i in range(1, max_lookback_steps + 1)]
    resolved = await asyncio.gather(*lookup_tasks)
    candidates = [r for r in resolved if r is not None]

    if not candidates:
        try:
            await session.close()
        except Exception:
            pass
        return []

    semaphore = asyncio.Semaphore(max_concurrent_history)
    results = []

    async def download_one(age_seconds, remote_path):
        cache_path, lock_path = glm_nc_cache_paths(cache_dir, remote_path)
        async with semaphore:
            if os.path.exists(cache_path):
                return age_seconds, cache_path
            got_lock = await asyncio.to_thread(acquire_background_lock, lock_path)
            try:
                if os.path.exists(cache_path):
                    return age_seconds, cache_path
                if not got_lock:
                    log_warning(
                        f"Timeout esperando lock do histórico GLM ({os.path.basename(remote_path)}); "
                        "tentando baixar mesmo assim."
                    )
                tmp_download_path = f"{cache_path}.part_{os.getpid()}"
                try:
                    await fs._get(remote_path, tmp_download_path)
                    os.replace(tmp_download_path, cache_path)
                    return age_seconds, cache_path
                except Exception:
                    if os.path.exists(tmp_download_path):
                        try:
                            os.remove(tmp_download_path)
                        except OSError:
                            pass
                    return None
            finally:
                if got_lock:
                    release_background_lock(lock_path)

    download_tasks = [download_one(age, path) for age, path in candidates]
    downloaded = await asyncio.gather(*download_tasks)
    results = [r for r in downloaded if r is not None]

    try:
        await session.close()
    except Exception as e:
        log_warning(f"Falha ao fechar sessão S3 do histórico GLM: {e}")

    return results

def _fetch_flash_history(target_dt, sat_bucket, cache_dir, max_lookback_steps, max_concurrent_history):
    os.makedirs(cache_dir, exist_ok=True)
    history_files = asyncio.run(
        _list_and_download_history(target_dt, sat_bucket, cache_dir, max_lookback_steps, max_concurrent_history)
    )
    parsed = []
    for age_seconds, local_path in history_files:
        try:
            with Dataset(local_path, "r") as nc_past:
                if "flash_lon" in nc_past.variables and "flash_lat" in nc_past.variables:
                    lons = list(nc_past.variables["flash_lon"][:])
                    lats = list(nc_past.variables["flash_lat"][:])
                    parsed.append((age_seconds, lons, lats))
        except Exception:
            continue
    return parsed

def _resolve_cached_background(target_dt, band_id, remote_abi_file, config, map_geo, current_gen_type):
    abi_minute = (target_dt.minute // 10) * 10
    background_dt = target_dt.replace(minute=abi_minute, second=0, microsecond=0)
    sat_bucket, _ = get_satellite_info(background_dt)

    if not remote_abi_file:
        return None, background_dt

    if current_gen_type == "I":
        local_abi_path = os.path.join(
            BACKGROUND_CACHE_DIR, f"temp_bg_{sat_bucket}_C{band_id:02d}_{background_dt.strftime('%Y%j%H%M')}.nc"
        )
        os.makedirs(BACKGROUND_CACHE_DIR, exist_ok=True)
        batch = [{
            "remote_path": remote_abi_file,
            "local_path": local_abi_path,
            "required_variables": ABI_REQUIRED_VARS,
            "label": f"fundo-ABI C{band_id:02d} {background_dt.strftime('%H:%M')}",
        }]
        results = download_batch(batch, max_concurrent=1)
        ok, msg = results[remote_abi_file]
        if not ok:
            log_error(f"Falha ao baixar o fundo ABI C{band_id:02d}: {msg}")
            return None, background_dt
        try:
            band_data = goes_bands.extract_band_data(local_abi_path, config, band_id, map_geo)
            band_data["cmap"] = goes_bands.rebuild_cmap_for_band(config, band_id, band_data["is_reflectance"])
        except Exception as e:
            log_error(f"Falha ao processar o fundo C{band_id:02d}: {e}")
            return None, background_dt
        finally:
            if os.path.exists(local_abi_path):
                os.remove(local_abi_path)
        return band_data, background_dt

    cache_data_path, lock_path = background_cache_paths(
        BACKGROUND_CACHE_DIR, sat_bucket, band_id, background_dt
    )

    def _load_from_cache():
        band_data = load_background_data(cache_data_path)
        band_data["cmap"] = goes_bands.rebuild_cmap_for_band(config, band_id, band_data["is_reflectance"])
        return band_data

    if os.path.exists(cache_data_path):
        try:
            return _load_from_cache(), background_dt
        except Exception as e:
            log_warning(f"Cache do fundo C{band_id:02d} corrompido, regenerando: {e}")

    got_lock = acquire_background_lock(lock_path)
    if not got_lock:
        log_warning(
            f"Timeout esperando o fundo C{band_id:02d} de {background_dt.strftime('%H:%M')} UTC "
            "ser renderizado por outro worker. Gerando sem fundo neste frame."
        )
        return None, background_dt

    try:
        if os.path.exists(cache_data_path):
            try:
                return _load_from_cache(), background_dt
            except Exception as e:
                log_warning(f"Cache do fundo C{band_id:02d} corrompido, regenerando: {e}")

        os.makedirs(BACKGROUND_CACHE_DIR, exist_ok=True)
        local_abi_path = os.path.join(
            BACKGROUND_CACHE_DIR, f"temp_bg_{sat_bucket}_C{band_id:02d}_{background_dt.strftime('%Y%j%H%M')}.nc"
        )

        log_info(
            f"Fundo C{band_id:02d} de {background_dt.strftime('%H:%M')} UTC ainda não está em cache. "
            "Baixando e processando uma única vez para todo o bloco de 10 minutos."
        )

        batch = [{
            "remote_path": remote_abi_file,
            "local_path": local_abi_path,
            "required_variables": ABI_REQUIRED_VARS,
            "label": f"fundo-ABI C{band_id:02d} {background_dt.strftime('%H:%M')}",
        }]
        results = download_batch(batch, max_concurrent=1)
        ok, msg = results[remote_abi_file]
        if not ok:
            log_error(f"Falha ao baixar o fundo ABI C{band_id:02d} para cache: {msg}")
            return None, background_dt

        try:
            band_data = goes_bands.extract_band_data(local_abi_path, config, band_id, map_geo)
            save_background_data(cache_data_path, band_data)
            band_data["cmap"] = goes_bands.rebuild_cmap_for_band(config, band_id, band_data["is_reflectance"])
        except Exception as e:
            log_error(f"Falha ao processar o fundo C{band_id:02d} para cache: {e}")
            return None, background_dt
        finally:
            if os.path.exists(local_abi_path):
                os.remove(local_abi_path)

        return band_data, background_dt
    finally:
        release_background_lock(lock_path)

def generate_image(local_path, png_path, sat_name, pretty_time, config, target_dt=None,
                    remote_abi_file=None, current_gen_type="I"):
    if not target_dt:
        log_error("Timestamp alvo (target_dt) não foi informado para o canal GLM.")
        return

    dpi = config.getint("PROCESSING", "dpi")
    map_geo = read_map_geometry_config(config)
    style = read_style_config(config)

    show_flash_age = config.getboolean("MAP", "glm_flash_age")
    background_band = config.get("MAP", "glm_background_band").strip()
    has_background = bool(background_band)

    max_lookback_steps = config.getint("PROCESSING", "glm_history_lookback_steps")
    max_concurrent_history = config.getint("PROCESSING", "glm_history_max_concurrent_downloads")
    max_history_age_seconds = max_lookback_steps * 20

    flash_marker_min = map_geo["glm_flash_marker_min"]
    flash_marker_max = map_geo["glm_flash_marker_max"]

    default_color, sorted_colors = _parse_age_palette(config, show_flash_age)

    all_lons, all_lats, all_colors, all_sizes = [], [], [], []
    sat_h, sat_lon = 35786023.0, -75.0
    sat_req, sat_rpol = 6378137.0, 6356752.31414

    current_flash_color = _get_color_for_age(0, sorted_colors) if sorted_colors else default_color
    current_flash_size = (
        _get_size_for_age(0, max_history_age_seconds, flash_marker_min, flash_marker_max)
        if show_flash_age else flash_marker_max
    )

    try:
        nc = Dataset(local_path)
        try:
            sat_h, sat_lon, parsed_req, parsed_rpol = get_projection_params(nc, "goes_lat_lon_projection")
            if parsed_req and parsed_rpol:
                sat_req, sat_rpol = parsed_req, parsed_rpol
        except (KeyError, AttributeError):
            pass

        if "flash_lon" in nc.variables and "flash_lat" in nc.variables:
            flash_count = len(nc.variables["flash_lon"][:])
            all_lons.extend(nc.variables["flash_lon"][:])
            all_lats.extend(nc.variables["flash_lat"][:])
            all_colors.extend([current_flash_color] * flash_count)
            all_sizes.extend([current_flash_size] * flash_count)
        nc.close()
    except Exception as e:
        log_error(f"Falha ao ler o arquivo principal do GLM: {e}")
        return

    if sorted_colors:
        sat_bucket, _ = get_satellite_info(target_dt)
        history = _fetch_flash_history(
            target_dt, sat_bucket, GLM_NC_CACHE_DIR, max_lookback_steps, max_concurrent_history
        )
        for age_seconds, lons, lats in history:
            all_lons.extend(lons)
            all_lats.extend(lats)
            all_colors.extend([_get_color_for_age(age_seconds, sorted_colors)] * len(lons))
            flash_size = _get_size_for_age(age_seconds, max_history_age_seconds, flash_marker_min, flash_marker_max)
            all_sizes.extend([flash_size] * len(lons))

    background_dt = None
    band_id = None

    if has_background:
        band_id = int(background_band)
        if not goes_bands.is_valid_band(band_id):
            log_error(f"Canal de fundo '{band_id}' inválido para o GLM. Os canais válidos vão de 1 a 16.")
            return

        band_data, background_dt = _resolve_cached_background(
            target_dt, band_id, remote_abi_file, config, map_geo, current_gen_type
        )

        if band_data is not None:
            axes_sat_h, axes_sat_lon = band_data["sat_h"], band_data["sat_lon"]
            axes_sat_req, axes_sat_rpol = band_data["sat_req"], band_data["sat_rpol"]
        else:
            axes_sat_h, axes_sat_lon = sat_h, sat_lon
            axes_sat_req, axes_sat_rpol = sat_req, sat_rpol

        figsize = (style["figure_width"], style["figure_height"])
        fig, ax, geo_proj, title_ax, _ = create_map_axes(
            map_geo["projection"], axes_sat_lon, axes_sat_h, style["clean_mode"],
            map_geo["target_coordinates"], figsize=figsize,
            semi_major_axis=axes_sat_req, semi_minor_axis=axes_sat_rpol,
            title_fontsize_pt=None if style["clean_mode"] else TITLE_FONTSIZE_PT,
        )

        if band_data is not None:
            goes_bands.draw_band_on_axes(ax, geo_proj, band_data, style)
        else:
            add_geo_features(ax, style)
            log_warning(f"Prosseguindo sem o fundo C{band_id:02d} neste frame ({pretty_time}).")

        if not style["clean_mode"]:
            add_watermark(ax, style["watermark"], color=style["watermark_color"], size=style["watermark_size"])
            glm_time_str = target_dt.strftime("%H:%M:%S")
            background_time_str = background_dt.strftime("%H:%M")
            base_date_str = target_dt.strftime("%d/%m/%Y")
            title = (f"{sat_name} | GLM — Densidade de Raios ({glm_time_str} UTC) | "
                     f"C{band_id:02d} ({background_time_str} UTC) | {base_date_str}")
            if title_ax is not None:
                add_map_title(title_ax, title, TITLE_FONTSIZE_PT)
    else:
        figsize = (style["figure_width"], style["figure_height"])
        fig, ax, _, title_ax, _ = create_map_axes(
            map_geo["projection"], sat_lon, sat_h, style["clean_mode"],
            map_geo["target_coordinates"], figsize=figsize,
            semi_major_axis=sat_req, semi_minor_axis=sat_rpol,
            title_fontsize_pt=None if style["clean_mode"] else TITLE_FONTSIZE_PT,
        )
        add_geo_features(ax, style)

        if not style["clean_mode"]:
            add_watermark(ax, style["watermark"], color=style["watermark_color"], size=style["watermark_size"])
            title = f"{sat_name} | GLM — Densidade de Raios | {target_dt.strftime('%d/%m/%Y %H:%M:%S')} UTC"
            if title_ax is not None:
                add_map_title(title_ax, title, TITLE_FONTSIZE_PT)

    if all_lons:
        ax.scatter(all_lons, all_lats, color=all_colors, s=all_sizes, alpha=0.9, transform=ccrs.PlateCarree(), zorder=5)

    if not style["clean_mode"] and sorted_colors:
        legend_patches = []
        for i, (limit, hex_color) in enumerate(sorted_colors):
            label = (f"< {sorted_colors[i + 1][0] / 60:.1f} min" if i + 1 < len(sorted_colors)
                      else f"≥ {limit / 60:.1f} min")
            legend_patches.append(mpatches.Patch(color=hex_color, label=label))

        legend = ax.legend(handles=legend_patches, loc="lower left", facecolor="black",
                            edgecolor="white", labelcolor="white", fontsize=12)
        legend.set_zorder(6)

    save_figure(fig, png_path, dpi, style["clean_mode"])