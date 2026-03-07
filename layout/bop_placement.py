import logging
import math
import geopandas as gpd
import numpy as np
from shapely.geometry import Point, LineString
from shapely.affinity import rotate, translate

logger = logging.getLogger("PVLayoutEngine.bop")


def place_inverters_and_transformers(blocks_gdf, rows_gdf, config, sub_pt):
    """
    Places inverters and block transformers based on real solar block architecture.
    Strictly matches generated strings to inverters and clusters them on a "Border Pad"
    nearest to the substation.
    """
    logger.info("Placing inverters and transformers (block architecture)...")

    solar_cfg = config["solar"]
    block_cfg = config["block"]

    inv_capacity_kw = solar_cfg["inverter_capacity_kw"]    # 320 kW
    strings_per_inv = solar_cfg["strings_per_inverter"]    # 22
    mods_per_string = solar_cfg["modules_per_string"]      # 28
    mod_power_w = solar_cfg["module_power_w"]              # 635 W

    inverters = []
    transformers = []

    for idx, block_row in blocks_gdf.iterrows():
        block_id = block_row["block_id"]
        block_geom = block_row.geometry
        block_angle = block_row.get("angle_deg", 0)

        # Get actual generated strings from rows_gdf
        if rows_gdf is not None and not rows_gdf.empty:
            block_rows = rows_gdf[rows_gdf["block_id"] == block_id]
            total_strings = block_rows["strings"].sum()
        else:
            total_strings = 0

        if total_strings == 0:
            logger.warning(f"Block {block_id} has no generated strings. Skipping BOP placement.")
            continue

        n_inverters = math.ceil(total_strings / strings_per_inv)

        # Find the block edge closest to the Substation (as a proxy for the road access)
        # We snap to the hull boundary nearest the substation to create a "Border Pad"
        from shapely.ops import nearest_points
        
        # We want the boundary of the block, not the filled polygon, so we can snap to the edge.
        block_boundary = block_geom.boundary
        
        # nearest_points returns a tuple (point_on_boundary, point_on_sub_pt)
        # We want the point on the block boundary
        pcu_pad_center = nearest_points(block_boundary, sub_pt)[0]
        
        # Place inverters around the pad rather than a mathematical grid
        strings_remainder = total_strings
        
        if rows_gdf is not None and not rows_gdf.empty:
            block_rows = rows_gdf[rows_gdf["block_id"] == block_id]
        else:
            block_rows = gpd.GeoDataFrame()
            
        for i in range(n_inverters):
            # Cluster tightly around the Border Pad for the Virtual Central approach
            # Offset each inverter slightly for visualization purposes
            offset_x = (i - (n_inverters / 2)) * 1.5  # 1.5m spacing between inverters on the pad
            inv_point = Point(pcu_pad_center.x + offset_x, pcu_pad_center.y - 3.0) # 3m south of the transformer
            
            # Ensure it falls within block hull (fallback if centroid is weird)
            if not block_geom.contains(inv_point):
                from shapely.ops import nearest_points
                inv_point = nearest_points(block_geom, inv_point)[0]

            # Assign valid strings evenly, last inverter might have fewer
            allocated_strings = min(strings_per_inv, strings_remainder)
            strings_remainder -= allocated_strings

            inv_dc_kw = allocated_strings * mods_per_string * mod_power_w / 1000  # kW
            # Avoid placing inverters with 0 strings
            if allocated_strings > 0:
                inverters.append({
                    "inverter_id": f"{block_id}_INV{i+1:02d}",
                    "block_id": block_id,
                    "capacity_kw_ac": inv_capacity_kw,
                    "capacity_kw_dc": round(inv_dc_kw, 1),
                    "strings": allocated_strings,
                    "modules": allocated_strings * mods_per_string,
                    "geometry": inv_point,
                })

        # Place block transformer
        block_ac_mw = n_inverters * inv_capacity_kw / 1000
        transformers.append({
            "transformer_id": f"{block_id}_XFMR",
            "block_id": block_id,
            "capacity_mva": round(block_ac_mw, 2),
            "n_inverters": n_inverters,
            "geometry": pcu_pad_center,
        })

    # Create GeoDataFrames
    if inverters:
        inverters_gdf = gpd.GeoDataFrame(inverters, crs=blocks_gdf.crs)
    else:
        inverters_gdf = gpd.GeoDataFrame(
            columns=["inverter_id", "block_id", "capacity_kw_ac", "geometry"],
            crs=blocks_gdf.crs
        )

    if transformers:
        transformers_gdf = gpd.GeoDataFrame(transformers, crs=blocks_gdf.crs)
    else:
        transformers_gdf = gpd.GeoDataFrame(
            columns=["transformer_id", "block_id", "capacity_mva", "geometry"],
            crs=blocks_gdf.crs
        )

    # Summary
    total_modules = sum(inv.get("modules", 0) for inv in inverters)
    total_strings = sum(inv.get("strings", 0) for inv in inverters)
    logger.info(f"Placed {len(inverters_gdf)} inverters and {len(transformers_gdf)} transformers")
    logger.info(f"  Total modules: {total_modules:,}")
    logger.info(f"  Total strings: {total_strings:,}")
    logger.info(f"  Total DC capacity: {total_modules * mod_power_w / 1e6:.2f} MW")

    return inverters_gdf, transformers_gdf
