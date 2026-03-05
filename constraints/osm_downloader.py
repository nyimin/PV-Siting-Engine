import os
import osmnx as ox
import geopandas as gpd
import logging

from utils.caching import get_cache_key

logger = logging.getLogger("PVLayoutEngine.osm")

def fetch_osm_constraints(site_gdf, cache_dir="data/cache/osm", use_cache=True):
    """
    Fetches OpenStreetMap constraints (roads, buildings, railways, power lines, water)
    for a given site boundary.
    """
    logger.info("Fetching OpenStreetMap constraints...")
    
    # Reproject to WGS84 for OSMnx if needed
    site_wgs84 = site_gdf.to_crs(epsg=4326)
    
    # Get bounding box or polygon for query
    polygon = site_wgs84.geometry.iloc[0]
    
    # Generate cache key based on polygon bounds
    bounds = polygon.bounds
    cache_key = get_cache_key("osm_constraints", minx=bounds[0], miny=bounds[1], maxx=bounds[2], maxy=bounds[3])
    cache_path = os.path.join(cache_dir, f"{cache_key}.gpkg")

    if use_cache and os.path.exists(cache_path):
        logger.info(f"Loading OSM constraints from cache: {cache_path}")
        try:
            constraints: dict[str, gpd.GeoDataFrame] = {}
            for layer in ["buildings", "water", "roads", "railways", "power"]:
                try:
                    constraints[layer] = gpd.read_file(cache_path, layer=layer)
                except Exception as e:
                    logger.debug(f"Layer {layer} not found in cache or empty: {e}")
                    constraints[layer] = gpd.GeoDataFrame() # empty
            return constraints
        except Exception as e:
            logger.warning(f"Failed to load cache, re-fetching. Error: {e}")

    # If no cache or cache failed, fetch using OSMnx
    # Configure OSMnx to use memory/cache
    ox.settings.cache_folder = os.path.join(cache_dir, "osmnx_cache")
    ox.settings.use_cache = True
    
    tags_dict = {
        "buildings": {"building": True},
        "water": {"natural": ["water", "wetland"], "water": True, "waterway": ["river", "stream", "canal"]},
        "roads": {"highway": ["motorway", "trunk", "primary", "secondary", "tertiary", "unclassified", "residential"]},
        "railways": {"railway": ["rail", "light_rail", "subway", "tram"]},
        "power": {"power": ["line", "minor_line", "substation", "plant"]}
    }

    results = {}
    
    for layer_name, tags in tags_dict.items():
        logger.info(f"  Fetching OSM layer: {layer_name}")
        try:
            # fetch geometries
            gdf = ox.features_from_polygon(polygon, tags)
            if not gdf.empty:
                # keep only relevant columns to save space, at least geometry
                cols_to_keep = ['geometry']
                # keep specific columns if they exist
                for col in ['name', 'highway', 'building', 'water', 'waterway', 'railway', 'power']:
                    if col in gdf.columns:
                        cols_to_keep.append(col)
                gdf = gdf[cols_to_keep]
                results[layer_name] = gdf
            else:
                results[layer_name] = gpd.GeoDataFrame(geometry=[])
        except Exception as e:
            logger.warning(f"  Error fetching {layer_name}: {e}")
            results[layer_name] = gpd.GeoDataFrame(geometry=[])

    # Save to custom cache geopackage with layers
    if use_cache:
        os.makedirs(cache_dir, exist_ok=True)
        logger.info(f"Saving newly fetched geometries to cache: {cache_path}")
        for layer_name, gdf in results.items():
            if not gdf.empty:
                 # some geometries might be points/lines/polygons mixed, gpkg handles this but can complain if mixed types.
                 # force all to WKT or keep as is. Usually GeoPandas handles it if we convert lists/dicts to strings
                 for col in gdf.columns:
                     if gdf[col].apply(type).eq(list).any() or gdf[col].apply(type).eq(dict).any():
                         gdf[col] = gdf[col].astype(str)
                 try:
                     gdf.to_file(cache_path, layer=layer_name, driver="GPKG")
                 except Exception as e:
                     logger.warning(f"Failed to save layer {layer_name} to cache: {e}")

    return results

