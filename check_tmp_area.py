import geopandas as gpd
import warnings
warnings.filterwarnings('ignore')

b = gpd.read_file("outputs/shapefiles/buildable_area.shp")
c = gpd.read_file("outputs/shapefiles/corridors.shp") if "corridors" in "outputs/shapefiles" else None
try:
    c = gpd.read_file("outputs/shapefiles/corridors.shp")
except:
    c = None

print(f"debug_blocks buildable area: {b.geometry.area.sum() / 10000:.2f} ha")
if c is not None and not c.empty:
    c_union = c.geometry.union_all()
    reduced = b.geometry.difference(c_union)
    print(f"debug_blocks reduced area: {reduced.area.sum() / 10000:.2f} ha")
