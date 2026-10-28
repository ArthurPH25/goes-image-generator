from dataclasses import dataclass
from typing import Callable, Optional, Tuple

from products import abi, clavr, core
from utils import validate_color, GLM_PRODUCT

ABI_CMIP_PRODUCT = "ABI-L2-CMIPF"
CLOUD_HEIGHT_PRODUCT = "ABI-L2-ACHAF"
CLOUD_OPTICAL_DEPTH_PRODUCT = "ABI-L2-CODF"
CLOUD_PHASE_PRODUCT = "ABI-L2-ACTPF"


@dataclass(frozen=True)
class SourceSpec:
    source_id: str
    product_code: str
    band: Optional[int] = None


@dataclass(frozen=True)
class ProductSpec:
    key: str
    kind: str
    label: str
    sources: Tuple[SourceSpec, ...]
    renderer: Optional[Callable]
    palette_section: Optional[str] = None


def _band_source(band_id):
    return SourceSpec(source_id=f"C{band_id:02d}", product_code=ABI_CMIP_PRODUCT, band=band_id)


def build_registry():
    registry = {}

    for band_id in sorted(list(core.REFLECTANCE_BANDS) + list(core.THERMAL_BANDS)):
        key = f"C{band_id:02d}"
        kind = "reflectance" if band_id in core.REFLECTANCE_BANDS else "thermal"
        registry[key] = ProductSpec(
            key=key,
            kind=kind,
            label=core.band_label(band_id),
            sources=(_band_source(band_id),),
            renderer=abi.render_band,
            palette_section=(None if kind == "reflectance" else f"PALETTE_{key}"),
        )

    registry["SPLIT_WINDOW"] = ProductSpec(
        key="SPLIT_WINDOW",
        kind="split_window",
        label="Split Window Difference (C13 - C15)",
        sources=(_band_source(13), _band_source(15)),
        renderer=abi.render_split_window,
        palette_section="PALETTE_SPLIT_WINDOW",
    )

    rgb_products = {
        "AIRMASS": ("Air Mass (Airmass RGB)", (8, 10, 12, 13)),
        "DAY_CLOUD_PHASE": ("Day Cloud Phase Distinction", (2, 5, 13)),
        "NIGHT_MICROPHYSICS": ("Night Microphysics", (7, 13, 15)),
        "SEVERE_STORMS": ("Severe Storms", (2, 5, 7, 8, 10, 13)),
        "OVERSHOOTING_TOPS": ("Overshooting Tops", (8, 12, 13)),
        "FIRE_TEMPERATURE": ("Fire Temperature", (5, 6, 7)),
    }
    for key, (label, bands) in rgb_products.items():
        registry[key] = ProductSpec(
            key=key,
            kind="rgb",
            label=label,
            sources=tuple(_band_source(b) for b in bands),
            renderer=abi.render_rgb,
            palette_section=f"RGB_{key}",
        )

    registry["GEOCOLOR"] = ProductSpec(
        key="GEOCOLOR",
        kind="geocolor",
        label="GeoColor (CIRA-like approximation)",
        sources=(_band_source(1), _band_source(2), _band_source(3), _band_source(13)),
        renderer=abi.render_geocolor,
        palette_section="RGB_GEOCOLOR",
    )

    cloud_sources = (
        SourceSpec(source_id="ACHAF", product_code=CLOUD_HEIGHT_PRODUCT),
        SourceSpec(source_id="CODF", product_code=CLOUD_OPTICAL_DEPTH_PRODUCT),
        SourceSpec(source_id="ACTPF", product_code=CLOUD_PHASE_PRODUCT),
    )
    registry["CLOUD_TOP_HEIGHT"] = ProductSpec(
        key="CLOUD_TOP_HEIGHT",
        kind="cloud",
        label="Cloud Top Height",
        sources=(cloud_sources[0],),
        renderer=clavr.render_cloud_top_height,
        palette_section="PALETTE_CLOUD_TOP_HEIGHT",
    )
    registry["CLOUD_BASE_HEIGHT"] = ProductSpec(
        key="CLOUD_BASE_HEIGHT",
        kind="cloud",
        label="Cloud Base Height (estimate)",
        sources=cloud_sources,
        renderer=clavr.render_cloud_base_height,
        palette_section="PALETTE_CLOUD_BASE_HEIGHT",
    )
    registry["CLOUD_THICKNESS"] = ProductSpec(
        key="CLOUD_THICKNESS",
        kind="cloud",
        label="Cloud Geometric Thickness (estimate)",
        sources=cloud_sources,
        renderer=clavr.render_cloud_thickness,
        palette_section="PALETTE_CLOUD_THICKNESS",
    )

    registry["GLM"] = ProductSpec(
        key="GLM",
        kind="glm",
        label="GLM (Geostationary Lightning Mapper)",
        sources=(SourceSpec(source_id="GLM", product_code=GLM_PRODUCT),),
        renderer=None,
    )

    return registry


_REGISTRY_CACHE = None


def get_registry():
    global _REGISTRY_CACHE
    if _REGISTRY_CACHE is None:
        _REGISTRY_CACHE = build_registry()
    return _REGISTRY_CACHE


def is_valid_product_key(key):
    return key.upper() in get_registry()


def get_product(key):
    key = key.upper()
    registry = get_registry()
    if key not in registry:
        raise ValueError(
            f"product '{key}' not recognized. Accepted values: {', '.join(sorted(registry))}"
        )
    return registry[key]


def palette_snapshot(product_key, colors_config):
    spec = get_product(product_key)
    sections = [spec.palette_section] if spec.palette_section else []
    if spec.kind == "cloud":
        sections.append("ESTIMATE_CLOUD_GEOMETRY")
    return {section: sorted(colors_config.items(section)) for section in sections}


def validate_palette_section(colors_config, section, min_keys=2):
    if not colors_config.has_section(section):
        raise ValueError(f"section '[{section}]' missing from colors.ini")
    items = colors_config.items(section)
    if len(items) < min_keys:
        raise ValueError(
            f"section '[{section}]' needs at least {min_keys} different keys "
            f"to interpolate colors, found {len(items)}"
        )
    for key, value in items:
        try:
            int(key)
        except ValueError:
            raise ValueError(
                f"key '{key}' in section '[{section}]' is not a valid integer "
                "(physical values must be integers, e.g. -63, not -63.5)"
            ) from None
        validate_color(f"[{section}] key {key}", value)


def _check_recipe_bands(spec, recipe_bands):
    downloaded_bands = {source.band for source in spec.sources}
    not_downloaded = sorted(recipe_bands - downloaded_bands)
    if not_downloaded:
        raise ValueError(
            f"[{spec.palette_section}] uses band(s) {', '.join(f'C{b:02d}' for b in not_downloaded)}, "
            f"which {spec.key} does not download (downloaded bands: {', '.join(f'C{b:02d}' for b in sorted(downloaded_bands))}). "
            "Use only those bands or add the new band to the product sources in products/registry.py"
        )


def validate_product_colors(product_key, colors_config):
    spec = get_product(product_key)

    if spec.kind == "reflectance":
        return
    if spec.kind in ("thermal", "split_window"):
        validate_palette_section(colors_config, spec.palette_section)
    elif spec.kind == "rgb":
        recipe = abi.read_rgb_recipe(colors_config, spec.palette_section)
        _check_recipe_bands(spec, {band for channel in recipe.values() for band in channel["bands"]})
    elif spec.kind == "geocolor":
        cfg = abi.read_geocolor_config(colors_config, spec.palette_section)
        _check_recipe_bands(spec, {1, 2, 3, cfg["day_r_band"], cfg["day_b_band"], cfg["night_band"]})
    elif spec.kind == "cloud":
        validate_palette_section(colors_config, spec.palette_section)
        clavr.read_estimate_config(colors_config)
    else:
        raise ValueError(f"product kind cannot be used as a background / validated here: '{spec.kind}'")