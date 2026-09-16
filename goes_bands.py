from datetime import datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
from netCDF4 import Dataset
import numpy as np

from utils import (
    read_map_geometry_config,
    read_style_config,
    get_projection_params,
    create_map_axes,
    add_geo_features,
    add_watermark,
    add_map_title,
    save_figure,
    TITLE_FONTSIZE_PT,
)

COLORBAR_FRACTION = 0.03
COLORBAR_PAD = 0.04
COLORBAR_LABEL_RESERVE = 0.06
COLORBAR_VERTICAL_MARGIN_FRAC = 0.02

def add_colorbar_axes(fig, ax, fraction=0.03, pad=0.04):
    map_pos = ax.get_position()
    cbar_width = map_pos.width * fraction
    cbar_pad = map_pos.width * pad
    cbar_y0 = map_pos.y0 + map_pos.height * COLORBAR_VERTICAL_MARGIN_FRAC
    cbar_height = map_pos.height * (1.0 - 2 * COLORBAR_VERTICAL_MARGIN_FRAC)
    cax = fig.add_axes([map_pos.x1 + cbar_pad, cbar_y0, cbar_width, cbar_height])
    return cax

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

REFLECTANCE_BANDS = {
    1: ("Visível Azul", "0,47"),
    2: ("Visível Vermelho", "0,64"),
    3: ("Próximo ao Infravermelho - Vegetação", "0,86"),
    4: ("Próximo ao Infravermelho - Cirrus", "1,37"),
    5: ("Próximo ao Infravermelho - Neve/Gelo", "1,61"),
    6: ("Próximo ao Infravermelho - Tamanho de Partícula de Nuvem", "2,24"),
}

THERMAL_BANDS = {
    7: ("Infravermelho de Onda Curta", "3,90", 20),
    8: ("Vapor d'água de Altos Níveis", "6,19", 10),
    9: ("Vapor d'água de Médios Níveis", "6,95", 10),
    10: ("Vapor d'água de Baixos Níveis", "7,34", 10),
    11: ("Infravermelho - Fase de Topo de Nuvem", "8,44", 10),
    12: ("Infravermelho - Ozônio", "9,61", 10),
    13: ("Infravermelho Térmico - Janela Limpa", "10,33", 10),
    14: ("Infravermelho Térmico - Janela Principal", "11,19", 10),
    15: ("Infravermelho Térmico - Janela Suja", "12,27", 10),
    16: ("Infravermelho - Dióxido de Carbono", "13,27", 10),
}

def format_pretty_time(pretty_time):
    if not pretty_time:
        return pretty_time
    time_str = str(pretty_time).strip()
    if time_str.isdigit() and len(time_str) == 11:
        return datetime.strptime(time_str, "%Y%j%H%M").strftime("%d/%m/%Y %H:%M")
    return pretty_time

def compute_image_extent(x_rad, y_rad, sat_height):
    return (
        x_rad.min() * sat_height, x_rad.max() * sat_height,
        y_rad.min() * sat_height, y_rad.max() * sat_height,
    )

def is_valid_band(band_id):
    return band_id in REFLECTANCE_BANDS or band_id in THERMAL_BANDS

def _build_thermal_colormap(config, band_id):
    section = f"PALETTE_BAND_{band_id:02d}"
    colors_dict = {int(key): value for key, value in config.items(section)}
    min_v = min(colors_dict.keys())
    max_v = max(colors_dict.keys())
    norm_list = sorted(((value - min_v) / (max_v - min_v), color) for value, color in colors_dict.items())
    cmap = mcolors.LinearSegmentedColormap.from_list(f"THERMAL_{band_id:02d}", norm_list)
    return cmap, min_v, max_v

def extract_band_data(local_path, config, band_id, map_geo):
    is_reflectance = band_id in REFLECTANCE_BANDS
    nc = Dataset(local_path)
    try:
        sat_h, sat_lon, sat_req, sat_rpol = get_projection_params(nc)
        row_slice, col_slice = get_crop_slices(nc, map_geo["target_coordinates"])

        if is_reflectance:
            band_title, wavelength = REFLECTANCE_BANDS[band_id]
            data = np.clip(nc.variables["CMI"][row_slice, col_slice], 0, 1) ** (1 / 2.2)
            cmap, vmin, vmax, tick_step = "gray", 0, 1, None
        else:
            band_title, wavelength, tick_step = THERMAL_BANDS[band_id]
            data = (nc.variables["CMI"][row_slice, col_slice] - 273.15).astype("float32")
            cmap, vmin, vmax = _build_thermal_colormap(config, band_id)

        x_rad = nc.variables["x"][col_slice]
        y_rad = nc.variables["y"][row_slice]
        img_extent = compute_image_extent(x_rad, y_rad, sat_h)
    finally:
        nc.close()

    return {
        "is_reflectance": is_reflectance,
        "data": np.asarray(data),
        "img_extent": img_extent,
        "cmap": cmap,
        "vmin": vmin,
        "vmax": vmax,
        "band_title": band_title,
        "wavelength": wavelength,
        "tick_step": tick_step,
        "sat_h": sat_h,
        "sat_lon": sat_lon,
        "sat_req": sat_req,
        "sat_rpol": sat_rpol,
    }

def rebuild_cmap_for_band(config, band_id, is_reflectance):
    if is_reflectance:
        return "gray"
    cmap, _, _ = _build_thermal_colormap(config, band_id)
    return cmap

def draw_band_on_axes(ax, geo_proj, band_data, style):
    add_geo_features(ax, style)
    im = ax.imshow(band_data["data"], origin="upper", cmap=band_data["cmap"],
                    vmin=band_data["vmin"], vmax=band_data["vmax"],
                    extent=band_data["img_extent"], transform=geo_proj,
                    interpolation="bicubic", zorder=1)
    return im

def _render_common(local_path, config, band_id, map_geo, style):
    band_data = extract_band_data(local_path, config, band_id, map_geo)

    needs_colorbar = (not band_data["is_reflectance"]) and (not style["clean_mode"])
    reserve_right_frac = (
        COLORBAR_FRACTION + COLORBAR_PAD + COLORBAR_LABEL_RESERVE
    ) if needs_colorbar else 0.0

    figsize = (style["figure_width"], style["figure_height"])
    fig, ax, geo_proj, title_ax, _ = create_map_axes(
        map_geo["projection"], band_data["sat_lon"], band_data["sat_h"],
        style["clean_mode"], map_geo["target_coordinates"], figsize=figsize,
        semi_major_axis=band_data["sat_req"], semi_minor_axis=band_data["sat_rpol"],
        title_fontsize_pt=None if style["clean_mode"] else TITLE_FONTSIZE_PT,
        reserve_right_frac=reserve_right_frac,
    )

    im = draw_band_on_axes(ax, geo_proj, band_data, style)

    return (fig, ax, im, title_ax, band_data["is_reflectance"], band_data["band_title"],
            band_data["wavelength"], band_data["vmin"], band_data["vmax"], band_data["tick_step"])

def generate_image(local_path, png_path, sat_name, pretty_time, config, band_id):
    pretty_time = format_pretty_time(pretty_time)
    dpi = config.getint("PROCESSING", "dpi")
    map_geo = read_map_geometry_config(config)
    style = read_style_config(config)

    fig, ax, im, title_ax, is_reflectance, band_title, wavelength, vmin, vmax, tick_step = _render_common(
        local_path, config, band_id, map_geo, style
    )

    if not style["clean_mode"]:
        if not is_reflectance:
            ticks = np.arange(vmin, vmax + 1, tick_step)
            cax = add_colorbar_axes(fig, ax, fraction=COLORBAR_FRACTION, pad=COLORBAR_PAD)
            colorbar = plt.colorbar(im, cax=cax, orientation="vertical", ticks=ticks)
            colorbar.ax.set_yticklabels([f"{tick}°C" for tick in ticks], color="white", size=10)
            colorbar.outline.set_edgecolor("white")

        add_watermark(ax, style["watermark"], color=style["watermark_color"], size=style["watermark_size"])
        if title_ax is not None:
            title_text = f"{sat_name} | C{band_id:02d} — {band_title} ({wavelength} µm) | {pretty_time} UTC"
            add_map_title(title_ax, title_text, TITLE_FONTSIZE_PT)

    save_figure(fig, png_path, dpi)