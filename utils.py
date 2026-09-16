import os
import time
from datetime import datetime, timezone

import matplotlib
matplotlib.use("Agg")
matplotlib.rcParams["font.family"] = "DejaVu Sans"
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
import matplotlib.patheffects as path_effects
import matplotlib.transforms as mtransforms
from matplotlib.textpath import TextPath
from matplotlib.patches import PathPatch
from matplotlib.font_manager import FontProperties
import cartopy.crs as ccrs
import cartopy.feature as cfeature

GOES_TRANSITION_DATE = datetime(2025, 4, 7, tzinfo=timezone.utc)

LOCK_POLL_INTERVAL_S = 0.5
LOCK_STALE_TIMEOUT_S = 300

TEMP_NC_DIR = "satelite_temp_downloads"
GLM_NC_CACHE_DIR = os.path.join(TEMP_NC_DIR, "glm_nc_cache")
ABI_REQUIRED_VARS = ["CMI"]

LOG_COLLECTOR_STATE = {"collector": None}

def _record_log(level, message):
    collector = LOG_COLLECTOR_STATE["collector"]
    if collector is not None:
        try:
            collector.append((level, message))
        except Exception:
            pass

def log_error(message):
    print(f"❌ ERRO: {message}")
    _record_log("ERRO", message)

def log_warning(message):
    print(f"⚠️ AVISO: {message}")
    _record_log("AVISO", message)

def log_info(message):
    print(f"ℹ️ {message}")

def get_satellite_info(target_dt):
    if target_dt < GOES_TRANSITION_DATE:
        return "noaa-goes16", "GOES-16"
    return "noaa-goes19", "GOES-19"

ABI_STEP_MINUTES = 10

def floor_to_abi_step(dt):
    floored_minute = (dt.minute // ABI_STEP_MINUTES) * ABI_STEP_MINUTES
    return dt.replace(minute=floored_minute, second=0, microsecond=0)

def read_map_geometry_config(config):
    projection = config.get("MAP", "projection").strip().upper()
    if projection not in ("P", "C"):
        raise ValueError(f"projection deve ser 'P' ou 'C', valor recebido: '{projection}'")
    coords_raw = config.get("MAP", "target_coordinates").split(",")
    if len(coords_raw) != 4:
        raise ValueError(
            "target_coordinates deve ter exatamente 4 valores "
            f"(oeste, leste, sul, norte), recebido: '{config.get('MAP', 'target_coordinates')}'"
        )
    target_coordinates = [float(value.strip()) for value in coords_raw]

    flash_marker_min = config.getfloat("MAP", "glm_flash_marker_min")
    flash_marker_max = config.getfloat("MAP", "glm_flash_marker_max")
    if flash_marker_min <= 0 or flash_marker_max <= 0:
        raise ValueError(
            "glm_flash_marker_min e glm_flash_marker_max devem ser números positivos, "
            f"recebido: min={flash_marker_min}, max={flash_marker_max}"
        )
    if flash_marker_min > flash_marker_max:
        raise ValueError(
            "glm_flash_marker_min não pode ser maior que glm_flash_marker_max, "
            f"recebido: min={flash_marker_min}, max={flash_marker_max}"
        )

    return {
        "projection": projection,
        "target_coordinates": target_coordinates,
        "glm_flash_marker_min": flash_marker_min,
        "glm_flash_marker_max": flash_marker_max,
    }

def _validate_color(key, raw_value):
    value = raw_value.strip()
    if not mcolors.is_color_like(value):
        raise ValueError(
            f"{key} não é uma cor reconhecida pelo matplotlib: '{value}' "
            "(use um hexadecimal #RRGGBB ou um nome válido, ex: 'gray' em vez de 'grey')"
        )
    return value

def read_style_config(config):
    watermark_size = config.getfloat("STYLE", "watermark_size")
    if watermark_size <= 0:
        raise ValueError(f"watermark_size deve ser um número positivo, recebido: {watermark_size}")

    borders_color = _validate_color("borders_color", config.get("STYLE", "borders_color"))
    states_color = _validate_color("states_color", config.get("STYLE", "states_color"))
    coast_color = _validate_color("coast_color", config.get("STYLE", "coast_color"))

    watermark_color = config.get("STYLE", "watermark_color").strip()
    if watermark_color:
        _validate_color("watermark_color", watermark_color)

    return {
        "watermark": config.get("STYLE", "watermark"),
        "watermark_color": watermark_color,
        "watermark_size": watermark_size,
        "clean_mode": config.getboolean("STYLE", "clean_mode"),
        "borders_color": borders_color,
        "borders_line_width": config.getfloat("STYLE", "borders_line_width"),
        "states_color": states_color,
        "states_line_width": config.getfloat("STYLE", "states_line_width"),
        "coast_color": coast_color,
        "coast_line_width": config.getfloat("STYLE", "coast_line_width"),
        "figure_width": config.getfloat("STYLE", "figure_width"),
        "figure_height": config.getfloat("STYLE", "figure_height"),
    }

def get_projection_params(nc, variable_name="goes_imager_projection"):
    projection_var = nc.variables[variable_name]
    height = projection_var.perspective_point_height
    lon = projection_var.longitude_of_projection_origin
    req = getattr(projection_var, "semi_major_axis", None)
    rpol = getattr(projection_var, "semi_minor_axis", None)
    return height, lon, req, rpol

TITLE_FONTSIZE_PT = 16

TITLE_BAND_LINE_FACTOR = 1.5
TITLE_BAND_FRAC_MIN = 0.012
TITLE_BAND_FRAC_MAX = 0.25

def compute_title_band_fraction(figure_height_in, title_fontsize_pt):
    if figure_height_in <= 0:
        raise ValueError(f"figure_height deve ser positivo, recebido: {figure_height_in}")
    if title_fontsize_pt <= 0:
        raise ValueError(f"título fontsize deve ser positivo, recebido: {title_fontsize_pt}")
    band_height_in = (title_fontsize_pt * TITLE_BAND_LINE_FACTOR) / 72.0
    frac = band_height_in / figure_height_in
    return min(max(frac, TITLE_BAND_FRAC_MIN), TITLE_BAND_FRAC_MAX)

def _build_map_projection(projection_type, sat_lon, sat_height, semi_major_axis, semi_minor_axis):
    if semi_major_axis and semi_minor_axis:
        globe = ccrs.Globe(semimajor_axis=semi_major_axis, semiminor_axis=semi_minor_axis, ellipse=None)
    else:
        globe = None
    geo_proj = ccrs.Geostationary(central_longitude=sat_lon, satellite_height=sat_height, globe=globe)
    map_proj = ccrs.PlateCarree() if projection_type == "P" else geo_proj
    return geo_proj, map_proj

def compute_fitted_map_figsize(projection_type, sat_lon, sat_height, target_coordinates,
                                base_figsize, semi_major_axis=None, semi_minor_axis=None):
    probe_fig = plt.figure(figsize=base_figsize)
    try:
        _, map_proj = _build_map_projection(projection_type, sat_lon, sat_height,
                                             semi_major_axis, semi_minor_axis)
        probe_ax = probe_fig.add_axes([0, 0, 1, 1], projection=map_proj)
        probe_ax.set_extent(target_coordinates, crs=ccrs.PlateCarree())
        probe_fig.canvas.draw()
        pos = probe_ax.get_position()
        fitted_w = pos.width * base_figsize[0]
        fitted_h = pos.height * base_figsize[1]
        return fitted_w, fitted_h
    finally:
        plt.close(probe_fig)

def create_map_axes(projection_type, sat_lon, sat_height, clean_mode, target_coordinates,
                     figsize=(16, 14), semi_major_axis=None, semi_minor_axis=None,
                     title_fontsize_pt=None, reserve_right_frac=0.0):
    full_width_in, base_height_in = figsize
    map_width_in = full_width_in * (1.0 - reserve_right_frac) if reserve_right_frac else full_width_in

    fitted_w, fitted_h = compute_fitted_map_figsize(
        projection_type, sat_lon, sat_height, target_coordinates,
        (map_width_in, base_height_in), semi_major_axis, semi_minor_axis,
    )
    total_w = fitted_w / (1.0 - reserve_right_frac) if reserve_right_frac else fitted_w
    map_width_frac = fitted_w / total_w

    geo_proj, map_proj = _build_map_projection(projection_type, sat_lon, sat_height,
                                                semi_major_axis, semi_minor_axis)

    if clean_mode:
        fig = plt.figure(figsize=(total_w, fitted_h), facecolor="black")
        ax = fig.add_axes([0, 0, map_width_frac, 1], projection=map_proj)
        ax.axis("off")
        ax.set_extent(target_coordinates, crs=ccrs.PlateCarree())
        return fig, ax, geo_proj, None, map_width_frac

    if title_fontsize_pt is None:
        fig = plt.figure(figsize=(total_w, fitted_h), facecolor="black")
        ax = fig.add_axes([0, 0, map_width_frac, 1], projection=map_proj)
        ax.set_extent(target_coordinates, crs=ccrs.PlateCarree())
        return fig, ax, geo_proj, None, map_width_frac

    title_frac = compute_title_band_fraction(fitted_h, title_fontsize_pt)
    total_h = fitted_h / (1.0 - title_frac)
    map_frac_h = 1.0 - title_frac

    fig = plt.figure(figsize=(total_w, total_h), facecolor="black")

    title_ax = fig.add_axes([0, map_frac_h, 1, title_frac])
    title_ax.axis("off")
    title_ax.set_facecolor("black")

    ax = fig.add_axes([0, 0, map_width_frac, map_frac_h], projection=map_proj)
    ax.set_extent(target_coordinates, crs=ccrs.PlateCarree())

    return fig, ax, geo_proj, title_ax, map_width_frac

def add_geo_features(ax, style):
    ax.add_feature(cfeature.STATES.with_scale("10m"), linewidth=style["states_line_width"],
                    edgecolor=style["states_color"], alpha=0.7, zorder=2)
    ax.add_feature(cfeature.BORDERS.with_scale("10m"), linewidth=style["borders_line_width"],
                    edgecolor=style["borders_color"], alpha=1.0, zorder=3)
    ax.add_feature(cfeature.COASTLINE.with_scale("10m"), linewidth=style["coast_line_width"],
                    edgecolor=style["coast_color"], alpha=0.8, zorder=4)

def add_watermark(ax, watermark, color="#00FF00", size=18):
    watermark = (watermark or "").strip()
    if not watermark:
        return
    text = ax.text(0.98, 0.02, watermark, transform=ax.transAxes, color=color,
                    fontsize=size, fontweight="bold", ha="right", va="bottom", alpha=0.8)
    text.set_path_effects([path_effects.withStroke(linewidth=3, foreground="black")])

TITLE_AVAILABLE_WIDTH_FRAC = 0.96
TITLE_MIN_HORIZONTAL_SCALE = 0.5

def add_map_title(title_ax, text, fontsize_pt):
    fig = title_ax.get_figure()
    font_props = FontProperties(family="DejaVu Sans", size=fontsize_pt)
    text_path = TextPath((0, 0), text, prop=font_props)
    path_bbox = text_path.get_extents()

    fig_width_in, fig_height_in = fig.get_size_inches()
    ax_pos = title_ax.get_position()
    ax_width_in = ax_pos.width * fig_width_in
    ax_height_in = ax_pos.height * fig_height_in

    if path_bbox.width <= 0 or ax_width_in <= 0:
        scale_x = 1.0
    else:
        text_width_in = path_bbox.width / 72.0
        available_width_in = ax_width_in * TITLE_AVAILABLE_WIDTH_FRAC
        scale_x = 1.0
        if text_width_in > available_width_in:
            scale_x = max(TITLE_MIN_HORIZONTAL_SCALE, available_width_in / text_width_in)

    center_x_pt = path_bbox.x0 + path_bbox.width / 2.0
    center_y_pt = path_bbox.y0 + path_bbox.height / 2.0
    pt_to_axesfrac_x = (1.0 / 72.0) / ax_width_in
    pt_to_axesfrac_y = (1.0 / 72.0) / ax_height_in

    transform = (
        mtransforms.Affine2D()
        .translate(-center_x_pt, -center_y_pt)
        .scale(pt_to_axesfrac_x * scale_x, pt_to_axesfrac_y)
        .translate(0.5, 0.5)
        + title_ax.transAxes
    )
    patch = PathPatch(text_path, transform=transform, facecolor="white", edgecolor="none")
    title_ax.add_patch(patch)

def save_figure(fig, png_path, dpi):
    fig.savefig(png_path, facecolor="black", dpi=dpi)
    fig.clf()
    plt.close(fig)

def glm_nc_cache_paths(cache_dir, remote_path):
    base_name = os.path.basename(remote_path).replace(".nc", "")
    cache_data = os.path.join(cache_dir, f"glm_{base_name}.nc")
    lock_path = os.path.join(cache_dir, f"glm_{base_name}.lock")
    return cache_data, lock_path

def acquire_background_lock(lock_path):
    start = time.time()
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            os.write(fd, str(time.time()).encode("utf-8"))
            os.close(fd)
            return True
        except FileExistsError:
            try:
                with open(lock_path, "r", encoding="utf-8") as f:
                    timestamp_str = f.read().strip()
                if timestamp_str:
                    lock_time = float(timestamp_str)
                    if time.time() - lock_time > LOCK_STALE_TIMEOUT_S:
                        log_warning("Removendo lock obsoleto do fundo.")
                        os.remove(lock_path)
                        continue
            except Exception:
                pass
            if time.time() - start > LOCK_STALE_TIMEOUT_S:
                return False
            time.sleep(LOCK_POLL_INTERVAL_S)

def release_background_lock(lock_path):
    try:
        if os.path.exists(lock_path):
            os.remove(lock_path)
    except OSError:
        pass