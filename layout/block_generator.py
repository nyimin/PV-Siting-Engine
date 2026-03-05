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
    Computes the physical dimensions of a single PV block based on engineering parameters.
    Returns (flat_pitch, strings_per_row, row_width_m, phys_row_length, table_height_m, tilt_deg, total_strings).
    """
    solar = config["solar"]
    block_cfg = config["block"]

    # Module dimensions
    mod_w = solar["module_width_m"]   # 1.303 m
    mod_l = solar["module_length_m"]  # 2.384 m
    mods_per_string = solar["modules_per_string"]  # 28
    strings_per_inv = solar["strings_per_inverter"]  # 22
    invs_per_block = block_cfg["inverters_per_block"]  # 10
    orientation = solar.get("orientation", "portrait")

    # In portrait: module width is across the row, module length is up the tilt
    if orientation == "portrait":
        table_height = 2  # 2P configuration
        mods_per_row_segment = mods_per_string // table_height  # 14 modules wide
        row_width_m = mods_per_row_segment * mod_w
        table_height_m = table_height * mod_l
    else:
        table_height = 1
        mods_per_row_segment = mods_per_string
        row_width_m = mods_per_row_segment * mod_l
        table_height_m = table_height * mod_w

    tilt_deg = solar.get("tilt_deg", 26)
    winter_solar_elevation = max(10.0, 90.0 - abs(site_latitude) - 23.5)
    
    vertical_height = table_height_m * math.sin(math.radians(tilt_deg))
    gap = vertical_height / math.tan(math.radians(winter_solar_elevation))
    pitch = table_height_m * math.cos(math.radians(tilt_deg)) + gap
    row_pitch = round(max(pitch, 5.0), 2)  # ensure minimum safe pitch

    total_strings = strings_per_inv * invs_per_block
    strings_per_row = 4
    phys_row_length = strings_per_row * row_width_m
    flat_pitch = row_pitch

    return flat_pitch, strings_per_row, row_width_m, phys_row_length, table_height_m, tilt_deg, total_strings


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

def _check_row_terrain(row_geom, terrain_paths):
    """Checks if a row violates strict terrain thresholds."""
    if not terrain_paths: 
        return True, 0.0
        
    slope_val = 0.0
    if "slope" in terrain_paths:
        slope = _sample_raster_mean(row_geom, terrain_paths["slope"])
        if slope is not None:
            slope_val = slope
            if slope > 10.0:
                return False, slope_val
                
    if "curvature" in terrain_paths:
        curv = _sample_raster_mean(row_geom, terrain_paths["curvature"])
        if curv is not None and abs(curv) > 0.4:
            return False, slope_val
            
    return True, slope_val


def _enforce_azimuth_limits(angle_deg, tracker_type, config):
    """
    Snaps the azimuth to within allowable limits (e.g., ±15° from True Equator).
    """
    solar_cfg = config.get("solar", {})
    max_dev = solar_cfg.get("max_azimuth_deviation_deg", 15)
    
    norm_angle = angle_deg % 180
    ideals = [0, 90, 180]
    closest_ideal = min(ideals, key=lambda x: abs(angle_deg % 180 - x))
    
    if abs((angle_deg % 180) - closest_ideal) > max_dev:
        if (angle_deg % 180) > closest_ideal:
            return closest_ideal + max_dev
        else:
            return closest_ideal - max_dev
            
    return angle_deg


def _compute_slope_adjusted_pitch(base_pitch, tilt_deg, terrain_slope_pct, winter_elevation_deg, latitude):
    """
    Adjusts the row pitch dynamically based on the local N-S terrain slope.
    """
    if terrain_slope_pct == 0.0:
        return base_pitch
        
    slope_ang_deg = math.degrees(math.atan(terrain_slope_pct / 100.0))
    
    adjusted_pitch = base_pitch * (math.cos(math.radians(tilt_deg)) + 
                                   math.sin(math.radians(tilt_deg)) / 
                                   math.tan(math.radians(max(5.0, winter_elevation_deg - slope_ang_deg))))
    
    return max(4.0, min(15.0, adjusted_pitch))


def generate_solar_blocks(buildable_area_gdf, config, terrain_paths=None):
    """
    Generates contiguous blocks via spatial tessellation/grid, and fills them with rows.
    This explicitly removes the KMeans clustering to guarantee block continuity, prevents 
    spilling over exclusion zones, and ensures correct engineering layouts.
    """
    logger.info("Generating tessellated grid blocks and internal rows...")

    if buildable_area_gdf.empty:
        return buildable_area_gdf, buildable_area_gdf
        
    # Get Site Latitude
    site_wgs84 = buildable_area_gdf.to_crs(epsg=4326)
    site_latitude = site_wgs84.geometry.unary_union.centroid.y
    winter_solar_elevation = max(10.0, 90.0 - abs(site_latitude) - 23.5)

    block_cfg = config["block"]
    solar_cfg = config["solar"]
    tracker_type = solar_cfg.get("tracking", "fixed")

    ac_per_block = block_cfg["ac_capacity_mw"]
    dc_per_block = block_cfg["dc_capacity_mw"]

    # Compute physical parameters
    flat_pitch, strings_per_row, row_width_m, phys_row_length, table_height_m, tilt_deg, target_strings_per_block = _compute_block_dimensions(config, site_latitude)
    
    # Map projection height of the table (horizontal length)
    row_height_m = table_height_m * math.cos(math.radians(tilt_deg))

    # Determine base orientation
    angle_deg = 180 if tracker_type == "fixed" and site_latitude > 0 else 0
    angle_deg = _enforce_azimuth_limits(angle_deg, tracker_type, config)
    
    minx, miny, maxx, maxy = buildable_area_gdf.total_bounds
    
    # Define an ideal Block Grid
    # We allow the block to be 1 'phys_row_length' wide (+ 5m aisle)
    # The height is dependent on the number of rows needed vertically
    block_col_width = phys_row_length + 5.0
    rows_needed = math.ceil(target_strings_per_block / strings_per_row)
    block_row_height = rows_needed * flat_pitch + 10.0  # 10m maintenance aisle N-S
    
    logger.info(f"Target Block Dimensions: {block_col_width:.1f}m W x {block_row_height:.1f}m H")
    
    # Phase 1: Generate Master Tessellation Grid
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
            # Apply base rotation to the grid block around its center if not purely N-S
            if angle_deg % 90 != 0:
                poly = rotate(poly, angle_deg, origin='center')
            grid_polys.append(poly)
            y += block_row_height
        x += block_col_width
    
    grid_gdf = gpd.GeoDataFrame(geometry=grid_polys, crs=buildable_area_gdf.crs)
    
    # Phase 2: Intersect Grid with Buildable Area
    # This precisely bounds the blocks so they do not overlap buildable boundaries
    logger.info("Intersecting tessellation grid with buildable area...")
    block_candidates = gpd.overlay(grid_gdf, buildable_area_gdf, how='intersection')
    
    if block_candidates.empty:
        logger.warning("No buildable blocks found after tessellation.")
        return buildable_area_gdf, buildable_area_gdf

    blocks = []
    final_rows = []
    block_id_counter = 1
    
    # Phase 3: Fill Blocks with Rows
    for idx, cand in block_candidates.iterrows():
        geom = cand.geometry
        
        # Discard tiny fragments (< 30% of a full block area)
        ideal_area = block_col_width * block_row_height
        if geom.area < (ideal_area * 0.3): 
            continue
            
        # Ensure it's a Polygon, not MultiPolygon for clean block processing
        if geom.geom_type == 'MultiPolygon':
            polys = list(geom.geoms)
        else:
            polys = [geom]
            
        for component_poly in polys:
            if component_poly.area < (ideal_area * 0.2):
                continue
                
            c_minx, c_miny, c_maxx, c_maxy = component_poly.bounds
            
            y = c_miny + row_height_m/2
            current_strings = 0
            block_rows = []
            
            while y <= c_maxy and current_strings < target_strings_per_block:
                x = c_minx + phys_row_length/2
                
                row_pitch = flat_pitch
                if terrain_paths and "slope" in terrain_paths:
                    pt_slope = _sample_raster_mean(Point(x, y).buffer(10), terrain_paths["slope"])
                    if pt_slope is not None:
                         row_pitch = _compute_slope_adjusted_pitch(flat_pitch, tilt_deg, pt_slope, winter_solar_elevation, site_latitude)
                
                while x <= c_maxx and current_strings < target_strings_per_block:
                    half_len = phys_row_length / 2
                    half_h = row_height_m / 2
                    row_rect = Polygon([
                        (x - half_len, y - half_h),
                        (x + half_len, y - half_h),
                        (x + half_len, y + half_h),
                        (x - half_len, y + half_h),
                    ])
                    row_rotated = rotate(row_rect, angle_deg, origin=(x, y))
                    
                    if component_poly.contains(row_rotated):
                        terrain_ok, slope_val = _check_row_terrain(row_rotated, terrain_paths)
                        if terrain_ok:
                            strings_to_add = strings_per_row
                            # Adjust final row if it exceeds capacity
                            if current_strings + strings_to_add > target_strings_per_block:
                                strings_to_add = target_strings_per_block - current_strings
                                
                            block_rows.append({
                                "geometry": row_rotated,
                                "strings": strings_to_add,
                                "slope_pct": slope_val
                            })
                            current_strings += strings_to_add
                            
                    x += phys_row_length + 5.0
                    
                y += row_pitch
                
            if current_strings >= target_strings_per_block * 0.4:
                # Accept block if at least 40% utilized
                block_name = f"B{block_id_counter:03d}"
                capacity_factor = current_strings / target_strings_per_block
                
                blocks.append({
                    "block_id": block_name,
                    "geometry": component_poly,
                    "area_ha": component_poly.area / 10000,
                    "capacity_ac_mw": round(ac_per_block * capacity_factor, 3),
                    "capacity_dc_mw": round(dc_per_block * capacity_factor, 3),
                    "strings": current_strings
                })
                
                for r_idx, r in enumerate(block_rows):
                    final_rows.append({
                        "block_id": block_name,
                        "row_id": r_idx,
                        "geometry": r["geometry"],
                        "strings": r["strings"]
                    })
                    
                block_id_counter += 1

    if not blocks:
        logger.warning("No rows or blocks successfully generated.")
        return gpd.GeoDataFrame(columns=["block_id", "geometry"], crs=buildable_area_gdf.crs), gpd.GeoDataFrame(columns=["block_id", "geometry"], crs=buildable_area_gdf.crs)

    blocks_gdf = gpd.GeoDataFrame(blocks, crs=buildable_area_gdf.crs)
    rows_gdf = gpd.GeoDataFrame(final_rows, crs=buildable_area_gdf.crs)

    # Concept Design Integration (Phase 6) - Target Capacity Limiting
    target_ac_mw = config.get("project", {}).get("target_ac_mw")
    if target_ac_mw and target_ac_mw > 0:
        target_blocks = int(round(target_ac_mw / block_cfg["ac_capacity_mw"]))
        logger.info(f"Target capacity specified: {target_ac_mw} MWac. Limiting to exactly {target_blocks} best blocks.")
        
        # Sort blocks by capacity descending to keep the most complete blocks
        blocks_gdf = blocks_gdf.sort_values(by="strings", ascending=False)
        
        if len(blocks_gdf) > target_blocks:
            blocks_gdf = blocks_gdf.head(target_blocks).reset_index(drop=True)
            logger.info(f"  -> Retained top {target_blocks} blocks.")
        else:
            logger.warning(f"  -> Could only fit {len(blocks_gdf)} blocks, which is less than the requested {target_blocks}.")
            blocks_gdf = blocks_gdf.reset_index(drop=True)
            
        valid_block_ids = blocks_gdf["block_id"].tolist()
        rows_gdf = rows_gdf[rows_gdf["block_id"].isin(valid_block_ids)].copy()

    total_ac = blocks_gdf["capacity_ac_mw"].sum()
    total_dc = blocks_gdf["capacity_dc_mw"].sum()
    logger.info(f"Generated {len(blocks_gdf)} discrete tessellated blocks: {total_ac:.1f} MWac / {total_dc:.1f} MWdc")

    return blocks_gdf, rows_gdf
