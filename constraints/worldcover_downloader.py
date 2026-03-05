import os
import math
import logging
import requests
import rasterio
from rasterio.merge import merge
from rasterio.mask import mask as rio_mask
from pathlib import Path

logger = logging.getLogger("PVLayoutEngine.lulc")

# ESA WorldCover v200 2021 public S3 bucket (no auth needed - requester pays disabled)
# Tiles are 3x3 degree cells, naming: ESA_WorldCover_10m_2021_v200_{ll_tile}_Map.tif
# e.g., N18E093 means lat bottom = 18°N, lon left = 93°E
WORLDCOVER_S3_BASE = "https://esa-worldcover.s3.amazonaws.com/v200/2021/map"

# WorldCover LULC class values and their suitability for solar
WORLDCOVER_CLASSES = {
    10: ("Tree cover", "excluded"),
    20: ("Shrubland", "suitable"),
    30: ("Grassland", "suitable"),
    40: ("Cropland", "suitable"),
    50: ("Built-up", "excluded"),
    60: ("Bare/sparse vegetation", "suitable"),
    70: ("Snow and Ice", "excluded"),
    80: ("Permanent water bodies", "excluded"),
    90: ("Herbaceous wetland", "excluded"),
    95: ("Mangroves", "excluded"),
    100: ("Moss and lichen", "suitable"),
}

EXCLUDED_CLASSES = {10, 50, 70, 80, 90, 95}


def _latlon_to_tile_name(lat_south, lon_west):
    """
    Converts a lat/lon lower-left corner to ESA WorldCover tile name.
    Tiles are 3-degree x 3-degree, starting at multiples of 3.
    Example: lat=18, lon=93 → 'N18E093'
    """
    ns = 'S' if lat_south < 0 else 'N'
    ew = 'W' if lon_west < 0 else 'E'
    lat_abs = abs(int(lat_south))
    lon_abs = abs(int(lon_west))
    return f"{ns}{lat_abs:02d}{ew}{lon_abs:03d}"


def _get_tile_names_for_bounds(minx, miny, maxx, maxy):
    """Returns list of WorldCover tile names covering a bounding box."""
    tile_size = 3  # degrees
    tiles = []
    lat = math.floor(miny / tile_size) * tile_size
    while lat < maxy:
        lon = math.floor(minx / tile_size) * tile_size
        while lon < maxx:
            tiles.append(_latlon_to_tile_name(lat, lon))
            lon += tile_size
        lat += tile_size
    return tiles


def _download_worldcover_tile(tile_name, output_path):
    """Downloads a single WorldCover tile from ESA S3."""
    filename = f"ESA_WorldCover_10m_2021_v200_{tile_name}_Map.tif"
    url = f"{WORLDCOVER_S3_BASE}/{filename}"
    logger.info(f"  Downloading WorldCover tile: {filename}")
    try:
        response = requests.get(url, stream=True, timeout=180)
        response.raise_for_status()
        with open(output_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=65536):
                f.write(chunk)
        logger.info(f"  Tile saved: {output_path}")
        return output_path
    except requests.exceptions.HTTPError as e:
        logger.warning(f"  HTTP error downloading {filename}: {e}")
    except Exception as e:
        logger.warning(f"  Error downloading {filename}: {e}")

    if os.path.exists(output_path):
        os.remove(output_path)
    return None


def fetch_worldcover(site_gdf, cache_dir="data/cache/worldcover", use_cache=True):
    """
    Downloads ESA WorldCover 2021 (10m) tiles covering the site from public S3.
    Merges multiple tiles if needed and clips to a buffer around the site.
    """
    logger.info("Fetching ESA WorldCover 2021 (10m)...")

    site_wgs84 = site_gdf.to_crs(epsg=4326)
    bounds = site_wgs84.total_bounds.tolist()  # [minx, miny, maxx, maxy]

    rounded = [round(b, 3) for b in bounds]
    os.makedirs(cache_dir, exist_ok=True)
    tile_cache_dir = os.path.join(cache_dir, "tiles")
    os.makedirs(tile_cache_dir, exist_ok=True)

    merged_cache = os.path.join(cache_dir, f"worldcover_{rounded[0]}_{rounded[1]}_{rounded[2]}_{rounded[3]}.tif")

    if use_cache and os.path.exists(merged_cache):
        logger.info(f"Loading WorldCover from cache: {merged_cache}")
        return merged_cache

    # Determine needed tiles
    tile_names = _get_tile_names_for_bounds(*bounds)
    logger.info(f"  Tiles required: {tile_names}")

    downloaded_tiles = []
    for tile_name in tile_names:
        tile_path = os.path.join(tile_cache_dir, f"worldcover_{tile_name}.tif")
        if use_cache and os.path.exists(tile_path):
            logger.info(f"  Using cached tile: {tile_name}")
            downloaded_tiles.append(tile_path)
            continue
        result = _download_worldcover_tile(tile_name, tile_path)
        if result:
            downloaded_tiles.append(result)

    if not downloaded_tiles:
        logger.error("No WorldCover tiles downloaded. WorldCover constraints will be skipped.")
        return None

    # Merge tiles if more than one
    logger.info("  Merging WorldCover tiles...")
    if len(downloaded_tiles) == 1:
        import shutil
        shutil.copy(downloaded_tiles[0], merged_cache)
    else:
        datasets = [rasterio.open(t) for t in downloaded_tiles]
        merged, merged_transform = merge(datasets)
        for ds in datasets:
            ds.close()

        profile = rasterio.open(downloaded_tiles[0]).profile
        profile.update({
            "height": merged.shape[1],
            "width": merged.shape[2],
            "transform": merged_transform,
        })
        with rasterio.open(merged_cache, 'w', **profile) as dst:
            dst.write(merged)

    logger.info(f"WorldCover data ready: {merged_cache}")
    return merged_cache


def worldcover_exclusion_mask(worldcover_path, site_gdf, output_path):
    """
    Creates a polygon exclusion mask from WorldCover excluded classes (forest, water, wetland, urban).
    Returns a GeoDataFrame with excluded zones.
    """
    import geopandas as gpd
    import pandas as pd
    from shapely.geometry import shape as shapely_shape
    from rasterio.features import shapes
    import numpy as np

    logger.info("Extracting WorldCover exclusion zones...")

    if worldcover_path is None or not os.path.exists(worldcover_path):
        logger.warning("No WorldCover path available. Skipping LULC exclusions.")
        return gpd.GeoDataFrame(columns=["geometry", "constraint_type"])

    site_wgs84 = site_gdf.to_crs(epsg=4326)

    with rasterio.open(worldcover_path) as src:
        # Clip to site bounds
        site_geom_list = [site_wgs84.geometry.unary_union.__geo_interface__]
        try:
            out_image, out_transform = rio_mask(src, site_geom_list, crop=True)
        except Exception as e:
            logger.warning(f"Could not clip WorldCover to site: {e}. Using full tile.")
            out_image = src.read(1, window=src.window(*site_wgs84.total_bounds))
            out_transform = src.transform
            out_image = out_image[np.newaxis, ...]

        lulc = out_image[0]
        crs = src.crs

        # Create exclusion binary mask (1 = excluded)
        excl_mask = np.isin(lulc, list(EXCLUDED_CLASSES)).astype(np.uint8)

        if excl_mask.max() == 0:
            logger.info("  No excluded LULC classes found within site.")
            return gpd.GeoDataFrame(columns=["geometry", "constraint_type"], crs=site_gdf.crs)

        # Polygonise
        results = [
            {"geometry": s, "properties": {"class": int(v) } }
            for s, v in shapes(lulc, mask=excl_mask, transform=out_transform)
        ]

        if not results:
            return gpd.GeoDataFrame(columns=["geometry", "constraint_type"], crs=site_gdf.crs)

        gdf = gpd.GeoDataFrame.from_features(results, crs=crs)
        gdf["constraint_type"] = gdf["class"].map(
            {k: f"lulc_{WORLDCOVER_CLASSES.get(k, ('unknown',))[0]}" for k in EXCLUDED_CLASSES}
        ).fillna("lulc_excluded")

        # Reproject to site CRS
        gdf = gdf.to_crs(site_gdf.crs)
        logger.info(f"  Extracted {len(gdf)} WorldCover exclusion polygons.")
        return gdf[["geometry", "constraint_type"]]
