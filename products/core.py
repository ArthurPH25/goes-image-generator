from dataclasses import dataclass
from typing import Optional, Sequence

import matplotlib
import matplotlib.colors as mcolors
import numpy as np

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

NO_DATA_COLOR = "black"

GOES_EAST_SATELLITE_HEIGHT_M = 35786023.0
GOES_EAST_SATELLITE_LON_DEG = -75.0
GRS80_SEMI_MAJOR_AXIS_M = 6378137.0
GRS80_SEMI_MINOR_AXIS_M = 6356752.31414


def build_colormap_from_section(colors_config, section, cmap_name=None):
    colors_dict = {int(key): value for key, value in colors_config.items(section)}
    min_v = min(colors_dict.keys())
    max_v = max(colors_dict.keys())
    norm_list = sorted(((value - min_v) / (max_v - min_v), color) for value, color in colors_dict.items())
    cmap = mcolors.LinearSegmentedColormap.from_list(cmap_name or section, norm_list).with_extremes(bad=NO_DATA_COLOR)
    return cmap, min_v, max_v


def reflectance_cmap():
    return matplotlib.colormaps["gray"].with_extremes(bad=NO_DATA_COLOR)


@dataclass
class LayerResult:
    kind: str
    data: np.ndarray
    extent: tuple
    sat_h: float
    sat_lon: float
    sat_req: float
    sat_rpol: float
    title_fragment: str
    cmap: object = None
    vmin: Optional[float] = None
    vmax: Optional[float] = None
    colorbar_ticks: Optional[Sequence[float]] = None
    colorbar_unit: str = ""
    show_colorbar: bool = False


def band_label(band_id):
    if band_id in REFLECTANCE_BANDS:
        return REFLECTANCE_BANDS[band_id][0]
    return THERMAL_BANDS[band_id][0]


def get_projection_params(nc):
    projection_var = nc.variables["goes_imager_projection"]
    height = projection_var.perspective_point_height
    lon = projection_var.longitude_of_projection_origin
    req = projection_var.semi_major_axis
    rpol = projection_var.semi_minor_axis
    return height, lon, req, rpol


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
    visible = sx * (sat_height_total - sx) - sy ** 2 - (req ** 2 / rpol ** 2) * sz ** 2 >= 0
    return np.where(visible, x, np.nan), np.where(visible, y, np.nan)


def visible_boundary_angles(target_coords, sat_lon, sat_height, req, rpol, edge_samples=25):
    lon_w, lon_e, lat_s, lat_n = target_coords
    edge_lons = np.linspace(lon_w, lon_e, edge_samples)
    edge_lats = np.linspace(lat_s, lat_n, edge_samples)
    boundary_lons = np.concatenate([
        edge_lons, edge_lons, np.full(edge_samples, lon_w), np.full(edge_samples, lon_e),
    ])
    boundary_lats = np.concatenate([
        np.full(edge_samples, lat_n), np.full(edge_samples, lat_s), edge_lats, edge_lats,
    ])
    xs, ys = _lonlat_to_scan_angle(boundary_lons, boundary_lats, sat_lon, sat_height + req, req, rpol)
    valid = np.isfinite(xs) & np.isfinite(ys)
    if not valid.any():
        raise ValueError(
            f"target_coordinates {target_coords} fora do disco visível pelo satélite "
            "(o GOES-East enxerga aproximadamente de -156 a 6 de longitude). Revise [MAP] em config.ini."
        )
    return xs[valid], ys[valid]


def get_crop_slices(nc, target_coords, margin_frac=0.05, min_margin_px=6):
    sat_h, sat_lon, req, rpol = get_projection_params(nc)
    xs, ys = visible_boundary_angles(target_coords, sat_lon, sat_h, req, rpol)
    x_rad = nc.variables["x"][:]
    y_rad = nc.variables["y"][:]
    col_start_raw = int(np.searchsorted(x_rad, xs.min(), side="left"))
    col_end_raw = int(np.searchsorted(x_rad, xs.max(), side="right"))
    y_asc = y_rad[::-1]
    row_start_asc = int(np.searchsorted(y_asc, ys.min(), side="left"))
    row_end_asc = int(np.searchsorted(y_asc, ys.max(), side="right"))
    n_y = len(y_rad)
    row_start_raw = n_y - row_end_asc
    row_end_raw = n_y - row_start_asc
    margin_cols = max(min_margin_px, int((col_end_raw - col_start_raw) * margin_frac))
    margin_rows = max(min_margin_px, int((row_end_raw - row_start_raw) * margin_frac))
    col_start = max(col_start_raw - margin_cols, 0)
    col_end = min(col_end_raw + margin_cols, len(x_rad))
    row_start = max(row_start_raw - margin_rows, 0)
    row_end = min(row_end_raw + margin_rows, n_y)
    return slice(row_start, row_end), slice(col_start, col_end)


def compute_image_extent(x_rad, y_rad, sat_height):
    return (
        x_rad.min() * sat_height, x_rad.max() * sat_height,
        y_rad.min() * sat_height, y_rad.max() * sat_height,
    )


def _nearest_indices(src_coords_asc, target_coords):
    idx = np.searchsorted(src_coords_asc, target_coords)
    idx = np.clip(idx, 0, len(src_coords_asc) - 1)
    left = np.clip(idx - 1, 0, len(src_coords_asc) - 1)
    choose_left = np.abs(src_coords_asc[left] - target_coords) < np.abs(src_coords_asc[idx] - target_coords)
    return np.where(choose_left, left, idx)


def resample_to_grid(data, src_x, src_y, ref_x, ref_y):
    if data.shape == (len(ref_y), len(ref_x)) and np.array_equal(src_x, ref_x) and np.array_equal(src_y, ref_y):
        return data

    col_idx = _nearest_indices(src_x, ref_x)

    src_y_asc = src_y[::-1]
    row_idx_asc = _nearest_indices(src_y_asc, ref_y)
    row_idx = (len(src_y) - 1) - row_idx_asc

    return data[np.ix_(row_idx, col_idx)]


def pick_reference_band(band_datas):
    return min(band_datas.values(), key=lambda bd: bd["data"].size)


def align_bands_to_reference(band_datas, reference):
    aligned = {}
    for band_id, bd in band_datas.items():
        aligned[band_id] = resample_to_grid(bd["data"], bd["x_rad"], bd["y_rad"], reference["x_rad"], reference["y_rad"])
    return aligned