import os
import logging
import geopandas as gpd
import matplotlib
matplotlib.use('Agg')  # Non-interactive backend
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np
import rasterio
from matplotlib.colors import LinearSegmentedColormap, BoundaryNorm, ListedColormap

logger = logging.getLogger("PVLayoutEngine.visualization")


# ─────────────────────────────────────────────────────────────────────────────
# GIS layer export
# ─────────────────────────────────────────────────────────────────────────────

def save_gis_layers(output_dir, **gdfs):
    """
    Saves GeoDataFrames to GeoJSON (WGS84), Shapefile (UTM), and GeoPackage.
    GeoJSON is reprojected to WGS84 per RFC 7946 (RP-04 fix).
    """
    logger.info("Saving GIS layers...")

    geojson_dir = os.path.join(output_dir, "geojson")
    shp_dir     = os.path.join(output_dir, "shapefiles")
    gpkg_path   = os.path.join(output_dir, "layout.gpkg")

    os.makedirs(geojson_dir, exist_ok=True)
    os.makedirs(shp_dir,     exist_ok=True)

    for name, gdf in gdfs.items():
        if gdf is None or gdf.empty:
            logger.debug(f"Skipping empty layer: {name}")
            continue

        logger.info(f"  Saving {name} ({len(gdf)} features)...")

        # GeoJSON → always WGS84 (RFC 7946)
        try:
            gdf_wgs = gdf.to_crs(epsg=4326)
            gdf_wgs.to_file(os.path.join(geojson_dir, f"{name}.geojson"), driver="GeoJSON")
        except Exception as e:
            logger.warning(f"Error saving {name} GeoJSON: {e}")

        # Shapefile → UTM (original CRS)
        try:
            gdf.to_file(os.path.join(shp_dir, f"{name}.shp"))
        except Exception as e:
            logger.warning(f"Error saving {name} Shapefile: {e}")

        # GeoPackage
        try:
            gdf.to_file(gpkg_path, layer=name, driver="GPKG")
        except Exception as e:
            logger.warning(f"Error saving {name} to GeoPackage: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Layout map
# ─────────────────────────────────────────────────────────────────────────────

def create_layout_map(site_gdf, buildable_gdf, blocks_gdf, rows_gdf,
                      inverters_gdf, transformers_gdf, substation_gdf, bess_gdf,
                      roads_gdf, mv_cables_gdf, lv_cables_gdf, output_dir):
    """Creates a static overview map of the generated conceptual layout."""
    logger.info("Generating layout map...")

    fig, ax = plt.subplots(figsize=(16, 14))

    if not site_gdf.empty:
        site_gdf.plot(ax=ax, facecolor="none", edgecolor="black", linewidth=2.5, zorder=1)
    if not buildable_gdf.empty:
        buildable_gdf.plot(ax=ax, facecolor="#c8e6c9", alpha=0.4, edgecolor="#388e3c",
                           linewidth=0.8, zorder=2)
    if not blocks_gdf.empty:
        blocks_gdf.plot(ax=ax, facecolor="#bbdefb", alpha=0.5, edgecolor="#1565c0",
                        linewidth=1.0, zorder=3)
    if rows_gdf is not None and not rows_gdf.empty:
        rows_gdf.plot(ax=ax, facecolor="#1565c0", alpha=0.7, edgecolor="none", zorder=4)
    if roads_gdf is not None and not roads_gdf.empty:
        roads_gdf.plot(ax=ax, color="#9e9e9e", linewidth=1.5, linestyle="-", zorder=5)
    if lv_cables_gdf is not None and not lv_cables_gdf.empty:
        lv_cables_gdf.plot(ax=ax, color="#ffb74d", linewidth=0.8, linestyle=":", zorder=6)
    if mv_cables_gdf is not None and not mv_cables_gdf.empty:
        mv_cables_gdf.plot(ax=ax, color="#e53935", linewidth=1.2, linestyle="--", zorder=7)
    if inverters_gdf is not None and not inverters_gdf.empty:
        inverters_gdf.plot(ax=ax, color="#ff9800", marker="s", markersize=15, zorder=7)
    if transformers_gdf is not None and not transformers_gdf.empty:
        transformers_gdf.plot(ax=ax, color="#7b1fa2", marker="^", markersize=40, zorder=8)
    if substation_gdf is not None and not substation_gdf.empty:
        substation_gdf.plot(ax=ax, color="#d32f2f", marker="*", markersize=200, zorder=9)
    if bess_gdf is not None and not bess_gdf.empty:
        bess_gdf.plot(ax=ax, facecolor="#fbc02d", edgecolor="#f57f17", linewidth=1.5, zorder=10)

    try:
        import contextily as cx
        cx.add_basemap(ax, crs=site_gdf.crs, source=cx.providers.CartoDB.Positron)
    except Exception as e:
        logger.debug(f"Could not add basemap: {e}")

    legend_items = [
        mpatches.Patch(facecolor="none",    edgecolor="black",   linewidth=2, label="Site Boundary"),
        mpatches.Patch(facecolor="#c8e6c9", alpha=0.4, edgecolor="#388e3c", label="Buildable Area"),
        mpatches.Patch(facecolor="#bbdefb", alpha=0.5, edgecolor="#1565c0", label="PV Blocks"),
        mpatches.Patch(facecolor="#1565c0", alpha=0.7, label="PV Rows"),
        plt.Line2D([0], [0], color="#9e9e9e", linewidth=2, label="Access Roads"),
        plt.Line2D([0], [0], color="#ffb74d", linewidth=1, linestyle=":", label="LV AC Cables"),
        plt.Line2D([0], [0], color="#e53935", linewidth=1.5, linestyle="--", label="MV Cables"),
        plt.Line2D([0], [0], marker="s", color="w", markerfacecolor="#ff9800",  markersize=8, label="String Inverters"),
        plt.Line2D([0], [0], marker="^", color="w", markerfacecolor="#7b1fa2",  markersize=10, label="Block Transformers"),
        plt.Line2D([0], [0], marker="*", color="w", markerfacecolor="#d32f2f",  markersize=14, label="Substation"),
        mpatches.Patch(facecolor="#fbc02d", edgecolor="#f57f17", label="BESS"),
    ]
    ax.legend(handles=legend_items, loc='upper left', bbox_to_anchor=(1, 1), fontsize=9)

    ax.set_title("Conceptual Solar PV Layout", fontsize=14, fontweight="bold")
    ax.set_xlabel("Easting (m)")
    ax.set_ylabel("Northing (m)")
    ax.ticklabel_format(style='plain')

    plt.tight_layout()
    map_path = os.path.join(output_dir, "layout_map.png")
    plt.savefig(map_path, dpi=300, bbox_inches='tight')
    plt.close(fig)

    logger.info(f"Layout map saved to {map_path}")
    return map_path


# ─────────────────────────────────────────────────────────────────────────────
# Interactive map
# ─────────────────────────────────────────────────────────────────────────────

def create_interactive_map(site_gdf, buildable_gdf, blocks_gdf, rows_gdf,
                           inverters_gdf, transformers_gdf, substation_gdf, bess_gdf,
                           roads_gdf, mv_cables_gdf, lv_cables_gdf, output_dir,
                           om_gdf=None, guard_gdf=None):
    """Creates an interactive Folium map of the generated conceptual layout."""
    logger.info("Generating interactive Folium layout map...")

    try:
        import folium
    except ImportError:
        logger.error("folium not installed. Skipping interactive map.")
        return None

    if not site_gdf.empty:
        site_wgs84 = site_gdf.to_crs(epsg=4326)
        center_y = site_wgs84.geometry.unary_union.centroid.y
        center_x = site_wgs84.geometry.unary_union.centroid.x
    else:
        center_y, center_x = 0, 0

    m = folium.Map(location=[center_y, center_x], zoom_start=15, control_scale=True)

    folium.TileLayer('cartodbpositron', name="Base Map").add_to(m)
    folium.TileLayer(
        'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
        attr='Esri', name="Satellite", overlay=False
    ).add_to(m)

    def add_gdf(gdf, name, style_kwds, tooltip=None, show=True):
        if gdf is not None and not gdf.empty:
            folium.GeoJson(
                gdf.to_crs(epsg=4326), name=name,
                style_function=lambda x: style_kwds,
                tooltip=tooltip, show=show
            ).add_to(m)

    add_gdf(site_gdf,      "Site Boundary",  {'color':'black','weight':3,'fillOpacity':0})
    add_gdf(buildable_gdf, "Buildable Area", {'color':'#388e3c','weight':1,'fillColor':'#c8e6c9','fillOpacity':0.4})

    blocks_tt = folium.GeoJsonTooltip(fields=['block_id','capacity_ac_mw']) if 'block_id' in blocks_gdf.columns else None
    add_gdf(blocks_gdf, "PV Blocks", {'color':'#1565c0','weight':1.5,'fillColor':'#bbdefb','fillOpacity':0.5}, tooltip=blocks_tt)

    if rows_gdf is not None and not rows_gdf.empty:
        rows_tt = folium.GeoJsonTooltip(fields=['block_id','strings']) if 'strings' in rows_gdf.columns else None
        add_gdf(rows_gdf, "PV Rows", {'color':'#1565c0','weight':1,'fillColor':'#1565c0','fillOpacity':0.8}, tooltip=rows_tt)

    add_gdf(roads_gdf,       "Access Roads", {'color':'#9e9e9e','weight':3})
    add_gdf(lv_cables_gdf,   "LV AC Cables", {'color':'#ffb74d','weight':2,'dashArray':'5, 5'})
    add_gdf(mv_cables_gdf,   "MV Cables",    {'color':'#e53935','weight':3,'dashArray':'10, 5'})

    def add_points(gdf, name, color, radius=3, tooltip_fields=None, show=True):
        if gdf is not None and not gdf.empty:
            fg = folium.FeatureGroup(name=name, show=show)
            gdf_wgs = gdf.to_crs(epsg=4326)
            for idx, row in gdf_wgs.iterrows():
                geom = row.geometry
                pt = [geom.y, geom.x]
                tt = None
                if tooltip_fields:
                    tt = "<br>".join([f"{f}: {row[f]}" for f in tooltip_fields if f in row])
                folium.CircleMarker(location=pt, radius=radius, color=color,
                                    fill=True, fill_color=color, fill_opacity=1.0,
                                    tooltip=tt).add_to(fg)
            fg.add_to(m)

    add_points(inverters_gdf,    "String Inverters", "#ff9800", radius=3, tooltip_fields=['inverter_id','capacity_kw_ac'])
    add_points(transformers_gdf, "Transformers",     "#7b1fa2", radius=5, tooltip_fields=['transformer_id'])

    sub_tt  = folium.GeoJsonTooltip(fields=['compound_id','type','area_m2']) if substation_gdf is not None and 'compound_id' in substation_gdf.columns else None
    bess_tt = folium.GeoJsonTooltip(fields=['bess_id','capacity_mw','capacity_mwh']) if bess_gdf is not None and 'bess_id' in bess_gdf.columns else None
    om_tt   = folium.GeoJsonTooltip(fields=['compound_id','type','total_area_m2']) if om_gdf is not None and not om_gdf.empty and 'compound_id' in om_gdf.columns else None
    guard_tt = folium.GeoJsonTooltip(fields=['compound_id','type']) if guard_gdf is not None and not guard_gdf.empty and 'compound_id' in guard_gdf.columns else None

    add_gdf(substation_gdf, "Substation Compound", {'color':'#b71c1c','weight':2.5,'fillColor':'#ef9a9a','fillOpacity':0.7}, tooltip=sub_tt)
    add_gdf(bess_gdf,       "BESS Compound",        {'color':'#f57f17','weight':2,'fillColor':'#fbc02d','fillOpacity':0.8},   tooltip=bess_tt)
    add_gdf(om_gdf,         "O&M Facility",          {'color':'#1b5e20','weight':2,'fillColor':'#a5d6a7','fillOpacity':0.8},   tooltip=om_tt)
    add_gdf(guard_gdf,      "Guard House",           {'color':'#37474f','weight':1.5,'fillColor':'#78909c','fillOpacity':0.9}, tooltip=guard_tt)

    folium.LayerControl().add_to(m)
    map_path = os.path.join(output_dir, "layout_map.html")
    m.save(map_path)
    logger.info(f"Interactive map saved to {map_path}")
    return map_path


# ─────────────────────────────────────────────────────────────────────────────
# Terrain maps  (Phase 1 + 2 required outputs)
# ─────────────────────────────────────────────────────────────────────────────

def create_terrain_maps(terrain_paths, site_gdf, output_dir, exclusions_gdf=None):
    """
    Generates all Phase 1 + 2 terrain maps.

    Phase 1 outputs (renamed per spec):
        terrain_slope_map.png
        terrain_aspect_map.png           ← NEW
        terrain_ruggedness_map.png       ← NEW
        terrain_constraints_map.png      ← renamed from constraint_map.png

    Phase 2 outputs:
        terrain_suitability_map.png      ← renamed from suitability_map.png
        terrain_slope_suitability_map.png ← NEW
        terrain_aspect_suitability_map.png ← NEW
    """
    logger.info("Generating terrain maps (Phase 1 & 2)...")

    slope_path       = terrain_paths.get("slope")
    aspect_path      = terrain_paths.get("aspect")
    tri_path         = terrain_paths.get("tri")
    tpi_path         = terrain_paths.get("tpi")
    hillshade_path   = terrain_paths.get("hillshade")
    suitability_path = terrain_paths.get("suitability")
    slope_suit_path  = terrain_paths.get("slope_suitability")
    aspect_suit_path = terrain_paths.get("aspect_suitability")

    # ── Phase 1: Core terrain maps ────────────────────────────────────────────

    if slope_path and os.path.exists(slope_path):
        _plot_raster(
            slope_path,
            os.path.join(output_dir, "terrain_slope_map.png"),
            title="Slope Analysis (degrees)",
            cmap="RdYlGn_r",
            vmin=0, vmax=20,
            label="Slope (°)",
            overlay_boundary=site_gdf
        )

    if aspect_path and os.path.exists(aspect_path):
        _plot_aspect_map(
            aspect_path,
            slope_path,
            os.path.join(output_dir, "terrain_aspect_map.png"),
            site_gdf=site_gdf
        )

    if tri_path and os.path.exists(tri_path):
        _plot_raster(
            tri_path,
            os.path.join(output_dir, "terrain_ruggedness_map.png"),
            title="Terrain Ruggedness Index — TRI (m)",
            cmap="OrRd",
            vmin=0, vmax=5,
            label="TRI (m)",
            overlay_boundary=site_gdf
        )

    if tpi_path and os.path.exists(tpi_path):
        _plot_raster(
            tpi_path,
            os.path.join(output_dir, "terrain_valley_tpi_map.png"),
            title="Topographic Position Index (TPI) - Valley Detection",
            cmap="RdBu", # Red = valley, Blue = ridge
            vmin=-5, vmax=5,
            label="TPI (m)",
            overlay_boundary=site_gdf
        )

    if hillshade_path and os.path.exists(hillshade_path):
        _plot_raster(
            hillshade_path,
            os.path.join(output_dir, "terrain_hillshade_map.png"),
            title="Terrain Hillshade",
            cmap="gray",
            vmin=0, vmax=255,
            label="Hillshade"
        )

    # ── Phase 1: Constraints map ───────────────────────────────────────────────
    if exclusions_gdf is not None and not exclusions_gdf.empty:
        _create_constraint_map(site_gdf, exclusions_gdf, output_dir)

    # ── Phase 2: Suitability maps ──────────────────────────────────────────────

    if slope_suit_path and os.path.exists(slope_suit_path):
        _plot_suitability_0_3(
            slope_suit_path,
            os.path.join(output_dir, "terrain_slope_suitability_map.png"),
            title="Slope Suitability Score (0–3)"
        )

    if aspect_suit_path and os.path.exists(aspect_suit_path):
        _plot_suitability_0_3(
            aspect_suit_path,
            os.path.join(output_dir, "terrain_aspect_suitability_map.png"),
            title="Aspect Suitability Score (0–3)"
        )

    if suitability_path and os.path.exists(suitability_path):
        _plot_suitability_0_3(
            suitability_path,
            os.path.join(output_dir, "terrain_suitability_map.png"),
            title="Combined Terrain Suitability Index (0–3)",
            overlay_boundary=site_gdf,
            show_threshold=True
        )


def _plot_aspect_map(aspect_path, slope_path, output_path, site_gdf=None):
    """
    Renders aspect as an 8-direction classified map with N-facing penalty zones
    highlighted, colour-coded by suitability class.
    """
    try:
        with rasterio.open(aspect_path) as src:
            aspect = src.read(1).astype(np.float32)
            transform = src.transform

        # Classify into 4 suitability zones (0-3 colour map)
        # 0=N-facing (red), 1=E/W (yellow), 2=SE/SW (light green), 3=S/flat (green)
        classified = np.zeros_like(aspect, dtype=np.uint8)
        # Score 3: S — 135–225
        classified[(aspect >= 135) & (aspect < 225)] = 3
        # Score 2: SE — 112.5–135 or SW — 225–247.5
        classified[((aspect >= 112.5) & (aspect < 135)) | ((aspect >= 225) & (aspect < 247.5))] = 2
        # Score 1: E — 67.5–112.5 or W — 247.5–292.5
        classified[((aspect >= 67.5)  & (aspect < 112.5)) | ((aspect >= 247.5) & (aspect < 292.5))] = 1
        # Score 0: N — already 0

        cmap = ListedColormap(["#e53935", "#ffb300", "#aed581", "#2e7d32"])
        norm = BoundaryNorm([0, 1, 2, 3, 4], cmap.N)

        extent = [transform[2],
                  transform[2] + transform[0] * aspect.shape[1],
                  transform[5] + transform[4] * aspect.shape[0],
                  transform[5]]

        fig, ax = plt.subplots(figsize=(12, 10))
        im = ax.imshow(classified, cmap=cmap, norm=norm, extent=extent, aspect='equal')

        # Site boundary overlay
        if site_gdf is not None and not site_gdf.empty:
            site_gdf.plot(ax=ax, facecolor="none", edgecolor="black", linewidth=2)

        cbar = plt.colorbar(im, ax=ax, shrink=0.7, ticks=[0.5, 1.5, 2.5, 3.5])
        cbar.ax.set_yticklabels(["0 — N-facing (Poor)",
                                 "1 — E/W (Moderate)",
                                 "2 — SE/SW (Good)",
                                 "3 — S-facing (Best)"])
        cbar.set_label("Aspect Suitability Class")

        ax.set_title("Aspect Suitability Classification\n(Fixed-tilt PV, Northern Hemisphere)",
                     fontsize=14, fontweight="bold")
        ax.set_xlabel("Easting (m)")
        ax.set_ylabel("Northing (m)")
        ax.ticklabel_format(style='plain')

        plt.tight_layout()
        plt.savefig(output_path, dpi=200, bbox_inches='tight')
        plt.close(fig)
        logger.info(f"  Saved: {output_path}")
    except Exception as e:
        logger.warning(f"  Failed to generate aspect map: {e}")


def _plot_suitability_0_3(raster_path, output_path, title,
                           overlay_boundary=None, show_threshold=False):
    """Renders a 0–3 suitability index raster with standard colour scheme."""
    try:
        with rasterio.open(raster_path) as src:
            data = src.read(1).astype(np.float32)
            transform = src.transform

        # 4-class colormap matching the SOP
        cmap = LinearSegmentedColormap.from_list(
            "suit", ["#e53935", "#ffb300", "#aed581", "#2e7d32"], N=256
        )

        extent = [transform[2],
                  transform[2] + transform[0] * data.shape[1],
                  transform[5] + transform[4] * data.shape[0],
                  transform[5]]

        fig, ax = plt.subplots(figsize=(12, 10))
        im = ax.imshow(data, cmap=cmap, vmin=0, vmax=3, extent=extent, aspect='equal')

        if show_threshold:
            ax.contour(data, levels=[2.25], colors='white', linewidths=1.5,
                       extent=extent, origin='upper')
            ax.text(0.02, 0.02, "White contour = buildable threshold (index ≥ 2.25)",
                    transform=ax.transAxes, fontsize=8, color='white',
                    bbox=dict(boxstyle='round,pad=0.3', facecolor='black', alpha=0.5))

        if overlay_boundary is not None and not overlay_boundary.empty:
            overlay_boundary.plot(ax=ax, facecolor="none", edgecolor="black", linewidth=2)

        cbar = plt.colorbar(im, ax=ax, shrink=0.7)
        cbar.set_label("Suitability Index (0–3)")

        # Add score class labels
        for y, label in zip([0.1, 0.43, 0.77, 1.0],
                            ["0–1: Unsuitable / N-facing", "1–2: Marginal (E/W, steep)",
                             "2–2.25: Good", "2.25–3: Best (buildable)"]):
            cbar.ax.text(2.5, y, label, fontsize=6, va='center', transform=cbar.ax.transAxes)

        ax.set_title(title, fontsize=14, fontweight="bold")
        ax.set_xlabel("Easting (m)")
        ax.set_ylabel("Northing (m)")
        ax.ticklabel_format(style='plain')

        plt.tight_layout()
        plt.savefig(output_path, dpi=200, bbox_inches='tight')
        plt.close(fig)
        logger.info(f"  Saved: {output_path}")
    except Exception as e:
        logger.warning(f"  Failed to generate {title}: {e}")


def _create_constraint_map(site_gdf, exclusions_gdf, output_dir):
    """Creates a terrain_constraints_map.png showing exclusion zones by type."""
    try:
        fig, ax = plt.subplots(figsize=(14, 12))
        site_gdf.plot(ax=ax, facecolor="#e8f5e9", edgecolor="black", linewidth=2, zorder=1)

        constraint_colors = {
            "osm_buildings":              "#e53935",
            "osm_water":                  "#1e88e5",
            "osm_roads":                  "#ff9800",
            "osm_railways":               "#6d4c41",
            "osm_power":                  "#9c27b0",
            "terrain_steep_slope":        "#f44336",
            "terrain_north_facing":       "#ff6f00",   # amber — new
            "terrain_roughness_tri":      "#8d6e63",
            "terrain_curvature":          "#546e7a",   # new
            "forest_edge_buffer":         "#1b5e20",   # new
            "lulc_Tree cover":            "#2e7d32",
            "lulc_Built-up":              "#757575",
            "lulc_Permanent water bodies":"#0277bd",
            "lulc_Herbaceous wetland":    "#00897b",
            "lulc_Snow and Ice":          "#90a4ae",
            "lulc_Mangroves":             "#1b5e20",
            "site_setback":               "#cfd8dc",
        }
        default_color = "#b0bec5"

        if "constraint_type" in exclusions_gdf.columns:
            for ctype in exclusions_gdf["constraint_type"].unique():
                subset = exclusions_gdf[exclusions_gdf["constraint_type"] == ctype]
                color  = constraint_colors.get(ctype, default_color)
                label  = (ctype.replace("osm_", "OSM: ")
                               .replace("terrain_", "Terrain: ")
                               .replace("lulc_", "LULC: ")
                               .replace("_", " ").title())
                subset.plot(ax=ax, facecolor=color, alpha=0.7, edgecolor="none",
                            label=label, zorder=2)
        else:
            exclusions_gdf.plot(ax=ax, facecolor="#e53935", alpha=0.5, label="Excluded", zorder=2)

        ax.set_title("Terrain Constraint Map — Exclusion Zones", fontsize=14, fontweight="bold")
        ax.set_xlabel("Easting (m)")
        ax.set_ylabel("Northing (m)")
        ax.ticklabel_format(style='plain')

        handles, labels = ax.get_legend_handles_labels()
        if handles:
            ax.legend(handles, labels, loc='upper left', bbox_to_anchor=(1, 1), fontsize=9)

        plt.tight_layout()
        constraint_path = os.path.join(output_dir, "terrain_constraints_map.png")
        plt.savefig(constraint_path, dpi=200, bbox_inches='tight')
        plt.close(fig)
        logger.info(f"  Saved: {constraint_path}")
    except Exception as e:
        logger.warning(f"  Failed to generate constraint map: {e}")


def _plot_raster(raster_path, output_path, title, cmap, vmin, vmax, label,
                 overlay_boundary=None):
    """Generic helper to plot a single raster band."""
    try:
        with rasterio.open(raster_path) as src:
            data = src.read(1).astype(np.float32)
            transform = src.transform

        extent = [transform[2],
                  transform[2] + transform[0] * data.shape[1],
                  transform[5] + transform[4] * data.shape[0],
                  transform[5]]

        fig, ax = plt.subplots(figsize=(12, 10))
        im = ax.imshow(data, cmap=cmap, vmin=vmin, vmax=vmax,
                       extent=extent, aspect='equal')

        if overlay_boundary is not None and not overlay_boundary.empty:
            overlay_boundary.plot(ax=ax, facecolor="none", edgecolor="black", linewidth=2)

        plt.colorbar(im, ax=ax, shrink=0.7, label=label)
        ax.set_title(title, fontsize=14, fontweight="bold")
        ax.set_xlabel("Easting (m)")
        ax.set_ylabel("Northing (m)")
        ax.ticklabel_format(style='plain')

        plt.tight_layout()
        plt.savefig(output_path, dpi=200, bbox_inches='tight')
        plt.close(fig)
        logger.info(f"  Saved: {output_path}")
    except Exception as e:
        logger.warning(f"  Failed to generate {title}: {e}")
