import logging
import geopandas as gpd
import json
import shapely

logging.basicConfig(level=logging.INFO)
from layout.block_generator import generate_solar_blocks

# Load the working area and constraints
buildable_area = gpd.read_file("outputs/shapefiles/buildable_area.shp")
buildable_area = buildable_area.to_crs(buildable_area.crs) # safety load
try:
    corridors = gpd.read_file("outputs/shapefiles/corridors.shp")
    if not corridors.empty:
        corridor_union = corridors.geometry.union_all()
        # Ensure polygons
        buildable_area_geom = buildable_area.geometry.difference(corridor_union)
        buildable_area = gpd.GeoDataFrame({"geometry": buildable_area_geom}, crs=buildable_area.crs)
        buildable_area = buildable_area.explode(index_parts=False).reset_index(drop=True)
        # Filter small
        buildable_area = buildable_area[buildable_area.geometry.area > 1000]
except Exception as e:
    print("Corridor subtract failed", e)
    
buildable_area["paddock_id"] = ["P" + str(i).zfill(3) for i in range(len(buildable_area))]
print(f"Loaded {len(buildable_area)} paddocks")

with open("config/config.yaml", "r") as f:
    import yaml
    config = yaml.safe_load(f)

# Hardcode the target capacity
config["project"]["target_ac_mw"] = 49.2
config["block"]["origin_search_step_m"] = 0

blocks, rows, _ = generate_solar_blocks(buildable_area, config, {})
print(f"Result: {len(blocks)} blocks, {len(rows)} rows")
