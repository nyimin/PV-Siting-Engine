import os
import hashlib
import json
import logging
from pathlib import Path

logger = logging.getLogger("PVLayoutEngine.caching")


def get_cache_key(prefix, **kwargs):
    """
    Generates a stable cache key based on dataset type and spatial parameters.
    Cache key = prefix + md5(sorted parameters).
    """
    param_str = json.dumps(kwargs, sort_keys=True)
    hash_obj = hashlib.md5(param_str.encode('utf-8'))
    return f"{prefix}_{hash_obj.hexdigest()[:12]}"


def check_cache(cache_dir, cache_key, ext=".gpkg"):
    """Checks if a cached file exists. Returns path if found, None otherwise."""
    filepath = Path(cache_dir) / f"{cache_key}{ext}"
    if filepath.exists():
        logger.info(f"  Cache HIT: {cache_key}")
        return str(filepath)
    logger.debug(f"  Cache MISS: {cache_key}")
    return None


def save_to_cache(gdf, cache_dir, cache_key, ext=".gpkg"):
    """Saves a GeoDataFrame to the cache directory."""
    os.makedirs(cache_dir, exist_ok=True)
    filepath = Path(cache_dir) / f"{cache_key}{ext}"

    if ext == ".gpkg":
        gdf.to_file(filepath, driver="GPKG")
    elif ext == ".geojson":
        gdf.to_file(filepath, driver="GeoJSON")
    elif ext == ".shp":
        gdf.to_file(filepath)
    else:
        raise ValueError(f"Unsupported cache extension: {ext}")

    logger.info(f"  Cached: {filepath}")
    return str(filepath)


def get_cache_summary(cache_dir="data/cache"):
    """
    Returns a summary of cached datasets for logging/debugging.
    """
    summary = {}
    cache_path = Path(cache_dir)
    if not cache_path.exists():
        return summary

    for subdir in ["dem", "worldcover", "osm"]:
        subpath = cache_path / subdir
        if subpath.exists():
            files = list(subpath.rglob("*"))
            files = [f for f in files if f.is_file()]
            total_size = sum(f.stat().st_size for f in files)
            summary[subdir] = {
                "files": len(files),
                "size_mb": round(total_size / 1e6, 2),
            }

    return summary


def log_cache_status(cache_dir="data/cache"):
    """Logs the current cache status."""
    summary = get_cache_summary(cache_dir)
    if not summary:
        logger.info("  Cache: empty")
        return

    logger.info("  Cache status:")
    for dataset, info in summary.items():
        logger.info(f"    {dataset}: {info['files']} files, {info['size_mb']} MB")
