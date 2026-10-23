import matplotlib
matplotlib.use("Agg")
matplotlib.rcParams["font.family"] = "DejaVu Sans"
import matplotlib.pyplot as plt
import matplotlib.patheffects as path_effects
import matplotlib.transforms as mtransforms
from matplotlib.font_manager import FontProperties
from matplotlib.patches import PathPatch
from matplotlib.textpath import TextPath
import cartopy.crs as ccrs
import cartopy.feature as cfeature

from utils import read_map_geometry_config, read_style_config

TITLE_FONTSIZE_PT = 16

TITLE_BAND_LINE_FACTOR = 1.5
TITLE_BAND_FRAC_MIN = 0.012
TITLE_BAND_FRAC_MAX = 0.25

def _build_map_projection(projection_type, sat_lon, sat_height, semi_major_axis, semi_minor_axis):
    globe = ccrs.Globe(semimajor_axis=semi_major_axis, semiminor_axis=semi_minor_axis, ellipse=None)
    geo_proj = ccrs.Geostationary(central_longitude=sat_lon, satellite_height=sat_height, globe=globe,
                                   sweep_axis="x")
    map_proj = ccrs.PlateCarree() if projection_type == "P" else geo_proj
    return geo_proj, map_proj

def compute_fitted_map_figsize(projection_type, sat_lon, sat_height, target_coordinates,
                                base_figsize, semi_major_axis, semi_minor_axis):
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
                     figsize, semi_major_axis, semi_minor_axis,
                     reserve_right_frac):
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
        return fig, ax, geo_proj, None

    title_band_in = TITLE_FONTSIZE_PT * TITLE_BAND_LINE_FACTOR / 72.0
    title_frac = min(max(title_band_in / fitted_h, TITLE_BAND_FRAC_MIN), TITLE_BAND_FRAC_MAX)
    total_h = fitted_h / (1.0 - title_frac)
    map_frac_h = 1.0 - title_frac

    fig = plt.figure(figsize=(total_w, total_h), facecolor="black")

    title_ax = fig.add_axes([0, map_frac_h, 1, title_frac])
    title_ax.axis("off")
    title_ax.set_facecolor("black")

    ax = fig.add_axes([0, 0, map_width_frac, map_frac_h], projection=map_proj)
    ax.set_facecolor("black")
    ax.set_extent(target_coordinates, crs=ccrs.PlateCarree())

    return fig, ax, geo_proj, title_ax

def add_geo_features(ax, style):
    if style["hide_map_lines"]:
        return
    ax.add_feature(cfeature.STATES.with_scale("10m"), linewidth=style["states_line_width"],
                    edgecolor=style["states_color"], alpha=0.7, zorder=2)
    ax.add_feature(cfeature.BORDERS.with_scale("10m"), linewidth=style["borders_line_width"],
                    edgecolor=style["borders_color"], alpha=1.0, zorder=3)
    ax.add_feature(cfeature.COASTLINE.with_scale("10m"), linewidth=style["coast_line_width"],
                    edgecolor=style["coast_color"], alpha=0.8, zorder=4)

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

def save_figure(fig, png_path, dpi, transparent=False):
    if transparent:
        fig.savefig(png_path, transparent=True, dpi=dpi)
    else:
        fig.savefig(png_path, facecolor="black", dpi=dpi)
    fig.clf()
    plt.close(fig)


def add_watermark_and_title(ax, title_ax, style, title_text):
    watermark = style["watermark"].strip()
    if watermark:
        text = ax.text(0.98, 0.02, watermark, transform=ax.transAxes, color=style["watermark_color"],
                        fontsize=style["watermark_size"], fontweight="bold", ha="right", va="bottom", alpha=0.8)
        text.set_path_effects([path_effects.withStroke(linewidth=3, foreground="black")])
    if title_ax is not None:
        add_map_title(title_ax, title_text, TITLE_FONTSIZE_PT)


COLORBAR_FRACTION = 0.03
COLORBAR_PAD = 0.04
COLORBAR_LABEL_RESERVE = 0.06
COLORBAR_VERTICAL_MARGIN_FRAC = 0.02
COLORBAR_RESERVE_RIGHT_FRAC = COLORBAR_FRACTION + COLORBAR_PAD + COLORBAR_LABEL_RESERVE


def add_colorbar_axes(fig, ax, fraction=COLORBAR_FRACTION, pad=COLORBAR_PAD):
    map_pos = ax.get_position()
    cbar_width = map_pos.width * fraction
    cbar_pad = map_pos.width * pad
    cbar_y0 = map_pos.y0 + map_pos.height * COLORBAR_VERTICAL_MARGIN_FRAC
    cbar_height = map_pos.height * (1.0 - 2 * COLORBAR_VERTICAL_MARGIN_FRAC)
    return fig.add_axes([map_pos.x1 + cbar_pad, cbar_y0, cbar_width, cbar_height])


def render_layer(product_spec, nc_paths, config, colors_config, map_geo, target_dt):
    return product_spec.renderer(nc_paths, config, colors_config, map_geo, product_spec, target_dt)


def draw_layer_on_axes(ax, geo_proj, style, layer):
    add_geo_features(ax, style)
    if layer.kind == "rgb":
        im = ax.imshow(layer.data, origin="upper", extent=layer.extent, transform=geo_proj,
                        interpolation="bicubic", zorder=1)
    else:
        im = ax.imshow(layer.data, origin="upper", cmap=layer.cmap, vmin=layer.vmin, vmax=layer.vmax,
                        extent=layer.extent, transform=geo_proj, interpolation="bicubic", zorder=1)
    return im


def generate_standalone_image(product_spec, nc_paths, sat_name, config, colors_config, png_path, target_dt):
    dpi = config.getint("PROCESSING", "dpi")
    map_geo = read_map_geometry_config(config)
    style = read_style_config(config)

    layer = render_layer(product_spec, nc_paths, config, colors_config, map_geo, target_dt)

    needs_colorbar = layer.kind == "mono" and layer.show_colorbar and not style["clean_mode"]
    reserve_right_frac = COLORBAR_RESERVE_RIGHT_FRAC if needs_colorbar else 0.0

    figsize = (style["figure_width"], style["figure_height"])
    fig, ax, geo_proj, title_ax = create_map_axes(
        map_geo["projection"], layer.sat_lon, layer.sat_h, style["clean_mode"],
        map_geo["target_coordinates"], figsize=figsize,
        semi_major_axis=layer.sat_req, semi_minor_axis=layer.sat_rpol,
        reserve_right_frac=reserve_right_frac,
    )

    try:
        im = draw_layer_on_axes(ax, geo_proj, style, layer)

        if needs_colorbar:
            cax = add_colorbar_axes(fig, ax)
            colorbar = plt.colorbar(im, cax=cax, orientation="vertical", ticks=layer.colorbar_ticks)
            unit = layer.colorbar_unit
            colorbar.ax.set_yticklabels([f"{tick:g}{unit}" for tick in layer.colorbar_ticks],
                                          color="white", size=10)
            colorbar.outline.set_edgecolor("white")

        title_text = f"{sat_name} | {layer.title_fragment} | {target_dt.strftime('%d/%m/%Y %H:%M')} UTC"
        add_watermark_and_title(ax, title_ax, style, title_text)
    except Exception:
        plt.close(fig)
        raise

    save_figure(fig, png_path, dpi)