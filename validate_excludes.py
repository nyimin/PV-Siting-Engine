import geopandas as gpd
import time

def validate_exclusions():
    site = gpd.read_file('outputs/geojson/site_boundary.geojson').to_crs(epsg=32646)
    excl = gpd.read_file('outputs/geojson/exclusions.geojson').to_crs(epsg=32646)
    
    start_time = time.time()
    # Simplify geometry for faster processing
    site_geom = site.geometry.union_all().simplify(tolerance=0.1)
    print(f"Total Site Area: {site_geom.area/10000:.2f} ha")

    # Vectorized dissolve
    excl_dissolved = excl.dissolve(by='constraint_type')
    
    # Use Spatial Index to speed up intersection checks
    sindex = excl_dissolved.sindex
    possible_matches_index = list(sindex.intersection(site_geom.bounds))
    possible_matches = excl_dissolved.iloc[possible_matches_index]

    for c_type, row in possible_matches.iterrows():
        # Simplify constraint geom as well
        subset_geom = row.geometry.simplify(tolerance=0.1)
        intersect = subset_geom.intersection(site_geom)
        area_ha = getattr(intersect, 'area', 0.0) / 10000.0
        print(f"{c_type}: {area_ha:.2f} ha")
        
    print(f"Validation completed in {time.time() - start_time:.2f}s")

if __name__ == "__main__":
    validate_exclusions()

