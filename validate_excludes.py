import geopandas as gpd

site = gpd.read_file('outputs/geojson/site_boundary.geojson').to_crs(epsg=32646)
excl = gpd.read_file('outputs/geojson/exclusions.geojson').to_crs(epsg=32646)
site_geom = site.geometry.union_all()

print(f"Total Site Area: {site_geom.area/10000:.2f} ha")

for c_type in excl['constraint_type'].unique():
    subset = excl[excl['constraint_type'] == c_type]
    subset_geom = subset.geometry.union_all()
    intersect = subset_geom.intersection(site_geom)
    area_ha = getattr(intersect, 'area', 0.0) / 10000.0
    print(f"{c_type}: {area_ha:.2f} ha")
