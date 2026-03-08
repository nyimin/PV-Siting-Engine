import geopandas as gpd
import warnings
import time
warnings.filterwarnings('ignore')

def check_area():
    try:
        b = gpd.read_file("outputs/shapefiles/buildable_area.shp")
    except Exception as e:
        print(f"Error reading buildable area: {e}")
        return

    try:
        c = gpd.read_file("outputs/shapefiles/corridors.shp")
    except Exception:
        c = None

    print(f"debug_blocks buildable area: {b.geometry.area.sum() / 10000:.2f} ha")
    
    if c is not None and not c.empty:
        start_time = time.time()
        c_union = c.geometry.union_all().simplify(tolerance=0.1)
        
        # Dissolve and simplify the buildable area difference
        b_dissolved = b.dissolve()
        if not b_dissolved.empty:
            b_geom = b_dissolved.geometry.iloc[0].simplify(tolerance=0.1)
            # STRtree under the hood in shapely accelerates difference on simplified geometries
            reduced = b_geom.difference(c_union)
            area_ha = getattr(reduced, 'area', 0.0) / 10000.0
            print(f"debug_blocks reduced area: {area_ha:.2f} ha")
            print(f"Difference operation completed in {time.time() - start_time:.2f}s")

if __name__ == "__main__":
    check_area()

