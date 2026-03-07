import logging
import math
import geopandas as gpd
import numpy as np
from shapely.geometry import Point, LineString
from shapely.affinity import rotate, translate
from shapely.ops import unary_union, nearest_points

logger = logging.getLogger("PVLayoutEngine.bop")


def place_inverters_and_transformers(blocks_gdf, rows_gdf, config, sub_pt, corridor_info=None):
    """
    Places inverters and block transformers based on real solar block architecture.
    Strictly matches generated strings to inverters and clusters them on a "Border Pad"
    nearest to the road network (or substation if roads are unavailable).
    """
    logger.info("Placing inverters and transformers (Virtual Central block architecture)...")

    solar_cfg = config["solar"]
    block_cfg = config["block"]

    inv_capacity_kw = solar_cfg["inverter_capacity_kw"]    # 320 kW
    strings_per_inv = solar_cfg["strings_per_inverter"]    # 22
    mods_per_string = solar_cfg["modules_per_string"]      # 28
    mod_power_w = solar_cfg["module_power_w"]              # 635 W

    # Assemble possible snap targets (road corridors)
    corridor_lines = []
    if corridor_info:
        if "spine_line" in corridor_info and corridor_info["spine_line"]:
            corridor_lines.append(corridor_info["spine_line"])
        if "branch_lines" in corridor_info and corridor_info["branch_lines"]:
            corridor_lines.extend(corridor_info["branch_lines"])
            
    combined_corridors = unary_union(corridor_lines) if corridor_lines else None

    inverters = []
    transformers = []

    for idx, block_row in blocks_gdf.iterrows():
        block_id = block_row["block_id"]
        block_geom = block_row.geometry

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

        # Edge-based Transformer Placement
        # Snap directly to the external road corridors bordering the paddock/block
        pcu_pad_center = None
        if combined_corridors is not None:
            # Find the closest point on the corridor network to the block boundary
            pcu_pad_center, _ = nearest_points(block_geom, combined_corridors)
        else:
            # Fallback to the substation point
            pcu_pad_center, _ = nearest_points(block_geom, sub_pt)
        
        # Place inverters around the pad rather than a mathematical grid
        strings_remainder = total_strings
            
        for i in range(n_inverters):
            # Cluster tightly around the Central Skid
            offset_x = (i - (n_inverters / 2)) * 1.5  # 1.5m spacing between inverters on the pad
            inv_point = Point(pcu_pad_center.x + offset_x, pcu_pad_center.y - 3.0) # slightly offset from transformer
            
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

