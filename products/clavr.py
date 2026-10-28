import numpy as np
from netCDF4 import Dataset

from products import core
from products.core import LayerResult

CLOUD_TOP_HEIGHT_VAR = "HT"
CLOUD_OPTICAL_DEPTH_VAR = "COD"
CLOUD_PHASE_VAR = "Phase"
ICE_PHASE_CODES = frozenset({4})
NO_RETRIEVAL_PHASE_CODES = frozenset({0, 5})


def _read_scalar_grid(local_path, variable_name, map_geo):
    nc = Dataset(local_path)
    try:
        if variable_name not in nc.variables:
            raise ValueError(
                f"variable '{variable_name}' not found in '{local_path}'. "
                "Check that the downloaded product is the expected one (ACHAF/CODF/ACTPF) "
                "and that NOAA did not rename the variable in a newer version of the product."
            )
        sat_h, sat_lon, sat_req, sat_rpol = core.get_projection_params(nc)
        row_slice, col_slice = core.get_crop_slices(nc, map_geo["target_coordinates"])
        raw = nc.variables[variable_name][row_slice, col_slice]
        data = np.ma.filled(raw.astype("float64"), np.nan)
        x_rad = np.asarray(nc.variables["x"][col_slice])
        y_rad = np.asarray(nc.variables["y"][row_slice])
        img_extent = core.compute_image_extent(x_rad, y_rad, sat_h)
    finally:
        nc.close()
    return {
        "data": data, "x_rad": x_rad, "y_rad": y_rad, "img_extent": img_extent,
        "sat_h": sat_h, "sat_lon": sat_lon, "sat_req": sat_req, "sat_rpol": sat_rpol,
    }


def read_estimate_config(colors_config, section="ESTIMATE_CLOUD_GEOMETRY"):
    cfg = {
        "water_coeff": colors_config.getfloat(section, "water_coeff"),
        "water_exponent": colors_config.getfloat(section, "water_exponent"),
        "ice_coeff": colors_config.getfloat(section, "ice_coeff"),
        "ice_exponent": colors_config.getfloat(section, "ice_exponent"),
        "min_thickness_m": colors_config.getfloat(section, "min_thickness_m"),
        "max_thickness_m": colors_config.getfloat(section, "max_thickness_m"),
        "base_height_floor_m": colors_config.getfloat(section, "base_height_floor_m"),
    }
    if cfg["max_thickness_m"] <= cfg["min_thickness_m"]:
        raise ValueError(f"[{section}] max_thickness_m must be greater than min_thickness_m")
    return cfg


def _estimate_top_and_thickness(nc_paths, colors_config, map_geo):
    top = _read_scalar_grid(nc_paths["ACHAF"], CLOUD_TOP_HEIGHT_VAR, map_geo)
    cod = _read_scalar_grid(nc_paths["CODF"], CLOUD_OPTICAL_DEPTH_VAR, map_geo)
    phase = _read_scalar_grid(nc_paths["ACTPF"], CLOUD_PHASE_VAR, map_geo)

    sources = {"top": top, "cod": cod, "phase": phase}
    reference = core.pick_reference_band(sources)
    aligned = core.align_bands_to_reference(sources, reference)

    est = read_estimate_config(colors_config)
    cod_safe = np.clip(aligned["cod"], 1e-3, None)
    phase_rounded = np.round(aligned["phase"])
    is_ice = np.isin(phase_rounded, list(ICE_PHASE_CODES))
    no_retrieval = np.isin(phase_rounded, list(NO_RETRIEVAL_PHASE_CODES)) | np.isnan(aligned["top"]) | np.isnan(aligned["cod"])

    thickness_water_m = est["water_coeff"] * (cod_safe ** est["water_exponent"]) * 1000.0
    thickness_ice_m = est["ice_coeff"] * (cod_safe ** est["ice_exponent"]) * 1000.0
    thickness = np.where(is_ice, thickness_ice_m, thickness_water_m)
    thickness = np.clip(thickness, est["min_thickness_m"], est["max_thickness_m"])
    thickness = np.where(no_retrieval, np.nan, thickness)

    base = aligned["top"] - thickness
    base = np.clip(base, est["base_height_floor_m"], None)
    base = np.where(no_retrieval, np.nan, base)

    return reference, thickness, base


def _mono_meters_layer(reference, data, title_fragment, colors_config, palette_section, tick_divisions):
    cmap, vmin, vmax = core.build_colormap_from_section(colors_config, palette_section)
    tick_step = max(500, round((vmax - vmin) / tick_divisions / 500) * 500)
    return LayerResult(
        kind="mono", data=data, extent=reference["img_extent"],
        sat_h=reference["sat_h"], sat_lon=reference["sat_lon"], sat_req=reference["sat_req"], sat_rpol=reference["sat_rpol"],
        title_fragment=title_fragment,
        cmap=cmap, vmin=vmin, vmax=vmax, colorbar_ticks=np.arange(vmin, vmax + 1, tick_step),
        colorbar_unit="m", show_colorbar=True,
    )


def render_cloud_top_height(nc_paths, config, colors_config, map_geo, product_spec, target_dt):
    layer = _read_scalar_grid(nc_paths["ACHAF"], CLOUD_TOP_HEIGHT_VAR, map_geo)
    return _mono_meters_layer(layer, layer["data"], "Cloud Top Height (ABI-L2-ACHAF)",
                              colors_config, product_spec.palette_section, 9)


def render_cloud_base_height(nc_paths, config, colors_config, map_geo, product_spec, target_dt):
    reference, _thickness, base = _estimate_top_and_thickness(nc_paths, colors_config, map_geo)
    return _mono_meters_layer(reference, base, "Cloud Base Height (estimate)",
                              colors_config, product_spec.palette_section, 8)


def render_cloud_thickness(nc_paths, config, colors_config, map_geo, product_spec, target_dt):
    reference, thickness, _base = _estimate_top_and_thickness(nc_paths, colors_config, map_geo)
    return _mono_meters_layer(reference, thickness, "Cloud Geometric Thickness (estimate)",
                              colors_config, product_spec.palette_section, 8)