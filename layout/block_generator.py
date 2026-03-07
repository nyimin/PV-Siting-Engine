import logging
import math
import geopandas as gpd
import pandas as pd
import numpy as np
from shapely.geometry import Polygon, MultiPolygon, LineString, box, mapping, Point
from shapely.affinity import rotate, translate
from shapely.ops import unary_union

from utils.raster_helpers import sample_raster_mean as _sample_raster_mean

logger = logging.getLogger("PVLayoutEngine.layout")


def _compute_block_dimensions(config, site_latitude):
    """
    Computes the physical dimensions of a single PV block using the commercial-standard
    2P portrait fixed-tilt methodology (as implemented in PVcase/Helioscope):

    PRIMARY: GCR drives row pitch — pitch = table_height / GCR.
    SECONDARY: Winter no-shade pitch is computed as a warning only, not as the driver.

    2P Portrait Configuration:
        - 2 modules stacked in the tilt direction (N-S, up-slope)
        - Modules arranged E-W for string width
        - table_height_m = 2 × module_length (tilted surface height)
        - Each string E-W width = (modules_per_string / 2) × module_width

    Returns: (flat_pitch, strings_per_row, string_ew_width, phys_row_length,
              table_height_m, row_height_m, tilt_deg, total_strings)
    """
    solar = config["solar"]
    block_cfg = config["block"]

    mod_w = solar["module_width_m"]    # 1.303 m — E-W span of one module
    mod_l = solar["module_length_m"]   # 2.384 m — along-slope span of one module
    mods_per_string = solar["modules_per_string"]   # 28
    strings_per_inv = solar["strings_per_inverter"]  # 22
    invs_per_block = block_cfg["inverters_per_block"]  # 10
    orientation = solar.get("orientation", "portrait")

    # ── Table geometry ──────────────────────────────────────────────────────────
    # 2P portrait: 2 modules in the tilt direction, string runs E-W
    if orientation == "portrait":
        n_high = 2                                         # modules stacked in tilt direction
        mods_ew_per_string = mods_per_string // n_high     # E-W modules per string (=14)
        string_ew_width = mods_ew_per_string * mod_w       # E-W width per string (=18.24 m)
        table_height_m = n_high * mod_l                    # tilted table height (=4.768 m)
    else:
        # Landscape (1P): module long side runs E-W
        n_high = 1
        mods_ew_per_string = mods_per_string
        string_ew_width = mods_ew_per_string * mod_l
        table_height_m = mod_w

    tilt_deg = solar.get("tilt_deg", 26)

    # ── PRIMARY: GCR → pitch (commercial standard) ──────────────────────────────
    gcr = solar.get("gcr", 0.38)
    flat_pitch = round(table_height_m / gcr, 2)
    logger.info(f"  Row pitch from GCR ({gcr}): {flat_pitch:.2f} m "
                f"(table depth {table_height_m:.3f} m / GCR {gcr})")

    # ── SECONDARY: winter no-shade check (warning only, does not change pitch) ──
    winter_solar_elevation = max(10.0, 90.0 - abs(site_latitude) - 23.5)
    vertical_height = table_height_m * math.sin(math.radians(tilt_deg))
    gap = vertical_height / math.tan(math.radians(winter_solar_elevation))
    no_shade_pitch = table_height_m * math.cos(math.radians(tilt_deg)) + gap
    if flat_pitch < no_shade_pitch:
        logger.warning(
            f"  GCR-derived pitch ({flat_pitch:.2f} m) < winter no-shade pitch "
            f"({no_shade_pitch:.2f} m). Inter-row shading will occur at winter "
            f"solstice. Acceptable for GCR={gcr} — shade angle limited by design."
        )
    else:
        logger.info(f"  ✓ No-shade check passed (no-shade pitch = {no_shade_pitch:.2f} m ≤ {flat_pitch:.2f} m)")

    # ── Row E-W length from strings per table (configurable) ────────────────────
    strings_per_row = block_cfg.get("strings_per_table", 4)
    phys_row_length = strings_per_row * string_ew_width   # e.g. 4 × 18.24 = 72.96 m

    # ── N-S ground projection of the table ──────────────────────────────────────
    row_height_m = table_height_m * math.cos(math.radians(tilt_deg))  # e.g. 4.285 m

    # ── Total strings per block (from inverter architecture) ────────────────────
    total_strings = strings_per_inv * invs_per_block   # e.g. 22 × 10 = 220

    logger.info(f"  Table: {table_height_m:.3f} m tilted × {phys_row_length:.2f} m E-W "
                f"({strings_per_row} strings × {string_ew_width:.2f} m)")
    logger.info(f"  Table ground depth (N-S): {row_height_m:.3f} m")
    logger.info(f"  Block: {invs_per_block} inverters × {strings_per_inv} strings = "
                f"{total_strings} strings/block, "
                f"rows needed ≈ {math.ceil(total_strings / strings_per_row)}")

    return flat_pitch, strings_per_row, string_ew_width, phys_row_length, table_height_m, row_height_m, tilt_deg, total_strings


# _sample_raster_mean imported from utils.raster_helpers


def _check_row_terrain(row_geom, terrain_paths, config=None):
    """
    Checks if a row violates terrain thresholds.
    Reads max_slope_deg from config (LG-04 fix — no longer hardcoded).
    """
    if not terrain_paths:
        return True, 0.0

    # Read threshold from config (LG-04)
    max_slope = 15.0
    if config:
        max_slope = config.get("terrain", {}).get("max_slope_deg", 15.0)

    slope_val = 0.0
    if "slope" in terrain_paths:
        slope = _sample_raster_mean(row_geom, terrain_paths["slope"])
        if slope is not None:
            slope_val = slope
            if slope > max_slope:
                return False, slope_val

    if "curvature" in terrain_paths:
        max_curv = 0.4
        if config:
            max_curv = config.get("terrain", {}).get("max_curvature", 0.4)
        curv = _sample_raster_mean(row_geom, terrain_paths["curvature"])
        if curv is not None and abs(curv) > max_curv:
            return False, slope_val

    return True, slope_val


def _compute_slope_adjusted_pitch(base_pitch, tilt_deg, terrain_slope_deg, winter_elevation_deg):
    """
    Adjusts the row pitch for local N-S terrain slope.
    Uphill rows need less pitch; downhill rows need more.
    """
    if terrain_slope_deg == 0.0:
        return base_pitch

    adjusted = base_pitch * (
        math.cos(math.radians(tilt_deg)) +
        math.sin(math.radians(tilt_deg)) /
        math.tan(math.radians(max(5.0, winter_elevation_deg - terrain_slope_deg)))
    )
    return max(4.0, min(20.0, adjusted))


def generate_solar_blocks(buildable_area_gdf, config, terrain_paths=None):
    """
    Generates PV blocks by tessellating the buildable area with a regular E-W/N-S grid,
    then filling each grid cell with 2P portrait fixed-tilt rows.

    Commercial-standard layout rules (PVcase / Helioscope):
      - Rows are oriented E-W (long axis runs along lines of latitude).
      - Rows step N-S by `row_pitch = table_height / GCR`.
      - Row E-W length = strings_per_table × (modules_per_string/2) × module_width.
      - Block DC capacity is computed bottom-up from inverter × string × module × Wp.
      - Blocks below `min_fill_fraction` of target string count are discarded.
    """
    logger.info("Generating tessellated grid blocks (2P portrait fixed-tilt, GCR-driven spacing)...")

    if buildable_area_gdf.empty:
        return buildable_area_gdf, buildable_area_gdf

    # Derive site latitude from buildable area centroid
    site_wgs84 = buildable_area_gdf.to_crs(epsg=4326)
    site_latitude = site_wgs84.geometry.union_all().centroid.y
    winter_solar_elevation = max(10.0, 90.0 - abs(site_latitude) - 23.5)
    logger.info(f"  Site latitude: {site_latitude:.2f}° — winter solar elevation: {winter_solar_elevation:.1f}°")

    solar_cfg = config["solar"]
    block_cfg = config["block"]
    tracker_type = solar_cfg.get("tracking", "fixed")

    # Compute physical parameters (GCR-driven)
    (flat_pitch, strings_per_row, string_ew_width, phys_row_length,
     table_height_m, row_height_m, tilt_deg, target_strings_per_block) = \
        _compute_block_dimensions(config, site_latitude)

    # ── DC/AC capacity from first principles (LG-03) ────────────────────────────
    mod_power_w = solar_cfg["module_power_w"]
    mods_per_string = solar_cfg["modules_per_string"]
    inv_capacity_kw = solar_cfg["inverter_capacity_kw"]
    invs_per_block = block_cfg["inverters_per_block"]
    strings_per_inv = solar_cfg["strings_per_inverter"]

    dc_per_block = (invs_per_block * strings_per_inv * mods_per_string * mod_power_w) / 1e6  # MWdc
    ac_per_block = (invs_per_block * inv_capacity_kw) / 1000  # MWac
    logger.info(f"  Block capacity (bottom-up): {dc_per_block:.3f} MWdc / {ac_per_block:.3f} MWac "
                f"(DC/AC = {dc_per_block/ac_per_block:.2f})")

    # ── Minimum fill fraction (LG-06) ───────────────────────────────────────────
    min_fill = block_cfg.get("min_fill_fraction", 0.60)

    # ── Oblique Tessellation (Task 6.2) ─────────────────────────────────────────
    oblique = block_cfg.get("oblique_tessellation", False)
    
    if tracker_type == "fixed" and oblique:
        logger.info("  Oblique tessellation disabled: Fixed-tilt systems require strict True South alignment.")
        oblique = False

    working_area = buildable_area_gdf.copy()
    centroid = working_area.geometry.union_all().centroid
    angle_deg = 0.0

    if oblique:
        # Find principal axis from Minimum Bounding Rectangle
        hull = working_area.geometry.union_all().convex_hull
        rect = hull.minimum_rotated_rectangle
        coords = list(rect.exterior.coords)
        edge_lengths = [math.hypot(coords[i+1][0] - coords[i][0], coords[i+1][1] - coords[i][1]) for i in range(4)]
        longest_edge_idx = np.argmax(edge_lengths)
        dx = coords[longest_edge_idx+1][0] - coords[longest_edge_idx][0]
        dy = coords[longest_edge_idx+1][1] - coords[longest_edge_idx][1]
        angle_rad = math.atan2(dy, dx)
        angle_deg = math.degrees(angle_rad)
        
        # Normalize angle to keep it somewhat horizontal (-45 to 45 deg if possible)
        # We want the *rows* to run parallel to the longest edge of the site.
        # Rotating by -angle_deg makes the longest edge point straight E-W.
        working_area["geometry"] = working_area.geometry.rotate(-angle_deg, origin=centroid)
        logger.info(f"  Oblique tessellation: rotated bounding box by {-angle_deg:.2f}° to align with principal axis.")

    minx, miny, maxx, maxy = working_area.total_bounds

    # ── Block tessellation strip dimensions ──────────────────────────────────────
    # We divide the site into vertical (N-S) strips of block_col_width.
    # Rows are placed E-W inside these strips.
    block_col_width = phys_row_length + 5.0      # E-W width of one block column

    logger.info(f"  Block tessellation column width: {block_col_width:.1f} m E-W. Height is variable (Task 6.1).")

    blocks = []
    final_rows = []
    block_id_counter = 1

    current_block_rows = []
    current_strings = 0

    x = minx
    while x < maxx:
        # Define the column strip
        col_poly = Polygon([
            (x, miny),
            (x + block_col_width, miny),
            (x + block_col_width, maxy),
            (x, maxy)
        ])

        # Intersect strip with working area
        col_intersection = gpd.overlay(
            gpd.GeoDataFrame(geometry=[col_poly], crs=working_area.crs),
            working_area,
            how='intersection'
        )

        if col_intersection.empty:
            x += block_col_width
            continue

        # Task 6.1: Variable block sizing - collect all valid rows in this column
        column_rows = []
        for _, cand in col_intersection.iterrows():
            geom = cand.geometry
            if geom.geom_type == 'MultiPolygon':
                polys = list(geom.geoms)
            else:
                polys = [geom]

            for poly in polys:
                c_minx, c_miny, c_maxx, c_maxy = poly.bounds
                # Start Y
                y = c_miny + row_height_m / 2

                while y + row_height_m / 2 <= c_maxy:
                    # Local slope-adjusted pitch (terrain-following, optional)
                    row_pitch = flat_pitch
                    if terrain_paths and "slope" in terrain_paths:
                        mid_x = (c_minx + c_maxx) / 2
                        pt = Point(mid_x, y)
                        if angle_deg != 0.0:
                            pt = rotate(pt, angle_deg, origin=centroid)
                        pt_slope = _sample_raster_mean(pt.buffer(10), terrain_paths["slope"])
                        if pt_slope is not None and pt_slope > 0:
                            row_pitch = _compute_slope_adjusted_pitch(
                                flat_pitch, tilt_deg, pt_slope, winter_solar_elevation
                            )

                    # Row rectangle: width = phys_row_length (E-W), height = row_height_m (N-S)
                    half_len = phys_row_length / 2
                    half_h = row_height_m / 2
                    row_center_x = x + block_col_width / 2

                    row_rect = Polygon([
                        (row_center_x - half_len, y - half_h),
                        (row_center_x + half_len, y - half_h),
                        (row_center_x + half_len, y + half_h),
                        (row_center_x - half_len, y + half_h),
                    ])

                    if poly.contains(row_rect.centroid):
                        overlap = poly.intersection(row_rect)
                        if overlap.area / row_rect.area >= 0.50:
                            # Terrain check must use the UN-ROTATED polygon
                            true_row_rect = row_rect
                            if angle_deg != 0.0:
                                true_row_rect = rotate(row_rect, angle_deg, origin=centroid)

                            terrain_ok, slope_val = _check_row_terrain(true_row_rect, terrain_paths, config)
                            if terrain_ok:
                                column_rows.append({
                                    "geometry": true_row_rect, # Store true geometry
                                    "slope_deg": round(slope_val, 2),
                                    "y": y
                                })
                    y += row_pitch

        # Sort rows in column from south to north (by Y)
        column_rows.sort(key=lambda r: r["y"])

        # Chunk rows into blocks across columns

        for r in column_rows:
            strings_to_add = min(strings_per_row, target_strings_per_block - current_strings)
            current_block_rows.append((r, strings_to_add))
            current_strings += strings_to_add

            if current_strings >= target_strings_per_block:
                fill_pct = current_strings / target_strings_per_block
                if fill_pct >= min_fill:
                    # Save block
                    block_geom = unary_union([b[0]["geometry"] for b in current_block_rows]).convex_hull
                    block_name = f"B{block_id_counter:03d}"
                    block_dc = round(current_strings * mods_per_string * mod_power_w / 1e6, 3)
                    block_ac = round(ac_per_block * (current_strings / target_strings_per_block), 3)

                    blocks.append({
                        "block_id": block_name,
                        "geometry": block_geom,
                        "area_ha": round(block_geom.area / 10000, 3),
                        "capacity_ac_mw": block_ac,
                        "capacity_dc_mw": block_dc,
                        "strings": current_strings,
                        "fill_pct": round(fill_pct * 100, 1),
                    })
                    for r_idx, (r_data, s_add) in enumerate(current_block_rows):
                        final_rows.append({
                            "block_id": block_name,
                            "row_id": r_idx,
                            "geometry": r_data["geometry"],
                            "strings": s_add,
                            "slope_deg": r_data["slope_deg"],
                        })
                    block_id_counter += 1
                current_block_rows = []
                current_strings = 0

        x += block_col_width

    # Save final remainder block after all columns are processed
    if current_block_rows:
        fill_pct = current_strings / target_strings_per_block
        if fill_pct >= min_fill:
            block_geom = unary_union([b[0]["geometry"] for b in current_block_rows]).convex_hull
            block_name = f"B{block_id_counter:03d}"
            block_dc = round(current_strings * mods_per_string * mod_power_w / 1e6, 3)
            block_ac = round(ac_per_block * (current_strings / target_strings_per_block), 3)

            blocks.append({
                "block_id": block_name,
                "geometry": block_geom,
                "area_ha": round(block_geom.area / 10000, 3),
                "capacity_ac_mw": block_ac,
                "capacity_dc_mw": block_dc,
                "strings": current_strings,
                "fill_pct": round(fill_pct * 100, 1),
            })
            for r_idx, (r_data, s_add) in enumerate(current_block_rows):
                final_rows.append({
                    "block_id": block_name,
                    "row_id": r_idx,
                    "geometry": r_data["geometry"],
                    "strings": s_add,
                    "slope_deg": r_data["slope_deg"],
                })

    if not blocks:
        logger.warning("No rows or blocks successfully generated.")
        empty = gpd.GeoDataFrame(columns=["block_id", "geometry"], crs=buildable_area_gdf.crs)
        return empty, empty

    blocks_gdf = gpd.GeoDataFrame(blocks, crs=buildable_area_gdf.crs)
    rows_gdf = gpd.GeoDataFrame(final_rows, crs=buildable_area_gdf.crs)

    # ── Target AC capacity limiting ─────────────────────────────────────────────
    target_ac_mw = config.get("project", {}).get("target_ac_mw")
    if target_ac_mw and target_ac_mw > 0:
        target_blocks = int(round(target_ac_mw / ac_per_block))
        logger.info(f"  Target: {target_ac_mw} MWac → {target_blocks} full blocks needed.")

        blocks_gdf = blocks_gdf.sort_values(by="strings", ascending=False)
        if len(blocks_gdf) > target_blocks:
            blocks_gdf = blocks_gdf.head(target_blocks).reset_index(drop=True)
            logger.info(f"  → Retained top {target_blocks} blocks (highest fill).")
        else:
            logger.warning(f"  → Only {len(blocks_gdf)} blocks available (< {target_blocks} requested).")
            blocks_gdf = blocks_gdf.reset_index(drop=True)

        valid_block_ids = blocks_gdf["block_id"].tolist()
        rows_gdf = rows_gdf[rows_gdf["block_id"].isin(valid_block_ids)].copy()

    total_ac = blocks_gdf["capacity_ac_mw"].sum()
    total_dc = blocks_gdf["capacity_dc_mw"].sum()
    total_rows = len(rows_gdf)
    logger.info(
        f"Generated {len(blocks_gdf)} blocks / {total_rows} PV rows: "
        f"{total_ac:.2f} MWac / {total_dc:.2f} MWdc "
        f"(DC/AC = {total_dc/total_ac:.2f})" if total_ac > 0 else
        f"Generated {len(blocks_gdf)} blocks / {total_rows} PV rows."
    )

    return blocks_gdf, rows_gdf
