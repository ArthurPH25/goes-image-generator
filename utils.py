import os
import sys
import time
from datetime import datetime, timezone

import matplotlib
matplotlib.use("Agg")
matplotlib.rcParams["font.family"] = "DejaVu Sans"
import matplotlib.pyplot as plt
import matplotlib.patheffects as path_effects
import matplotlib.transforms as mtransforms
from matplotlib.textpath import TextPath
from matplotlib.patches import PathPatch
from matplotlib.font_manager import FontProperties
import cartopy.crs as ccrs
import cartopy.feature as cfeature
import numpy as np

GOES_TRANSITION_DATE = datetime(2025, 4, 7, tzinfo=timezone.utc)

LOCK_POLL_INTERVAL_S = 0.5
LOCK_STALE_TIMEOUT_S = 300

def log_fatal(message):
    print(f"☠️ ERRO FATAL: {message}")

def log_error(message):
    print(f"❌ ERRO: {message}")

def log_warning(message):
    print(f"⚠️ AVISO: {message}")

def log_success(message):
    print(f"✅ {message}")

def log_info(message):
    print(f"ℹ️ {message}")

def exit_fatal(message):
    log_fatal(message)
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
        raise ValueError(
            f"horário '{time_str}' sem segundos: canal GLM exige o formato HH:MM:SS"
        )
    if not require_seconds and has_seconds:
        raise ValueError(
            f"horário '{time_str}' com segundos: canal ABI exige o formato HH:MM, sem segundos"
        )
    time_format = "%H:%M:%S" if has_seconds else "%H:%M"
    return datetime.strptime(full_str, f"%Y-%m-%d {time_format}").replace(tzinfo=timezone.utc)

def format_pretty_time(pretty_time):
    if not pretty_time:
        return pretty_time
    time_str = str(pretty_time).strip()
    if time_str.isdigit() and len(time_str) == 11:
        return datetime.strptime(time_str, "%Y%j%H%M").strftime("%d/%m/%Y %H:%M")
    return pretty_time

def get_satellite_info(target_dt):
    if target_dt < GOES_TRANSITION_DATE:
        return "noaa-goes16", "GOES-16"
    return "noaa-goes19", "GOES-19"

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

def read_style_config(config):
    watermark_size = config.getfloat("STYLE", "watermark_size")
    if watermark_size <= 0:
        raise ValueError(f"watermark_size deve ser um número positivo, recebido: {watermark_size}")
    return {
        "watermark": config.get("STYLE", "watermark"),
        "watermark_color": config.get("STYLE", "watermark_color").strip(),
        "watermark_size": watermark_size,
        "clean_mode": config.getboolean("STYLE", "clean_mode"),
        "borders_color": config.get("STYLE", "borders_color"),
        "borders_line_width": config.getfloat("STYLE", "borders_line_width"),
        "states_color": config.get("STYLE", "states_color"),
        "states_line_width": config.getfloat("STYLE", "states_line_width"),
        "coast_color": config.get("STYLE", "coast_color"),
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

def compute_image_extent(x_rad, y_rad, sat_height):
    return (
        x_rad.min() * sat_height, x_rad.max() * sat_height,
        y_rad.min() * sat_height, y_rad.max() * sat_height,
    )

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

COLORBAR_VERTICAL_MARGIN_FRAC = 0.02

def add_colorbar_axes(fig, ax, fraction=0.03, pad=0.04):
    map_pos = ax.get_position()
    cbar_width = map_pos.width * fraction
    cbar_pad = map_pos.width * pad
    cbar_y0 = map_pos.y0 + map_pos.height * COLORBAR_VERTICAL_MARGIN_FRAC
    cbar_height = map_pos.height * (1.0 - 2 * COLORBAR_VERTICAL_MARGIN_FRAC)
    cax = fig.add_axes([map_pos.x1 + cbar_pad, cbar_y0, cbar_width, cbar_height])
    return cax

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

def save_figure(fig, png_path, dpi, clean_mode):
    plt.savefig(png_path, facecolor="black", dpi=dpi)
    fig.clf()
    plt.close(fig)

def _lonlat_to_scan_angle(lon, lat, sat_lon, sat_height_total, req, rpol):
    lam0 = np.radians(sat_lon)
    phi = np.radians(lat)
    lam = np.radians(lon)
    e2 = 1.0 - (rpol ** 2) / (req ** 2)
    phi_c = np.arctan((rpol ** 2 / req ** 2) * np.tan(phi))
    rc = rpol / np.sqrt(1.0 - e2 * np.cos(phi_c) ** 2)
    sx = sat_height_total - rc * np.cos(phi_c) * np.cos(lam - lam0)
    sy = -rc * np.cos(phi_c) * np.sin(lam - lam0)
    sz = rc * np.sin(phi_c)
    y = np.arctan(sz / sx)
    x = np.arcsin(-sy / np.sqrt(sx ** 2 + sy ** 2 + sz ** 2))
    return x, y

def get_crop_slices(nc, target_coords, margin_frac=0.05, min_margin_px=6):
    lon_w, lon_e, lat_s, lat_n = target_coords
    proj = nc.variables["goes_imager_projection"]
    sat_lon = proj.longitude_of_projection_origin
    req = proj.semi_major_axis
    rpol = proj.semi_minor_axis
    sat_height_total = proj.perspective_point_height + req
    corner_lons = np.array([lon_w, lon_w, lon_e, lon_e])
    corner_lats = np.array([lat_s, lat_n, lat_s, lat_n])
    xs, ys = _lonlat_to_scan_angle(corner_lons, corner_lats, sat_lon, sat_height_total, req, rpol)
    valid = np.isfinite(xs) & np.isfinite(ys)
    if not valid.any():
        return slice(None), slice(None)
    xs, ys = xs[valid], ys[valid]
    x_rad = nc.variables["x"][:]
    y_rad = nc.variables["y"][:]
    col_start_raw = int(np.searchsorted(x_rad, xs.min(), side="left"))
    col_end_raw = int(np.searchsorted(x_rad, xs.max(), side="right"))
    y_desc = y_rad[::-1]
    row_start_desc = int(np.searchsorted(y_desc, ys.min(), side="left"))
    row_end_desc = int(np.searchsorted(y_desc, ys.max(), side="right"))
    n_y = len(y_rad)
    row_start_raw = n_y - row_end_desc
    row_end_raw = n_y - row_start_desc
    margin_cols = max(min_margin_px, int((col_end_raw - col_start_raw) * margin_frac))
    margin_rows = max(min_margin_px, int((row_end_raw - row_start_raw) * margin_frac))
    col_start = max(col_start_raw - margin_cols, 0)
    col_end = min(col_end_raw + margin_cols, len(x_rad))
    row_start = max(row_start_raw - margin_rows, 0)
    row_end = min(row_end_raw + margin_rows, n_y)
    return slice(row_start, row_end), slice(col_start, col_end)

def background_cache_paths(cache_dir, satellite_bucket, band_id, background_dt):
    time_key = background_dt.strftime("%Y%j%H%M")
    base_name = f"bg_{satellite_bucket}_C{band_id:02d}_{time_key}"
    cache_data = os.path.join(cache_dir, f"{base_name}.npz")
    lock_path = os.path.join(cache_dir, f"{base_name}.lock")
    return cache_data, lock_path

def save_background_data(cache_data_path, band_data):
    np.savez_compressed(
        cache_data_path,
        is_reflectance=band_data["is_reflectance"],
        data=band_data["data"],
        img_extent=np.asarray(band_data["img_extent"], dtype="float64"),
        vmin=band_data["vmin"],
        vmax=band_data["vmax"],
        sat_h=band_data["sat_h"],
        sat_lon=band_data["sat_lon"],
        sat_req=band_data["sat_req"],
        sat_rpol=band_data["sat_rpol"],
    )

def load_background_data(cache_data_path):
    with np.load(cache_data_path) as npz:
        is_reflectance = bool(npz["is_reflectance"])
        return {
            "is_reflectance": is_reflectance,
            "data": npz["data"],
            "img_extent": tuple(npz["img_extent"].tolist()),
            "vmin": float(npz["vmin"]),
            "vmax": float(npz["vmax"]),
            "sat_h": float(npz["sat_h"]),
            "sat_lon": float(npz["sat_lon"]),
            "sat_req": float(npz["sat_req"]),
            "sat_rpol": float(npz["sat_rpol"]),
        }

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