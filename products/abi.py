import numpy as np
from netCDF4 import Dataset

from products import core
from products.core import LayerResult

VALID_MODES = ("single", "difference")
DISPLAY_GAMMA = 1 / 2.2


def read_band_raw(local_path, band_id, map_geo):
    is_reflectance = band_id in core.REFLECTANCE_BANDS
    nc = Dataset(local_path)
    try:
        sat_h, sat_lon, sat_req, sat_rpol = core.get_projection_params(nc)
        row_slice, col_slice = core.get_crop_slices(nc, map_geo["target_coordinates"])

        raw = nc.variables["CMI"][row_slice, col_slice]
        if is_reflectance:
            data = np.clip(raw, 0, 1) * 100.0
        else:
            data = raw - 273.15
        data = np.ma.filled(data.astype("float32"), np.nan)

        x_rad = np.asarray(nc.variables["x"][col_slice])
        y_rad = np.asarray(nc.variables["y"][row_slice])
        img_extent = core.compute_image_extent(x_rad, y_rad, sat_h)
    finally:
        nc.close()

    return {
        "band_id": band_id,
        "is_reflectance": is_reflectance,
        "data": data,
        "x_rad": x_rad,
        "y_rad": y_rad,
        "img_extent": img_extent,
        "sat_h": sat_h,
        "sat_lon": sat_lon,
        "sat_req": sat_req,
        "sat_rpol": sat_rpol,
    }


def render_band(nc_paths, config, colors_config, map_geo, product_spec, target_dt):
    band_id = product_spec.sources[0].band
    local_path = nc_paths[product_spec.sources[0].source_id]

    band_data = read_band_raw(local_path, band_id, map_geo)
    is_reflectance = band_data["is_reflectance"]

    if is_reflectance:
        display_data = np.clip(band_data["data"] / 100.0, 0.0, 1.0) ** DISPLAY_GAMMA
        cmap, vmin, vmax = core.reflectance_cmap(), 0.0, 1.0
        ticks = None
    else:
        display_data = band_data["data"]
        cmap, vmin, vmax = core.build_colormap_from_section(colors_config, product_spec.palette_section)
        ticks = np.arange(vmin, vmax + 1, core.THERMAL_BANDS[band_id][2])

    wavelength = (core.REFLECTANCE_BANDS.get(band_id) or core.THERMAL_BANDS[band_id])[1]
    title_fragment = f"C{band_id:02d} — {core.band_label(band_id)} ({wavelength} µm)"

    return LayerResult(
        kind="mono",
        data=display_data,
        extent=band_data["img_extent"],
        sat_h=band_data["sat_h"],
        sat_lon=band_data["sat_lon"],
        sat_req=band_data["sat_req"],
        sat_rpol=band_data["sat_rpol"],
        title_fragment=title_fragment,
        cmap=cmap,
        vmin=vmin,
        vmax=vmax,
        colorbar_ticks=ticks,
        colorbar_unit="°C",
        show_colorbar=not is_reflectance,
    )


def render_split_window(nc_paths, config, colors_config, map_geo, product_spec, target_dt):
    band_13 = read_band_raw(nc_paths["C13"], 13, map_geo)
    band_15 = read_band_raw(nc_paths["C15"], 15, map_geo)

    reference = core.pick_reference_band({13: band_13, 15: band_15})
    aligned = core.align_bands_to_reference({13: band_13, 15: band_15}, reference)

    diff = aligned[13] - aligned[15]

    cmap, vmin, vmax = core.build_colormap_from_section(colors_config, product_spec.palette_section)
    tick_step = max(1, round((vmax - vmin) / 8))
    ticks = np.arange(vmin, vmax + 1, tick_step)

    return LayerResult(
        kind="mono",
        data=diff,
        extent=reference["img_extent"],
        sat_h=reference["sat_h"],
        sat_lon=reference["sat_lon"],
        sat_req=reference["sat_req"],
        sat_rpol=reference["sat_rpol"],
        title_fragment=product_spec.label,
        cmap=cmap,
        vmin=vmin,
        vmax=vmax,
        colorbar_ticks=ticks,
        colorbar_unit="°C",
        show_colorbar=True,
    )


def _parse_band_key(raw, section, key_name):
    raw = raw.strip().upper()
    if not (raw.startswith("C") and raw[1:].isdigit()):
        raise ValueError(f"[{section}] invalid {key_name}: '{raw}' (expected format: C01-C16)")
    band_id = int(raw[1:])
    if band_id not in core.REFLECTANCE_BANDS and band_id not in core.THERMAL_BANDS:
        raise ValueError(f"[{section}] invalid {key_name}: band C{band_id:02d} does not exist (use C01-C16)")
    return band_id


def read_channel_spec(colors_config, section, channel):
    mode = colors_config.get(section, f"{channel}_mode").strip().lower()
    if mode not in VALID_MODES:
        raise ValueError(f"[{section}] {channel}_mode must be 'single' or 'difference', got '{mode}'")

    spec = {"mode": mode}
    if mode == "single":
        spec["band"] = _parse_band_key(colors_config.get(section, f"{channel}_band"), section, f"{channel}_band")
        spec["bands"] = (spec["band"],)
    else:
        spec["band_a"] = _parse_band_key(colors_config.get(section, f"{channel}_band_a"), section, f"{channel}_band_a")
        spec["band_b"] = _parse_band_key(colors_config.get(section, f"{channel}_band_b"), section, f"{channel}_band_b")
        spec["bands"] = (spec["band_a"], spec["band_b"])

    spec["min"] = colors_config.getfloat(section, f"{channel}_min")
    spec["max"] = colors_config.getfloat(section, f"{channel}_max")
    spec["gamma"] = colors_config.getfloat(section, f"{channel}_gamma")
    spec["invert"] = colors_config.getboolean(section, f"{channel}_invert")

    if spec["max"] <= spec["min"]:
        raise ValueError(f"[{section}] {channel}_max must be greater than {channel}_min")
    if spec["gamma"] <= 0:
        raise ValueError(f"[{section}] {channel}_gamma must be positive")

    return spec


def read_rgb_recipe(colors_config, section):
    return {ch: read_channel_spec(colors_config, section, ch) for ch in ("r", "g", "b")}


def apply_gamma(norm, gamma):
    return norm ** (1.0 / gamma)


def normalize_channel(data, spec):
    norm = (data - spec["min"]) / (spec["max"] - spec["min"])
    no_data = np.isnan(norm)
    norm = np.clip(np.nan_to_num(norm, nan=0.0), 0.0, 1.0)
    if spec["invert"]:
        norm = 1.0 - norm
    norm = apply_gamma(norm, spec["gamma"])
    return np.where(no_data, 0.0, norm)


def compute_channel(spec, aligned_bands):
    if spec["mode"] == "single":
        raw = aligned_bands[spec["band"]]
    else:
        raw = aligned_bands[spec["band_a"]] - aligned_bands[spec["band_b"]]
    return normalize_channel(raw, spec)


def render_rgb(nc_paths, config, colors_config, map_geo, product_spec, target_dt):
    recipe = read_rgb_recipe(colors_config, product_spec.palette_section)

    needed_bands = sorted({b for spec in recipe.values() for b in spec["bands"]})
    band_datas = {b: read_band_raw(nc_paths[f"C{b:02d}"], b, map_geo) for b in needed_bands}

    reference = core.pick_reference_band(band_datas)
    aligned = core.align_bands_to_reference(band_datas, reference)

    r = compute_channel(recipe["r"], aligned)
    g = compute_channel(recipe["g"], aligned)
    b = compute_channel(recipe["b"], aligned)
    rgb_image = np.dstack([r, g, b])

    return LayerResult(
        kind="rgb",
        data=rgb_image,
        extent=reference["img_extent"],
        sat_h=reference["sat_h"],
        sat_lon=reference["sat_lon"],
        sat_req=reference["sat_req"],
        sat_rpol=reference["sat_rpol"],
        title_fragment=product_spec.label,
        show_colorbar=False,
    )


def solar_zenith_angle_deg(lat_deg, lon_deg, dt_utc):
    day_of_year = dt_utc.timetuple().tm_yday
    hour_utc = dt_utc.hour + dt_utc.minute / 60.0 + dt_utc.second / 3600.0

    gamma = 2.0 * np.pi / 365.0 * (day_of_year - 1 + (hour_utc - 12.0) / 24.0)

    declination_rad = (
        0.006918
        - 0.399912 * np.cos(gamma) + 0.070257 * np.sin(gamma)
        - 0.006758 * np.cos(2 * gamma) + 0.000907 * np.sin(2 * gamma)
        - 0.002697 * np.cos(3 * gamma) + 0.00148 * np.sin(3 * gamma)
    )

    eq_time_min = 229.18 * (
        0.000075 + 0.001868 * np.cos(gamma) - 0.032077 * np.sin(gamma)
        - 0.014615 * np.cos(2 * gamma) - 0.040849 * np.sin(2 * gamma)
    )

    time_offset_min = eq_time_min + 4.0 * lon_deg
    true_solar_time_min = hour_utc * 60.0 + time_offset_min
    hour_angle_deg = (true_solar_time_min / 4.0) - 180.0
    hour_angle_rad = np.radians(hour_angle_deg)

    lat_rad = np.radians(lat_deg)
    cos_zenith = (
        np.sin(lat_rad) * np.sin(declination_rad)
        + np.cos(lat_rad) * np.cos(declination_rad) * np.cos(hour_angle_rad)
    )
    cos_zenith = np.clip(cos_zenith, -1.0, 1.0)
    return np.degrees(np.arccos(cos_zenith))


def read_geocolor_config(colors_config, section="RGB_GEOCOLOR"):
    day_r_band = _parse_band_key(colors_config.get(section, "day_r_band"), section, "day_r_band")
    day_b_band = _parse_band_key(colors_config.get(section, "day_b_band"), section, "day_b_band")
    weights = [float(w.strip()) for w in colors_config.get(section, "green_synth_weights").split(",")]
    if len(weights) != 3:
        raise ValueError(f"[{section}] green_synth_weights needs exactly 3 numbers (C02, C03, C01)")
    night_band = _parse_band_key(colors_config.get(section, "night_band"), section, "night_band")

    cfg = {
        "day_r_band": day_r_band,
        "day_r_min": colors_config.getfloat(section, "day_r_min"),
        "day_r_max": colors_config.getfloat(section, "day_r_max"),
        "day_b_band": day_b_band,
        "day_b_min": colors_config.getfloat(section, "day_b_min"),
        "day_b_max": colors_config.getfloat(section, "day_b_max"),
        "green_synth_weights": weights,
        "night_band": night_band,
        "night_min": colors_config.getfloat(section, "night_min"),
        "night_max": colors_config.getfloat(section, "night_max"),
        "night_gamma": colors_config.getfloat(section, "night_gamma"),
        "twilight_zenith_start": colors_config.getfloat(section, "twilight_zenith_start"),
        "twilight_zenith_end": colors_config.getfloat(section, "twilight_zenith_end"),
    }
    for prefix in ("day_r", "day_b", "night"):
        if cfg[f"{prefix}_max"] <= cfg[f"{prefix}_min"]:
            raise ValueError(f"[{section}] {prefix}_max must be greater than {prefix}_min")
    if cfg["night_gamma"] <= 0:
        raise ValueError(f"[{section}] night_gamma must be positive")
    if cfg["twilight_zenith_end"] <= cfg["twilight_zenith_start"]:
        raise ValueError(f"[{section}] twilight_zenith_end must be greater than twilight_zenith_start")
    return cfg


def render_geocolor(nc_paths, config, colors_config, map_geo, product_spec, target_dt):
    cfg = read_geocolor_config(colors_config, product_spec.palette_section)

    needed_bands = sorted({1, 2, 3, cfg["day_r_band"], cfg["day_b_band"], cfg["night_band"]})
    band_datas = {b: read_band_raw(nc_paths[f"C{b:02d}"], b, map_geo) for b in needed_bands}

    reference = core.pick_reference_band(band_datas)
    aligned = core.align_bands_to_reference(band_datas, reference)

    raw_r = np.clip((aligned[cfg["day_r_band"]] - cfg["day_r_min"]) / (cfg["day_r_max"] - cfg["day_r_min"]), 0, 1)
    raw_b = np.clip((aligned[cfg["day_b_band"]] - cfg["day_b_min"]) / (cfg["day_b_max"] - cfg["day_b_min"]), 0, 1)
    w_red, w_veggie, w_blue = cfg["green_synth_weights"]
    raw_g = np.clip(
        w_red * (aligned[2] / 100.0) + w_veggie * (aligned[3] / 100.0) + w_blue * (aligned[1] / 100.0), 0, 1
    )
    day_rgb = np.dstack([raw_r, raw_g, raw_b])
    day_rgb = np.nan_to_num(day_rgb, nan=0.0) ** DISPLAY_GAMMA

    night_norm = np.clip(
        (aligned[cfg["night_band"]] - cfg["night_min"]) / (cfg["night_max"] - cfg["night_min"]), 0, 1
    )
    night_gray = apply_gamma(np.nan_to_num(1.0 - night_norm, nan=0.0), cfg["night_gamma"])
    night_rgb = np.dstack([night_gray, night_gray, night_gray])

    lon_grid, lat_grid = core.scan_angle_to_lonlat(
        reference["x_rad"], reference["y_rad"], reference["sat_lon"],
        reference["sat_h"] + reference["sat_req"], reference["sat_req"], reference["sat_rpol"],
    )
    zenith = solar_zenith_angle_deg(lat_grid, lon_grid, target_dt)
    night_fraction = np.clip(
        (zenith - cfg["twilight_zenith_start"]) / (cfg["twilight_zenith_end"] - cfg["twilight_zenith_start"]), 0, 1
    )
    night_fraction = np.nan_to_num(night_fraction, nan=1.0)[..., np.newaxis]

    rgb_image = day_rgb * (1.0 - night_fraction) + night_rgb * night_fraction
    rgb_image = np.clip(rgb_image, 0.0, 1.0)

    return LayerResult(
        kind="rgb",
        data=rgb_image,
        extent=reference["img_extent"],
        sat_h=reference["sat_h"],
        sat_lon=reference["sat_lon"],
        sat_req=reference["sat_req"],
        sat_rpol=reference["sat_rpol"],
        title_fragment=product_spec.label,
        show_colorbar=False,
    )