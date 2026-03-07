import sys
import logging
import geopandas as gpd
from shapely.geometry import Polygon

logging.basicConfig(level=logging.DEBUG)

# Dummy config
config = {
    "solar": {
        "module_width_m": 1.303,
        "module_length_m": 2.384,
        "modules_per_string": 28,
        "strings_per_inverter": 22,
        "inverter_capacity_kw": 250,
        "orientation": "portrait",
        "tilt_deg": 26,
        "gcr": 0.38,
        "module_power_w": 635,
        "modules_per_string": 28,
        "strings_per_inverter": 22,
        "inverter_capacity_kw": 250
    },
    "block": {
        "inverters_per_block": 10,
        "min_fill_fraction": 0.60,
        "oblique_tessellation": True,
        "strings_per_table": 2
    }
}

# Create a dummy rotated polygon (like a diagonal site)
# E-W is 500m, N-S is 500m, rotated 30 deg
from shapely.affinity import rotate
base_poly = Polygon([(0,0), (500,0), (500,500), (0,500)])
test_poly = rotate(base_poly, 30, origin=(250,250))
buildable_area_gdf = gpd.GeoDataFrame([{"geometry": test_poly}], crs="EPSG:32646")

from layout.block_generator import generate_solar_blocks

blocks, rows = generate_solar_blocks(buildable_area_gdf, config)
print(f"Blocks: {len(blocks)}, Rows: {len(rows)}")
