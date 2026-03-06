import logging
import math
import geopandas as gpd
import pandas as pd
import numpy as np
from shapely.geometry import Polygon, MultiPolygon, LineString, box, mapping, Point
from shapely.affinity import rotate, translate
from shapely.ops import unary_union

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


def _sample_raster_mean(geom, raster_path):
    """Samples the mean value of a raster within a given geometry."""
    try:
        import rasterio
        from rasterio.mask import mask
        with rasterio.open(raster_path) as src:
            out_image, _ = mask(src, [geom], crop=True)
            valid = out_image[out_image != src.nodata]
            if valid.size > 0:
                return float(np.nanmean(valid))
    except Exception:
        pass
    return None


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
    site_latitude = site_wgs84.geometry.unary_union.centroid.y
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

    # ── Row orientation: fixed-tilt northern hemisphere = 0° rotation ────────────
    # For fixed-tilt, rows run E-W naturally (no rotation needed).
    # angle_deg = 0 for N hemisphere; 0 for S hemisphere too (E-W rows are optimal globally).
    # This replaces the confusing angle_deg=180 which was a no-op but hid the intent (LG-02).
    use_rotation = False
    if tracker_type != "fixed":
        # Single-axis trackers rotate to face sun — leave to future implementation
        logger.warning("Non-fixed tracking detected — row rotation not yet implemented. "
                       "Defaulting to fixed-tilt E-W layout.")

    minx, miny, maxx, maxy = buildable_area_gdf.total_bounds

    # ── Block tessellation grid dimensions ──────────────────────────────────────
    # Use a fixed physical block footprint (from config) rather than deriving from
    # string counts. This matches PVcase: blocks are physical areas (~2.5 ha each),
    # independent of how many strings are configured per row.
    #
    # block_col_width = row E-W length + 5m aisle (one column of row panels)
    # block_row_height = target physical N-S height (from footprint_ha / col_width)
    footprint_ha = block_cfg.get("footprint_ha", 2.5)
    block_col_width = phys_row_length + 5.0      # E-W width of one block column
    block_row_height = max(
        (footprint_ha * 10000) / block_col_width, # N-S height from area target
        5 * flat_pitch + 10.0                     # minimum: 5 rows + buffer
    )

    expected_rows = int(block_row_height / flat_pitch)
    logger.info(f"  Block tessellation cell: {block_col_width:.1f} m E-W × {block_row_height:.1f} m N-S "
                f"(~{expected_rows} rows @ {flat_pitch:.2f} m pitch, {footprint_ha} ha target)")

    # Phase 1: Generate tessellation grid
    grid_polys = []
    x = minx
    while x < maxx:
        y = miny
        while y < maxy:
            poly = Polygon([
                (x, y),
                (x + block_col_width, y),
                (x + block_col_width, y + block_row_height),
                (x, y + block_row_height)
            ])
            grid_polys.append(poly)
            y += block_row_height
        x += block_col_width

    grid_gdf = gpd.GeoDataFrame(geometry=grid_polys, crs=buildable_area_gdf.crs)

    # Phase 2: Intersect grid with buildable area
    logger.info("  Intersecting tessellation grid with buildable area...")
    block_candidates = gpd.overlay(grid_gdf, buildable_area_gdf, how='intersection')

    if block_candidates.empty:
        logger.warning("No buildable blocks found after tessellation.")
        return buildable_area_gdf, buildable_area_gdf

    blocks = []
    final_rows = []
    block_id_counter = 1

    # Phase 3: Fill each grid cell with E-W rows, stepping N-S by pitch
    for idx, cand in block_candidates.iterrows():
        geom = cand.geometry

        # Discard tiny fragments (< 30% of ideal block area)
        ideal_area = block_col_width * block_row_height
        if geom.area < (ideal_area * 0.3):
            continue

        # Handle MultiPolygon fragments
        if geom.geom_type == 'MultiPolygon':
            polys = list(geom.geoms)
        else:
            polys = [geom]

        for component_poly in polys:
            if component_poly.area < (ideal_area * 0.2):
                continue

            c_minx, c_miny, c_maxx, c_maxy = component_poly.bounds

            # ── Fill rows: step N-S by flat_pitch, E-W by phys_row_length ─────
            # Rows are E-W rectangles; pitch is the N-S distance between row centres.
            # y = row centre (N-S), x = row centre (E-W)
            y = c_miny + row_height_m / 2
            current_strings = 0
            block_rows = []

            while y + row_height_m / 2 <= c_maxy and current_strings < target_strings_per_block:

                # Local slope-adjusted pitch (terrain-following, optional)
                row_pitch = flat_pitch
                if terrain_paths and "slope" in terrain_paths:
                    mid_x = (c_minx + c_maxx) / 2
                    pt_slope = _sample_raster_mean(Point(mid_x, y).buffer(10), terrain_paths["slope"])
                    if pt_slope is not None and pt_slope > 0:
                        row_pitch = _compute_slope_adjusted_pitch(
                            flat_pitch, tilt_deg, pt_slope, winter_solar_elevation
                        )

                # Place row segments E-W along this N-S position
                x = c_minx + phys_row_length / 2
                while x + phys_row_length / 2 <= c_maxx and current_strings < target_strings_per_block:

                    # Row rectangle: width = phys_row_length (E-W), height = row_height_m (N-S)
                    half_len = phys_row_length / 2
                    half_h = row_height_m / 2
                    row_rect = Polygon([
                        (x - half_len, y - half_h),
                        (x + half_len, y - half_h),
                        (x + half_len, y + half_h),
                        (x - half_len, y + half_h),
                    ])
                    # Accept row if its centroid is inside the buildable poly AND
                    # at least 50% of the row footprint overlaps — this handles the
                    # case where tessellation clipping creates irregular polygon edges
                    # that cannot fully contain an axis-aligned E-W rectangle.
                    centroid_ok = component_poly.contains(row_rect.centroid)
                    if centroid_ok:
                        overlap = component_poly.intersection(row_rect)
                        if overlap.area / row_rect.area >= 0.50:
                            terrain_ok, slope_val = _check_row_terrain(row_rect, terrain_paths, config)
                            if terrain_ok:
                                strings_to_add = min(strings_per_row,
                                                     target_strings_per_block - current_strings)
                                block_rows.append({
                                    "geometry": row_rect,
                                    "strings": strings_to_add,
                                    "slope_deg": round(slope_val, 2)
                                })
                                current_strings += strings_to_add

                    x += phys_row_length + 5.0   # 5 m inter-column aisle

                y += row_pitch   # N-S step by pitch

            # ── Accept block if physical utilisation meets minimum threshold (LG-06) ──
            # Physical utilisation = rows placed / max rows geometrically possible in this cell.
            # This decouples electrical block sizing from tessellation cell geometry:
            # a 2.5ha cell holds ~25 rows; target_strings_per_block (220) is the ideal
            # electrical block size, but tessellation cells can be smaller and still valid.
            c_ns_height = c_maxy - c_miny
            max_possible_rows = max(1, int(c_ns_height / flat_pitch))
            phys_utilisation = len(block_rows) / max_possible_rows

            if len(block_rows) >= 2 and phys_utilisation >= min_fill:
                block_name = f"B{block_id_counter:03d}"

                # Capacity from first principles: actual strings placed × per-string power (LG-03)
                mod_power_w = solar_cfg["module_power_w"]
                block_dc = round(current_strings * mods_per_string * mod_power_w / 1e6, 3)
                # AC: pro-rate inverter capacity by string fraction
                strings_fraction = current_strings / target_strings_per_block
                block_ac = round(ac_per_block * min(strings_fraction, 1.0), 3)

                blocks.append({
                    "block_id": block_name,
                    "geometry": component_poly,
                    "area_ha": round(component_poly.area / 10000, 3),
                    "capacity_ac_mw": block_ac,
                    "capacity_dc_mw": block_dc,
                    "strings": current_strings,
                    "fill_pct": round(phys_utilisation * 100, 1),
                })

                for r_idx, r in enumerate(block_rows):
                    final_rows.append({
                        "block_id": block_name,
                        "row_id": r_idx,
                        "geometry": r["geometry"],
                        "strings": r["strings"],
                        "slope_deg": r["slope_deg"],
                    })
                block_id_counter += 1
            else:
                logger.debug(f"  Discarded block: {len(block_rows)} rows, phys_util={phys_utilisation*100:.1f}% < {min_fill*100:.0f}%")

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
