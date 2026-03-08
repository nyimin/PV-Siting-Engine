import geopandas as gpd
from shapely.validation import make_valid
import pandas as pd
from shapely.ops import unary_union
import warnings
warnings.filterwarnings("ignore")

rows = gpd.read_file('outputs/geojson/pv_rows.geojson')

# Load BOP
bops = []
for p in ['outputs/geojson/substation.geojson', 'outputs/geojson/bess.geojson', 'outputs/geojson/om_compound.geojson']:
    try:
        gdf = gpd.read_file(p)
        if not gdf.empty:
            bops.append(gdf)
    except Exception:
        pass

if bops:
    bop_gdf = gpd.GeoDataFrame(pd.concat(bops, ignore_index=True), crs=rows.crs)
    # The actual constraint is against the 20m buffered version
    bop_union = make_valid(bop_gdf.geometry.union_all()).buffer(20)
    
    overlaps = rows[rows.geometry.intersects(bop_union)]
    real_bop_overlaps = 0
    for _, r in overlaps.iterrows():
        intersection = make_valid(r.geometry).intersection(bop_union)
        if intersection.area > 0.1: # 0.1 sqm tolerance
            real_bop_overlaps += 1
    print(f"PV Rows overlapping BOP (+20m buffer): {real_bop_overlaps} (Total touching >=0: {len(overlaps)})")
else:
    print("No BOP files found.")

# Load roads
try:
    roads = gpd.read_file('outputs/geojson/internal_roads.geojson')
    # Roads are linestrings, they have a road_width_m or we assume 6m. Buffer them.
    road_polys = []
    for _, r in roads.iterrows():
        width = r.get('road_width_m', 6)
        road_polys.append(r.geometry.buffer(width / 2.0, cap_style="flat"))
    
    road_union = make_valid(unary_union(road_polys))
    
    overlaps = rows[rows.geometry.intersects(road_union)]
    # Some minor intersection might happen at edges due to float precision, so we check area
    real_overlaps = 0
    for _, r in overlaps.iterrows():
        intersection = make_valid(r.geometry).intersection(road_union)
        if intersection.area > 0.1: # 0.1 sqm tolerance
            real_overlaps += 1
            
    print(f"PV Rows overlapping Roads (>{0.1}sqm): {real_overlaps}")
except Exception as e:
    print(f"No roads found or error: {e}")
