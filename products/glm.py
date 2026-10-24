import asyncio
import hashlib
import os
from datetime import timedelta

import cartopy.crs as ccrs
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
from netCDF4 import Dataset
import numpy as np

from products import core, registry, pipeline
from products.core import LayerResult
from utils import (
    read_map_geometry_config,
    read_style_config,
    validate_color,
    get_satellite_info,
    log_error,
    log_warning,
    log_info,
    acquire_file_lock,
    release_file_lock,
    TEMP_NC_DIR,
    GLM_PRODUCT,
    GLM_STEP_SECONDS,
    floor_to_abi_step,
)
from S3_downloader import download_sources, create_async_s3_fs, build_hour_path, build_file_prefix

GLM_NC_CACHE_DIR = os.path.join(TEMP_NC_DIR, "glm_nc_cache")
GLM_REQUIRED_VARS = ["flash_lon", "flash_lat"]

RENDER_MODES = ("POINTS", "DENSITY")
KM_PER_DEGREE_LAT = 111.0
BACKGROUND_COLORBAR_KINDS = ("thermal", "split_window", "cloud")


def read_glm_config(config):
    section = "GLM"
    render_mode = config.get(section, "render_mode").strip().upper()
    if render_mode not in RENDER_MODES:
        raise ValueError(f"[{section}] render_mode deve ser POINTS ou DENSITY, recebido '{render_mode}'")

    flash_age = config.getboolean(section, "flash_age")

    density_grid_resolution_km = config.getfloat(section, "density_grid_resolution_km")
    if density_grid_resolution_km <= 0:
        raise ValueError(f"[{section}] density_grid_resolution_km deve ser positivo")

    background_raw = config.get(section, "background").strip().upper()
    background = background_raw or None
    if background is not None:
        if not registry.is_valid_product_key(background):
            raise ValueError(f"[{section}] background '{background_raw}' não é um produto válido")
        if background == "GLM":
            raise ValueError(f"[{section}] background não pode ser 'GLM' (GLM não pode ser fundo de si mesmo)")

    flash_marker_min = config.getfloat(section, "flash_marker_min")
    flash_marker_max = config.getfloat(section, "flash_marker_max")
    if flash_marker_min <= 0 or flash_marker_max <= 0:
        raise ValueError(f"[{section}] flash_marker_min e flash_marker_max devem ser positivos")
    if flash_marker_min > flash_marker_max:
        raise ValueError(f"[{section}] flash_marker_min não pode ser maior que flash_marker_max")

    return {
        "render_mode": render_mode,
        "flash_age": flash_age,
        "density_grid_resolution_km": density_grid_resolution_km,
        "background": background,
        "flash_marker_min": flash_marker_min,
        "flash_marker_max": flash_marker_max,
    }


def validate_glm_config(config, colors_config):
    glm_cfg = read_glm_config(config)

    if glm_cfg["render_mode"] == "POINTS":
        section = "PALETTE_GLM"
        if not colors_config.has_section(section):
            raise ValueError(f"seção '[{section}]' ausente em colors.ini, obrigatória com [GLM] render_mode = POINTS")
        if not colors_config.has_option(section, "default"):
            raise ValueError(f"chave 'default' ausente na seção '[{section}]'")
        validate_color(f"[{section}] chave default", colors_config.get(section, "default"))

        numeric_items = []
        for key, value in colors_config.items(section):
            if key == "default":
                continue
            if not key.isdigit():
                raise ValueError(
                    f"chave '{key}' na seção '[{section}]' inválida: use 'default' ou uma idade "
                    "inteira em segundos (ex: 150)"
                )
            numeric_items.append((key, value))

        if glm_cfg["flash_age"] and not numeric_items:
            raise ValueError(
                f"[GLM] flash_age = True exige ao menos uma chave numérica de idade em [{section}]"
            )
        for key, value in numeric_items:
            validate_color(f"[{section}] chave {key}", value)
    else:
        registry.validate_palette_section(colors_config, "PALETTE_GLM_DENSITY")

    if glm_cfg["background"]:
        registry.validate_product_colors(glm_cfg["background"], colors_config)

    return glm_cfg


def _parse_age_palette(colors_config, show_flash_age):
    default_color = colors_config.get("PALETTE_GLM", "default").strip()
    sorted_colors = []
    if show_flash_age:
        for key, value in colors_config.items("PALETTE_GLM"):
            if key.isdigit():
                sorted_colors.append((int(key), value.strip()))
        sorted_colors.sort(key=lambda item: item[0])
    return default_color, sorted_colors


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


def compute_density_grid(all_lons, all_lats, target_coordinates, resolution_km):
    lon_w, lon_e, lat_s, lat_n = target_coordinates
    mean_lat = (lat_s + lat_n) / 2.0
    dlat = resolution_km / KM_PER_DEGREE_LAT
    dlon = resolution_km / (KM_PER_DEGREE_LAT * max(np.cos(np.radians(mean_lat)), 0.15))

    lon_edges = np.arange(lon_w, lon_e + dlon, dlon)
    lat_edges = np.arange(lat_s, lat_n + dlat, dlat)

    if len(all_lons) == 0:
        counts = np.zeros((len(lon_edges) - 1, len(lat_edges) - 1))
    else:
        counts, _, _ = np.histogram2d(all_lons, all_lats, bins=[lon_edges, lat_edges])

    return lon_edges, lat_edges, counts.T


def glm_nc_cache_paths(cache_dir, remote_path):
    base_name = os.path.basename(remote_path).replace(".nc", "")
    cache_data = os.path.join(cache_dir, f"glm_{base_name}.nc")
    lock_path = os.path.join(cache_dir, f"glm_{base_name}.lock")
    return cache_data, lock_path


async def _list_and_download_history(target_dt, sat_bucket, cache_dir, max_lookback_steps, max_concurrent_history):
    fs = create_async_s3_fs()
    session = await fs.set_session()
    try:
        steps = []
        for i in range(1, max_lookback_steps + 1):
            age_seconds = GLM_STEP_SECONDS * i
            past_dt = target_dt - timedelta(seconds=age_seconds)
            bucket_path = build_hour_path(sat_bucket, past_dt, GLM_PRODUCT)
            prefix = build_file_prefix(past_dt, GLM_PRODUCT)
            steps.append((age_seconds, bucket_path, prefix))

        async def list_hour(bucket_path):
            try:
                return bucket_path, await fs._ls(bucket_path)
            except FileNotFoundError:
                return bucket_path, []
            except Exception as e:
                log_warning(f"Falha ao listar {bucket_path} no histórico do GLM: {e}")
                return bucket_path, []

        listings = dict(await asyncio.gather(*(list_hour(path) for path in sorted({step[1] for step in steps}))))

        candidates = []
        for age_seconds, bucket_path, prefix in steps:
            found_file = next((f for f in listings[bucket_path] if prefix in f), None)
            if found_file:
                candidates.append((age_seconds, found_file))

        if not candidates:
            return []

        semaphore = asyncio.Semaphore(max_concurrent_history)

        async def download_one(age_seconds, remote_path):
            cache_path, lock_path = glm_nc_cache_paths(cache_dir, remote_path)
            async with semaphore:
                if os.path.exists(cache_path):
                    return age_seconds, cache_path
                got_lock = await asyncio.to_thread(acquire_file_lock, lock_path)
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
                    except Exception as e:
                        if os.path.exists(tmp_download_path):
                            try:
                                os.remove(tmp_download_path)
                            except OSError:
                                pass
                        log_warning(
                            f"Falha ao baixar o histórico GLM {os.path.basename(remote_path)}: {e}. "
                            "O frame sai com a cauda incompleta."
                        )
                        return None
                finally:
                    if got_lock:
                        release_file_lock(lock_path)

        download_tasks = [download_one(age, path) for age, path in candidates]
        downloaded = await asyncio.gather(*download_tasks)
        return [r for r in downloaded if r is not None]
    finally:
        try:
            await session.close()
        except Exception as e:
            log_warning(f"Falha ao fechar sessão S3 do histórico GLM: {e}")


def _fetch_flash_history(target_dt, sat_bucket, cache_dir, max_lookback_steps, max_concurrent_history):
    os.makedirs(cache_dir, exist_ok=True)
    history_files = asyncio.run(
        _list_and_download_history(target_dt, sat_bucket, cache_dir, max_lookback_steps, max_concurrent_history)
    )
    parsed = []
    for age_seconds, local_path in history_files:
        try:
            with Dataset(local_path, "r") as nc_past:
                if all(var in nc_past.variables for var in GLM_REQUIRED_VARS):
                    lons = list(nc_past.variables["flash_lon"][:])
                    lats = list(nc_past.variables["flash_lat"][:])
                    parsed.append((age_seconds, lons, lats))
        except Exception as e:
            log_warning(f"Arquivo de histórico GLM ilegível ({os.path.basename(local_path)}): {e}")
            continue
    return parsed


def _rebuild_cmap_for_background(background_key, colors_config):
    spec = registry.get_product(background_key)
    if spec.kind == "reflectance":
        return core.reflectance_cmap()
    if spec.kind in BACKGROUND_COLORBAR_KINDS:
        return core.build_colormap_from_section(colors_config, spec.palette_section)[0]
    raise ValueError(f"produto '{background_key}' (kind '{spec.kind}') não tem colormap definido para uso como fundo do GLM")


def save_background_layer(cache_data_path, layer):
    tmp_path = f"{cache_data_path}.part_{os.getpid()}.npz"
    np.savez_compressed(
        tmp_path,
        kind=layer.kind,
        data=layer.data,
        extent=np.asarray(layer.extent, dtype="float64"),
        sat_h=layer.sat_h,
        sat_lon=layer.sat_lon,
        sat_req=layer.sat_req,
        sat_rpol=layer.sat_rpol,
        title_fragment=layer.title_fragment,
        vmin=np.nan if layer.vmin is None else layer.vmin,
        vmax=np.nan if layer.vmax is None else layer.vmax,
        colorbar_ticks=np.asarray([] if layer.colorbar_ticks is None else layer.colorbar_ticks, dtype="float64"),
        colorbar_unit=layer.colorbar_unit,
        show_colorbar=layer.show_colorbar,
    )
    os.replace(tmp_path, cache_data_path)


def load_background_layer(cache_data_path, background_key, colors_config):
    with np.load(cache_data_path) as npz:
        kind = str(npz["kind"])
        vmin_raw, vmax_raw = float(npz["vmin"]), float(npz["vmax"])
        layer = LayerResult(
            kind=kind,
            data=npz["data"],
            extent=tuple(npz["extent"].tolist()),
            sat_h=float(npz["sat_h"]),
            sat_lon=float(npz["sat_lon"]),
            sat_req=float(npz["sat_req"]),
            sat_rpol=float(npz["sat_rpol"]),
            title_fragment=str(npz["title_fragment"]),
            vmin=None if np.isnan(vmin_raw) else vmin_raw,
            vmax=None if np.isnan(vmax_raw) else vmax_raw,
            colorbar_ticks=npz["colorbar_ticks"] if npz["colorbar_ticks"].size else None,
            colorbar_unit=str(npz["colorbar_unit"]),
            show_colorbar=bool(npz["show_colorbar"]),
        )
    if kind == "mono":
        layer.cmap = _rebuild_cmap_for_background(background_key, colors_config)
    return layer


def resolve_background_layer(target_dt, glm_cfg, remote_background_files, config, colors_config, map_geo, current_gen_type):
    background_key = glm_cfg["background"]
    background_dt = floor_to_abi_step(target_dt)
    if not background_key:
        return None, background_dt

    spec = registry.get_product(background_key)

    def _download_and_render():
        local_paths = download_sources(
            {s.source_id: remote_background_files.get(s.source_id) for s in spec.sources},
            TEMP_NC_DIR, "temp_bg_", f"Fundo {background_key} {background_dt.strftime('%H:%M')} UTC",
        )
        if local_paths is None:
            return None
        try:
            return pipeline.render_layer(spec, local_paths, config, colors_config, map_geo, background_dt)
        except Exception as e:
            log_error(f"Falha ao processar o fundo {background_key}: {e}")
            return None
        finally:
            for path in local_paths.values():
                if os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass

    if current_gen_type == "I":
        return _download_and_render(), background_dt

    sat_bucket, _ = get_satellite_info(background_dt)
    geo_fp = hashlib.sha1(f"{map_geo['projection']}|{map_geo['target_coordinates']}".encode("utf-8")).hexdigest()[:8]
    palette_repr = repr(sorted(registry.palette_snapshot(background_key, colors_config).items()))
    colors_fp = hashlib.sha1(palette_repr.encode("utf-8")).hexdigest()[:8]
    cache_base = os.path.join(
        GLM_NC_CACHE_DIR,
        f"bg_{sat_bucket}_{background_key}_{background_dt.strftime('%Y%j%H%M')}_{geo_fp}_{colors_fp}",
    )
    cache_data_path, lock_path = f"{cache_base}.npz", f"{cache_base}.lock"

    def _load_from_cache():
        return load_background_layer(cache_data_path, background_key, colors_config)

    if os.path.exists(cache_data_path):
        try:
            return _load_from_cache(), background_dt
        except Exception as e:
            log_warning(f"Cache do fundo {background_key} corrompido, regenerando: {e}")

    os.makedirs(GLM_NC_CACHE_DIR, exist_ok=True)
    got_lock = acquire_file_lock(lock_path)
    if not got_lock:
        log_warning(
            f"Timeout esperando o fundo {background_key} de {background_dt.strftime('%H:%M')} UTC "
            "ser renderizado por outro worker. Gerando sem fundo neste frame."
        )
        return None, background_dt
    try:
        if os.path.exists(cache_data_path):
            try:
                return _load_from_cache(), background_dt
            except Exception as e:
                log_warning(f"Cache do fundo {background_key} corrompido, regenerando: {e}")
        log_info(
            f"Fundo {background_key} de {background_dt.strftime('%H:%M')} UTC ainda não está em cache. "
            "Processando uma única vez para todo o bloco de 10 minutos."
        )
        layer = _download_and_render()
        if layer is not None:
            try:
                save_background_layer(cache_data_path, layer)
            except Exception as e:
                log_warning(f"Falha ao salvar o cache do fundo {background_key}: {e}")
        return layer, background_dt
    finally:
        release_file_lock(lock_path)


def generate_image(local_path, image_path, sat_name, pretty_time, config, colors_config, target_dt,
                    remote_background_files, current_gen_type):
    dpi = config.getint("PROCESSING", "dpi")
    map_geo = read_map_geometry_config(config)
    style = read_style_config(config)
    glm_cfg = read_glm_config(config)

    sat_h, sat_lon = core.GOES_EAST_SATELLITE_HEIGHT_M, core.GOES_EAST_SATELLITE_LON_DEG
    sat_req, sat_rpol = core.GRS80_SEMI_MAJOR_AXIS_M, core.GRS80_SEMI_MINOR_AXIS_M

    with Dataset(local_path) as nc:
        all_lons = np.array(nc.variables["flash_lon"][:])
        all_lats = np.array(nc.variables["flash_lat"][:])

    background_layer, background_dt = resolve_background_layer(
        target_dt, glm_cfg, remote_background_files, config, colors_config, map_geo, current_gen_type
    )

    if background_layer is not None:
        axes_sat_h, axes_sat_lon = background_layer.sat_h, background_layer.sat_lon
        axes_sat_req, axes_sat_rpol = background_layer.sat_req, background_layer.sat_rpol
    else:
        axes_sat_h, axes_sat_lon, axes_sat_req, axes_sat_rpol = sat_h, sat_lon, sat_req, sat_rpol

    background_key = glm_cfg["background"]
    reserve_background_colorbar = (
        background_key is not None
        and registry.get_product(background_key).kind in BACKGROUND_COLORBAR_KINDS
        and not style["clean_mode"]
    )
    needs_density_colorbar = glm_cfg["render_mode"] == "DENSITY" and not style["clean_mode"]
    reserve_right_frac = pipeline.colorbar_reserve_frac(int(reserve_background_colorbar) + int(needs_density_colorbar))

    figsize = (style["figure_width"], style["figure_height"])
    fig, ax, geo_proj, title_ax = pipeline.create_map_axes(
        map_geo["projection"], axes_sat_lon, axes_sat_h, style["clean_mode"],
        map_geo["target_coordinates"], figsize=figsize,
        semi_major_axis=axes_sat_req, semi_minor_axis=axes_sat_rpol,
        reserve_right_frac=reserve_right_frac,
    )

    transparent = glm_cfg["background"] is None
    if transparent:
        for spine in ax.spines.values():
            spine.set_visible(False)

    try:
        if background_layer is not None:
            background_image = pipeline.draw_layer_on_axes(ax, geo_proj, style, background_layer)
            if reserve_background_colorbar and background_layer.show_colorbar:
                pipeline.draw_colorbar(fig, ax, background_image, background_layer)
        else:
            pipeline.add_geo_features(ax, style)
            if glm_cfg["background"]:
                log_warning(f"Prosseguindo sem o fundo {glm_cfg['background']} neste frame ({pretty_time}).")

        if glm_cfg["render_mode"] == "DENSITY":
            flash_count = _draw_density(ax, fig, all_lons, all_lats, map_geo, glm_cfg, colors_config, style,
                                        colorbar_slot=int(reserve_background_colorbar))
            title_kind = f"Densidade de Raios, {flash_count} raios ({target_dt.strftime('%H:%M:%S')} UTC)"
        else:
            title_kind = _draw_points(ax, all_lons, all_lats, target_dt, config, colors_config, glm_cfg, style)

        if glm_cfg["background"] and background_layer is not None:
            title_text = (f"{sat_name} | GLM — {title_kind} | "
                          f"{glm_cfg['background']} ({background_dt.strftime('%H:%M')} UTC) | "
                          f"{target_dt.strftime('%d/%m/%Y')}")
        else:
            title_text = f"{sat_name} | GLM — {title_kind} | {target_dt.strftime('%d/%m/%Y')}"
        pipeline.add_watermark_and_title(ax, title_ax, style, title_text)
    except Exception:
        plt.close(fig)
        raise

    pipeline.save_figure(fig, image_path, dpi, transparent=transparent)


def _draw_points(ax, all_lons, all_lats, target_dt, config, colors_config, glm_cfg, style):
    show_flash_age = glm_cfg["flash_age"]
    max_lookback_steps = config.getint("PROCESSING", "glm_history_lookback_steps")
    max_concurrent_history = config.getint("PROCESSING", "glm_history_max_concurrent_downloads")
    max_history_age_seconds = max_lookback_steps * GLM_STEP_SECONDS
    flash_marker_min = glm_cfg["flash_marker_min"]
    flash_marker_max = glm_cfg["flash_marker_max"]

    default_color, sorted_colors = _parse_age_palette(colors_config, show_flash_age)

    plot_lons, plot_lats, plot_colors, plot_sizes = [], [], [], []

    current_color = _get_color_for_age(0, sorted_colors) if sorted_colors else default_color
    current_size = (
        _get_size_for_age(0, max_history_age_seconds, flash_marker_min, flash_marker_max)
        if show_flash_age else flash_marker_max
    )
    plot_lons.extend(all_lons)
    plot_lats.extend(all_lats)
    plot_colors.extend([current_color] * len(all_lons))
    plot_sizes.extend([current_size] * len(all_lons))

    if show_flash_age and sorted_colors:
        sat_bucket, _ = get_satellite_info(target_dt)
        history = _fetch_flash_history(
            target_dt, sat_bucket, GLM_NC_CACHE_DIR, max_lookback_steps, max_concurrent_history
        )
        for age_seconds, lons, lats in history:
            plot_lons.extend(lons)
            plot_lats.extend(lats)
            plot_colors.extend([_get_color_for_age(age_seconds, sorted_colors)] * len(lons))
            flash_size = _get_size_for_age(age_seconds, max_history_age_seconds, flash_marker_min, flash_marker_max)
            plot_sizes.extend([flash_size] * len(lons))

    if plot_lons:
        ax.scatter(plot_lons, plot_lats, color=plot_colors, s=plot_sizes, alpha=0.9,
                   transform=ccrs.PlateCarree(), zorder=5)

    if not style["clean_mode"] and sorted_colors:
        legend_patches = []
        n_colors = len(sorted_colors)
        for i, (limit, hex_color) in enumerate(sorted_colors):
            if i == 0 and n_colors > 1:
                label = f"≤ {sorted_colors[1][0] / 60:.1f} min"
            elif i + 1 < n_colors:
                next_limit = sorted_colors[i + 1][0]
                label = f"{limit / 60:.1f}–{next_limit / 60:.1f} min"
            else:
                label = f"≥ {limit / 60:.1f} min"
            label = label.replace(".", ",")
            legend_patches.append(mpatches.Patch(color=hex_color, label=label))

        legend = ax.legend(handles=legend_patches, loc="lower left", facecolor="black",
                            edgecolor="white", labelcolor="white", fontsize=12)
        legend.set_zorder(6)

    return f"{len(all_lons)} raios ({target_dt.strftime('%H:%M:%S')} UTC)"


def _draw_density(ax, fig, all_lons, all_lats, map_geo, glm_cfg, colors_config, style, colorbar_slot):
    resolution_km = glm_cfg["density_grid_resolution_km"]
    lon_edges, lat_edges, counts = compute_density_grid(
        all_lons, all_lats, map_geo["target_coordinates"], resolution_km
    )
    cmap, vmin, vmax = core.build_colormap_from_section(colors_config, "PALETTE_GLM_DENSITY")

    mesh = ax.pcolormesh(
        lon_edges, lat_edges, counts, cmap=cmap, vmin=vmin, vmax=vmax,
        transform=ccrs.PlateCarree(), zorder=5, shading="flat",
    )

    if not style["clean_mode"]:
        cax = pipeline.add_colorbar_axes(fig, ax, slot=colorbar_slot)
        tick_step = max(1, round((vmax - vmin) / 6))
        ticks = np.arange(vmin, vmax + 1, tick_step)
        colorbar = plt.colorbar(mesh, cax=cax, orientation="vertical", ticks=ticks)
        colorbar.ax.set_yticklabels([f"{int(t)}" for t in ticks], color="white", size=10)
        colorbar.set_label("raios / célula", color="white", size=10)
        colorbar.outline.set_edgecolor("white")

    return len(all_lons)