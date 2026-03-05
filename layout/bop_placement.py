import logging
import math
import geopandas as gpd
import numpy as np
from shapely.geometry import Point, LineString
from shapely.affinity import rotate, translate

logger = logging.getLogger("PVLayoutEngine.bop")


def place_inverters_and_transformers(blocks_gdf, rows_gdf, config):
    """
    Places inverters and block transformers based on real solar block architecture.
    Strictly matches generated strings to inverters.
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

        # Place block transformer at centroid
        centroid = block_geom.centroid
        
        # Place inverters along the rows rather than a mathematical grid
        strings_remainder = total_strings
        
        if rows_gdf is not None and not rows_gdf.empty:
            block_rows = rows_gdf[rows_gdf["block_id"] == block_id]
        else:
            block_rows = pd.DataFrame()
            
        row_geoms = block_rows["geometry"].tolist() if not block_rows.empty else [centroid]
        
        for i in range(n_inverters):
            # Target row for this inverter (distribute evenly)
            row_idx = int((i / max(1, n_inverters)) * len(row_geoms))
            target_geom = row_geoms[min(row_idx, len(row_geoms)-1)]
            
            # Place at one end of the row (e.g., Eastern edge)
            minx, miny, maxx, maxy = target_geom.bounds
            inv_point = Point(maxx + 1.0, (miny + maxy)/2) # 1m off the east edge
            
            # Ensure it falls within block hull
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
            "geometry": centroid,
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

    # 3. LV Cabling: Connect String Inverters to Block Transformers
    lv_cables = []
    if transformers and inverters:
        xfmr_dict = {x["block_id"]: x["geometry"] for x in transformers}
        for inv in inverters:
            block_id = inv["block_id"]
            if block_id in xfmr_dict:
                xfmr_pt = xfmr_dict[block_id]
                inv_pt = inv["geometry"]
                line = LineString([inv_pt, xfmr_pt])
                lv_cables.append({
                    "cable_id": inv["inverter_id"] + "_cable",
                    "block_id": block_id,
                    "cable_type": "LV_AC",
                    "length_m": round(line.length, 1),
                    "geometry": line
                })
    
    if lv_cables:
        lv_cables_gdf = gpd.GeoDataFrame(lv_cables, crs=blocks_gdf.crs)
    else:
        lv_cables_gdf = gpd.GeoDataFrame(columns=["cable_id", "block_id", "cable_type", "geometry"], crs=blocks_gdf.crs)

    # Summary
    total_modules = sum(inv.get("modules", 0) for inv in inverters)
    total_strings = sum(inv.get("strings", 0) for inv in inverters)
    logger.info(f"Placed {len(inverters_gdf)} inverters and {len(transformers_gdf)} transformers")
    logger.info(f"  Total modules: {total_modules:,}")
    logger.info(f"  Total strings: {total_strings:,}")
    logger.info(f"  Total DC capacity: {total_modules * mod_power_w / 1e6:.2f} MW")
    
    total_lv_km = lv_cables_gdf["length_m"].sum() / 1000 if not lv_cables_gdf.empty else 0
    logger.info(f"  Generated {len(lv_cables_gdf)} LV AC Cables ({total_lv_km:.2f} km)")

    return inverters_gdf, transformers_gdf, lv_cables_gdf
