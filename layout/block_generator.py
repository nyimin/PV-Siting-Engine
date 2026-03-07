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
    then fills the grid with 2P portrait fixed-tilt rows. Employs 2D Spatial Clustering 
    (K-Means) to group generated rows into contiguous, commercial-scale Inverter Blocks 
    (e.g., 3.2 MWac).
    """
    logger.info("Generating tessellated PV rows and clustering into Power Blocks...")

    if buildable_area_gdf.empty:
        return buildable_area_gdf, buildable_area_gdf

    # Ensure we have a valid buildable area geometry
    try:
        union_geom = buildable_area_gdf.geometry.union_all()
    except Exception:
        from shapely.validation import make_valid
        union_geom = make_valid(buildable_area_gdf.geometry).union_all()
        
    site_centroid_utm = union_geom.centroid
    
    # Reproject just the centroid point to WGS84 for latitude
    import pyproj
    from shapely.ops import transform
    project = pyproj.Transformer.from_crs(
        buildable_area_gdf.crs, "EPSG:4326", always_xy=True
    ).transform
    site_centroid_wgs84 = transform(project, site_centroid_utm)
    site_latitude = site_centroid_wgs84.y
    winter_solar_elevation = max(10.0, 90.0 - abs(site_latitude) - 23.5)
    logger.info(f"  Site latitude: {site_latitude:.2f}° — winter solar elevation: {winter_solar_elevation:.1f}°")

    solar_cfg = config["solar"]
    block_cfg = config["block"]
    tracker_type = solar_cfg.get("tracking", "fixed")

    # Compute physical parameters (GCR-driven)
    (flat_pitch, strings_per_row, string_ew_width, phys_row_length,
     table_height_m, row_height_m, tilt_deg, target_strings_per_block) = \
        _compute_block_dimensions(config, site_latitude)

    # ── DC/AC capacity from first principles ────────────────────────────
    mod_power_w = solar_cfg["module_power_w"]
    mods_per_string = solar_cfg["modules_per_string"]
    inv_capacity_kw = solar_cfg["inverter_capacity_kw"]
    invs_per_block = block_cfg["inverters_per_block"]
    strings_per_inv = solar_cfg["strings_per_inverter"]

    dc_per_block = (invs_per_block * strings_per_inv * mods_per_string * mod_power_w) / 1e6  # MWdc
    ac_per_block = (invs_per_block * inv_capacity_kw) / 1000  # MWac
    logger.info(f"  Target Power Block: {dc_per_block:.3f} MWdc / {ac_per_block:.3f} MWac "
                f"({target_strings_per_block} strings)")

    min_fill = block_cfg.get("min_fill_fraction", 0.99)
    
    # FOR FIXED TILT, ROWS MUST FACE SOUTH. OBLIQUE TESSELLATION IS INVALID.
    oblique = False
    logger.info("  Oblique tessellation disabled: Fixed-tilt systems require strict True South alignment.")

    # ── Phase 1: Explode buildable area into distinct Paddocks ──
    # The buildable area already has road corridors subtracted, creating natural breaks.
    # Explode multi-polygons into single contiguous polygons (Paddocks).
    working_area = buildable_area_gdf.copy()
    working_area = working_area.explode(index_parts=False).reset_index(drop=True)
    working_area["paddock_id"] = [f"P{i:03d}" for i in range(len(working_area))]
    logger.info(f"  Identified {len(working_area)} distinct PV Paddocks based on road corridor boundaries.")

    minx, miny, maxx, maxy = working_area.total_bounds

    all_candidate_rows = []
    dropped_overlap = 0
    dropped_centroid = 0
    dropped_terrain = 0

    # ── Phase 2: Generate all possible rows ──
    x = minx
    
    # Read gaps from config
    inter_table_gap_m = block_cfg.get("inter_table_gap_m", 0.5)
    
    while x < maxx:
        # The East-West width of the physical table
        table_ew_width = phys_row_length
        
        col_poly = Polygon([
            (x, miny), (x + table_ew_width, miny),
            (x + table_ew_width, maxy), (x, maxy)
        ])

        col_intersection = gpd.overlay(
            gpd.GeoDataFrame(geometry=[col_poly], crs=working_area.crs),
            working_area,
            how='intersection'
        )

        if not col_intersection.empty:
            for _, cand in col_intersection.iterrows():
                geom = cand.geometry
                polys = list(geom.geoms) if geom.geom_type == 'MultiPolygon' else [geom]

                for poly in polys:
                    c_minx, c_miny, c_maxx, c_maxy = poly.bounds
                    
                    # Align the starting Y coordinate to a global grid based on the site's flat_pitch
                    # This ensures rows are globally straight across different columns/polygons
                    n_steps = math.ceil(max(0, c_miny - miny) / flat_pitch)
                    y = miny + row_height_m / 2 + n_steps * flat_pitch

                    while y + row_height_m / 2 <= c_maxy:
                        row_pitch = flat_pitch
                        
                        half_len = table_ew_width / 2
                        half_h = row_height_m / 2
                        row_center_x = x + table_ew_width / 2

                        row_rect = Polygon([
                            (row_center_x - half_len, y - half_h),
                            (row_center_x + half_len, y - half_h),
                            (row_center_x + half_len, y + half_h),
                            (row_center_x - half_len, y + half_h),
                        ])

                        if poly.contains(row_rect.centroid):
                            overlap = poly.intersection(row_rect)
                            if overlap.area / row_rect.area >= 0.99:
                                terrain_ok, slope_val = _check_row_terrain(row_rect, terrain_paths, config)
                                if terrain_ok:
                                    all_candidate_rows.append({
                                        "geometry": row_rect,
                                        "slope_deg": round(slope_val, 2),
                                        "strings": strings_per_row,
                                        "paddock_id": cand.get("paddock_id", "P000")
                                    })
                                else:
                                    dropped_terrain += 1
                            else:
                                dropped_overlap += 1
                        else:
                            dropped_centroid += 1
                        y += row_pitch
        
        # Advance X for the next column 
        x += table_ew_width + inter_table_gap_m
    
    logger.info(f"  Row gen stats: {dropped_centroid} dropped (centroid out), {dropped_overlap} dropped (overlap < 50%), {dropped_terrain} dropped (terrain constraints)")

    total_generated_strings = sum(r["strings"] for r in all_candidate_rows)
    logger.info(f"  Generated {len(all_candidate_rows)} total rows ({total_generated_strings} strings).")

    if not all_candidate_rows:
        logger.warning("No valid rows generated.")
        empty = gpd.GeoDataFrame(columns=["block_id", "geometry"], crs=buildable_area_gdf.crs)
        return empty, empty

    # ── Phase 2: Region-Growing (BFS) Adjacency Grouping ──
    n_blocks = max(1, int(round(total_generated_strings / target_strings_per_block)))
    logger.info(f"  Clustering rows into ~{n_blocks} Power Blocks (Region-Growing Adjacency)...")
    
    # First, build a spatial index for fast neighbor lookups
    from shapely.strtree import STRtree
    
    row_geoms = [r["geometry"] for r in all_candidate_rows]
    tree = STRtree(row_geoms)
    
    avg_col_width = phys_row_length + inter_table_gap_m
    # We define "neighbors" as rows within 1.5x the row_pitch or avg_col_width
    adjacency_radius = max(flat_pitch * 2.0, avg_col_width * 1.5)
    
    blocks = []
    final_rows = []
    block_id_counter = 1

    def _finalize_block(c_rows, c_strings, b_counter):
        fill_pct = c_strings / target_strings_per_block
        if fill_pct < min_fill:
            return None
            
        r_geoms = [b[0]["geometry"] for b in c_rows]
        raw_hull = unary_union(r_geoms).convex_hull
        
        # Clip the convex hull strictly to the paddock boundary
        pid = c_rows[0][0]["paddock_id"]
        paddock_geom = working_area[working_area["paddock_id"] == pid].geometry.iloc[0]
        raw_hull = raw_hull.intersection(paddock_geom)
        
        # Find the center of this block to carve an internal maintenance access road
        cx = raw_hull.centroid.x
        
        # Carve a 6m wide internal access road canyon down the middle
        miny_hull, maxy_hull = raw_hull.bounds[1], raw_hull.bounds[3]
        canyon_poly = Polygon([
            (cx - 3.0, miny_hull - 10),
            (cx + 3.0, miny_hull - 10),
            (cx + 3.0, maxy_hull + 10),
            (cx - 3.0, maxy_hull + 10)
        ])
        block_geom = raw_hull.difference(canyon_poly)
        
        block_name = f"B{b_counter:03d}"
        block_dc = round(c_strings * mods_per_string * mod_power_w / 1e6, 3)
        block_ac = round((c_strings / target_strings_per_block) * ac_per_block, 3)

        b_dict = {
            "block_id": block_name,
            "geometry": block_geom,
            "area_ha": round(block_geom.area / 10000, 3),
            "capacity_ac_mw": block_ac,
            "capacity_dc_mw": block_dc,
            "strings": c_strings,
            "fill_pct": round(fill_pct * 100, 1),
        }
        
        r_list = []
        for r_idx, (r_data, s_add) in enumerate(c_rows):
            r_list.append({
                "block_id": block_name,
                "row_id": r_idx,
                "geometry": r_data["geometry"],
                "strings": s_add,
                "slope_deg": r_data["slope_deg"],
                "paddock_id": r_data["paddock_id"]
            })
            
        return b_dict, r_list

    # ── Phase 3: Paddock-Constrained Clustering ──
    # Process each paddock independently. Rows are only clustered with other rows
    # in the VERY SAME paddock.
    unassigned_by_paddock = {}
    for i, r in enumerate(all_candidate_rows):
        pid = r["paddock_id"]
        if pid not in unassigned_by_paddock:
            unassigned_by_paddock[pid] = set()
        unassigned_by_paddock[pid].add(i)

    for pid, p_indices in unassigned_by_paddock.items():
        # Sort indices by Y, then X to pick good seed points (e.g., South-West corner)
        ordered_indices = sorted(list(p_indices), 
                               key=lambda i: (all_candidate_rows[i]["geometry"].centroid.y, 
                                              all_candidate_rows[i]["geometry"].centroid.x))

        while p_indices:
            # 1. Pick a seed
            seed_idx = next(i for i in ordered_indices if i in p_indices)
            
            # 2. Start a BFS region-growing queue
            queue = [seed_idx]
            current_block_rows = []
            current_strings = 0
            
            while queue and current_strings < target_strings_per_block:
                curr_idx = queue.pop(0)
                if curr_idx not in p_indices:
                    continue
                    
                r = all_candidate_rows[curr_idx]
                strings_to_add = min(r["strings"], target_strings_per_block - current_strings)
                
                current_block_rows.append((r, strings_to_add))
                current_strings += strings_to_add
                p_indices.remove(curr_idx)
                
                # If we need more capacity, find neighbors of this row
                if current_strings < target_strings_per_block:
                    search_area = r["geometry"].centroid.buffer(adjacency_radius)
                    neighbor_indices = tree.query(search_area)
                    
                    # Sort neighbors by distance to keep the block compact
                    curr_pt = r["geometry"].centroid
                    
                    valid_neighbors = []
                    for n_idx in neighbor_indices:
                        # MUST be in the same paddock (already true if it's in p_indices)
                        if n_idx in p_indices and n_idx not in queue:
                            n_pt = all_candidate_rows[n_idx]["geometry"].centroid
                            dist = curr_pt.distance(n_pt)
                            valid_neighbors.append((dist, n_idx))
                            
                    valid_neighbors.sort(key=lambda x: x[0])
                    for _, n_idx in valid_neighbors:
                        if n_idx not in queue:
                            queue.append(n_idx)

            # 3. Finalize the gathered block
            if current_strings >= target_strings_per_block * min_fill:
                res = _finalize_block(current_block_rows, current_strings, block_id_counter)
                if res:
                    blocks.append(res[0])
                    final_rows.extend(res[1])
                    block_id_counter += 1
            else:
                # If the block couldn't hit min_fill (e.g. stranded fragment), we just drop those rows
                pass

    blocks_gdf = gpd.GeoDataFrame(blocks, crs=buildable_area_gdf.crs)
    rows_gdf = gpd.GeoDataFrame(final_rows, crs=buildable_area_gdf.crs)

    # ── Target AC capacity limiting ──
    target_ac_mw = config.get("project", {}).get("target_ac_mw")
    if target_ac_mw and target_ac_mw > 0:
        target_blocks = int(round(target_ac_mw / ac_per_block))
        logger.info(f"  Target limit applied: {target_ac_mw} MWac → {target_blocks} full blocks needed.")

        # Score blocks by flatness and distance to site centroid (proxy for substation/POI)
        # Lower score is better
        if len(blocks_gdf) > target_blocks:
            site_centroid = buildable_area_gdf.geometry.union_all().centroid
            
            def calculate_score(row):
                block_geom = row.geometry
                dist = block_geom.centroid.distance(site_centroid)
                
                # Approximate flat average slope from constituent rows
                block_rows = rows_gdf[rows_gdf["block_id"] == row["block_id"]]
                avg_slope = block_rows["slope_deg"].mean() if not block_rows.empty else 0.0
                
                # Penalize steep slope heavily
                return dist + (avg_slope * 200) # 1 degree slope = 200m distance penalty

            blocks_gdf["selection_score"] = blocks_gdf.apply(calculate_score, axis=1)
            blocks_gdf = blocks_gdf.sort_values(by="selection_score", ascending=True)
            
            blocks_gdf = blocks_gdf.head(target_blocks).reset_index(drop=True)
            blocks_gdf = blocks_gdf.drop(columns=["selection_score"])
            logger.info(f"  → Retained top {target_blocks} blocks prioritizing flat terrain and proximity.")
        
        valid_block_ids = blocks_gdf["block_id"].tolist()
        rows_gdf = rows_gdf[rows_gdf["block_id"].isin(valid_block_ids)].copy()

    total_ac = blocks_gdf["capacity_ac_mw"].sum() if not blocks_gdf.empty else 0
    total_dc = blocks_gdf["capacity_dc_mw"].sum() if not blocks_gdf.empty else 0
    total_rows = len(rows_gdf)
    logger.info(
        f"Generated {len(blocks_gdf)} blocks / {total_rows} PV rows: "
        f"{total_ac:.2f} MWac / {total_dc:.2f} MWdc "
        f"(DC/AC = {total_dc/total_ac:.2f})" if total_ac > 0 else
        f"Generated {len(blocks_gdf)} blocks / {total_rows} PV rows."
    )

    # Maintenance aisles logic removed inside the layout block since we use road corridors now.
    aisles_info = {}

    return blocks_gdf, rows_gdf, aisles_info
