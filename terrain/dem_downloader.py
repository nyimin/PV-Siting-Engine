import os
import time
import requests
import rasterio
from rasterio.transform import from_bounds
from pathlib import Path
import logging
import numpy as np
from dotenv import load_dotenv

load_dotenv()

from utils.caching import get_cache_key

logger = logging.getLogger("PVLayoutEngine.dem")

# DEM types available on OpenTopography, ordered from highest to lowest quality
OPENTOPO_DEM_TYPES = [
    ("COP30",   30,  "Copernicus DEM GLO-30 (30m)"),
    ("SRTMGL1", 30,  "SRTM GL1 (30m)"),
    ("AW3D30",  30,  "ALOS World 3D 30m"),
    ("SRTMGL3", 90,  "SRTM GL3 (90m)"),
]


def _validate_dem_resolution(dem_path, config):
    """
    Validates the downloaded DEM resolution against config thresholds.
    Returns (resolution_m, warnings_list).
    """
    warnings = []
    with rasterio.open(dem_path) as src:
        # Estimate resolution in meters
        if src.crs and src.crs.is_geographic:
            # Approximate: 1 degree ~ 111,320 m at equator, adjust for latitude
            center_lat = (src.bounds.top + src.bounds.bottom) / 2.0
            lat_factor = np.cos(np.radians(abs(center_lat)))
            res_x_m = abs(src.transform.a) * 111320 * lat_factor
            res_y_m = abs(src.transform.e) * 111320
        else:
            res_x_m = abs(src.transform.a)
            res_y_m = abs(src.transform.e)

        resolution_m = (res_x_m + res_y_m) / 2.0

        # Check coverage (no-data)
        data = src.read(1)
        nodata = src.nodata
        if nodata is not None:
            nodata_pct = np.sum(data == nodata) / data.size * 100
            if nodata_pct > 5:
                warnings.append(f"DEM has {nodata_pct:.1f}% NoData pixels — coverage may be incomplete")
        
        # Change config thresholds to match engineering standards
        if "dem" not in config:
            config["dem"] = {}
        
        # Determine engineering grade
        if resolution_m > 20:
            msg = (f"DEM resolution ({resolution_m:.1f}m) is poor (>20m). "
                   f"Layout placement precision is downgraded and terrain analysis uncertainty is high.")
            warnings.append(msg)
            config["dem"]["resolution_status"] = "poor"
        elif resolution_m > 10:
            msg = f"DEM resolution ({resolution_m:.1f}m) is acceptable (<=20m)."
            warnings.append(msg)
            config["dem"]["resolution_status"] = "acceptable"
        else:
            msg = f"DEM resolution ({resolution_m:.1f}m) is preferred (<=10m)."
            warnings.append(msg)
            config["dem"]["resolution_status"] = "preferred"

        logger.info(f"  DEM resolution: ~{resolution_m:.1f} m/pixel")
        logger.info(f"  DEM size: {src.width}×{src.height} pixels")
        logger.info(f"  DEM CRS: {src.crs}")
        logger.info(f"  DEM bounds: {src.bounds}")

    return resolution_m, warnings


def download_opentopography_dem(demtype, bounds, output_path, api_key, retries=3, retry_delay=5):
    """
    Downloads a DEM from OpenTopography for a given demtype and bounding box.
    Implements retry logic for robustness.
    """
    minx, miny, maxx, maxy = bounds
    api_key_param = f"&API_Key={api_key}" if api_key else ""
    url = (
        f"https://portal.opentopography.org/API/globaldem"
        f"?demtype={demtype}&west={minx}&south={miny}&east={maxx}&north={maxy}"
        f"&outputFormat=GTiff{api_key_param}"
    )

    for attempt in range(1, retries + 1):
        logger.info(f"  Attempt {attempt}/{retries}: Downloading {demtype} from OpenTopography...")
        try:
            response = requests.get(url, stream=True, timeout=180)
            response.raise_for_status()

            # Verify we got a valid GeoTIFF (not an error XML/JSON response)
            content_type = response.headers.get("Content-Type", "")
            if "image/tiff" not in content_type and "application/octet-stream" not in content_type:
                body = response.content[:500].decode("utf-8", errors="replace")
                logger.warning(f"  {demtype} response not a GeoTIFF (Content-Type={content_type}): {body}")
                if attempt < retries:
                    logger.info(f"  Retrying in {retry_delay}s...")
                    time.sleep(retry_delay)
                    continue
                return None

            with open(output_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)

            # Validity check
            with rasterio.open(output_path) as src:
                if src.count < 1 or src.width < 2 or src.height < 2:
                    logger.warning(f"  {demtype} downloaded but appears empty/invalid.")
                    os.remove(output_path)
                    if attempt < retries:
                        logger.info(f"  Retrying in {retry_delay}s...")
                        time.sleep(retry_delay)
                        continue
                    return None

            logger.info(f"  {demtype} DEM downloaded successfully to: {output_path}")
            return output_path

        except requests.exceptions.HTTPError as e:
            logger.warning(f"  {demtype} HTTP error (attempt {attempt}/{retries}): {e}")
        except requests.exceptions.ConnectionError as e:
            logger.warning(f"  {demtype} connection error (attempt {attempt}/{retries}): {e}")
        except requests.exceptions.Timeout as e:
            logger.warning(f"  {demtype} timeout (attempt {attempt}/{retries}): {e}")
        except Exception as e:
            logger.warning(f"  {demtype} download error (attempt {attempt}/{retries}): {e}")

        # Clean up partial file
        if os.path.exists(output_path):
            os.remove(output_path)

        if attempt < retries:
            logger.info(f"  Waiting {retry_delay}s before retry...")
            time.sleep(retry_delay)

    return None


def fetch_dem(site_gdf, cache_dir="data/cache/dem", use_cache=True, config=None):
    """
    Fetches the best-resolution DEM available for the site from OpenTopography.
    Tries DEM types in order (COP30 → SRTMGL1 → AW3D30 → SRTMGL3).
    Implements retry logic and validates resolution against config thresholds.
    """
    logger.info("Fetching DEM data (best available resolution)...")

    if config is None:
        config = {}

    api_key = os.getenv("OPENTOPOGRAPHY_API_KEY", "")
    if not api_key:
        logger.warning("OPENTOPOGRAPHY_API_KEY not set in .env — API may reject requests.")
    else:
        logger.info("OpenTopography API key loaded from .env.")

    site_wgs84 = site_gdf.to_crs(epsg=4326)
    buffered_site = site_wgs84.buffer(0.01)
    bounds = list(buffered_site.total_bounds)  # [minx, miny, maxx, maxy]

    os.makedirs(cache_dir, exist_ok=True)
    rounded = [round(b, 3) for b in bounds]

    # Get retry settings from config
    dem_cfg = config.get("dem", {})
    retries = dem_cfg.get("download_retries", 3)
    retry_delay = dem_cfg.get("retry_delay_s", 5)

    for demtype, resolution_m, description in OPENTOPO_DEM_TYPES:
        cache_key = f"dem_{demtype}_{rounded[0]}_{rounded[1]}_{rounded[2]}_{rounded[3]}"
        cache_path = os.path.join(cache_dir, f"{cache_key}.tif")

        if use_cache and os.path.exists(cache_path):
            logger.info(f"Loading {description} from cache: {cache_path}")
            # Validate even cached DEM
            res_m, warnings = _validate_dem_resolution(cache_path, config)
            for w in warnings:
                logger.warning(f"  DEM WARNING: {w}")
            return cache_path, warnings

        result = download_opentopography_dem(
            demtype, bounds, cache_path, api_key,
            retries=retries, retry_delay=retry_delay
        )
        if result:
            logger.info(f"Using {description} ({resolution_m}m) for terrain analysis.")
            # Validate downloaded DEM
            res_m, warnings = _validate_dem_resolution(result, config)
            for w in warnings:
                logger.warning(f"  DEM WARNING: {w}")
            return result, warnings

    # All downloads failed — raise error instead of generating mock data
    logger.error(
        "ALL DEM DOWNLOADS FAILED. Cannot proceed without elevation data. "
        "Check your OPENTOPOGRAPHY_API_KEY in .env and network connection."
    )
    raise RuntimeError(
        "Failed to download DEM from OpenTopography after trying all available sources. "
        "Verify your API key and internet connection."
    )
