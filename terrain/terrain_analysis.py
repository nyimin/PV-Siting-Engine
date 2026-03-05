import os
import numpy as np
import rasterio
from rasterio.warp import calculate_default_transform, reproject, Resampling
import logging
from scipy.ndimage import convolve

logger = logging.getLogger("PVLayoutEngine.terrain")


def _auto_utm_epsg(longitude, latitude):
    """Returns the EPSG code for the UTM zone at the given lon/lat."""
    zone_number = int((longitude + 180) / 6) + 1
    if latitude >= 0:
        return 32600 + zone_number  # Northern hemisphere
    else:
        return 32700 + zone_number  # Southern hemisphere


def reproject_dem_to_utm(dem_path, output_path):
    """
    Reprojects a DEM from geographic CRS (WGS84) to the appropriate UTM zone.
    Returns the path to the reprojected DEM and the UTM EPSG code.
    """
    with rasterio.open(dem_path) as src:
        if not src.crs.is_geographic:
            logger.info(f"  DEM already in projected CRS: {src.crs}")
            return dem_path, src.crs

        # Determine UTM zone from DEM center
        center_lon = (src.bounds.left + src.bounds.right) / 2
        center_lat = (src.bounds.bottom + src.bounds.top) / 2
        utm_epsg = _auto_utm_epsg(center_lon, center_lat)
        dst_crs = f"EPSG:{utm_epsg}"

        logger.info(f"  Reprojecting DEM from {src.crs} to {dst_crs} (UTM zone {utm_epsg - 32600 if center_lat >= 0 else utm_epsg - 32700})")

        transform, width, height = calculate_default_transform(
            src.crs, dst_crs, src.width, src.height, *src.bounds
        )

        profile = src.profile.copy()
        profile.update({
            'crs': dst_crs,
            'transform': transform,
            'width': width,
            'height': height,
            'compress': 'deflate'
        })

        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with rasterio.open(output_path, 'w', **profile) as dst:
            reproject(
                source=rasterio.band(src, 1),
                destination=rasterio.band(dst, 1),
                src_transform=src.transform,
                src_crs=src.crs,
                dst_transform=transform,
                dst_crs=dst_crs,
                resampling=Resampling.cubic
            )

    logger.info(f"  Reprojected DEM saved to: {output_path}")
    return output_path, dst_crs


def calculate_slope(elevation, cellsize_x, cellsize_y):
    """
    Calculates slope in percent using Horn's method with proper cell sizes.
    """
    # Horn's method kernels
    kernel_x = np.array([[-1, 0, 1],
                         [-2, 0, 2],
                         [-1, 0, 1]], dtype=float) / (8.0 * cellsize_x)
    kernel_y = np.array([[ 1,  2,  1],
                         [ 0,  0,  0],
                         [-1, -2, -1]], dtype=float) / (8.0 * cellsize_y)

    dzdx = convolve(elevation, kernel_x, mode='nearest')
    dzdy = convolve(elevation, kernel_y, mode='nearest')
    slope_rad = np.arctan(np.sqrt(dzdx**2 + dzdy**2))
    slope_percent = np.tan(slope_rad) * 100
    return slope_percent


def calculate_aspect(elevation, cellsize_x, cellsize_y):
    """
    Calculates aspect in degrees (0=North, 90=East, 180=South, 270=West).
    Uses Horn's method for consistent derivatives.
    """
    kernel_x = np.array([[-1, 0, 1],
                         [-2, 0, 2],
                         [-1, 0, 1]], dtype=float) / (8.0 * cellsize_x)
    kernel_y = np.array([[ 1,  2,  1],
                         [ 0,  0,  0],
                         [-1, -2, -1]], dtype=float) / (8.0 * cellsize_y)

    dzdx = convolve(elevation, kernel_x, mode='nearest')
    dzdy = convolve(elevation, kernel_y, mode='nearest')

    # Aspect: angle from north, clockwise
    aspect = np.degrees(np.arctan2(-dzdx, dzdy))
    aspect = np.where(aspect < 0, 360 + aspect, aspect)
    return aspect


def calculate_tri(elevation):
    """
    Calculates Terrain Ruggedness Index (TRI).
    TRI = mean absolute difference between center pixel and its 8 neighbors.
    Uses efficient array shifting instead of generic_filter.
    """
    e = elevation.astype(np.float64)
    tri = np.zeros_like(e)

    shifts = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]
    for dy, dx in shifts:
        shifted = np.roll(e, shift=(dy, dx), axis=(0, 1))
        # Fix wrapped edges
        if dy > 0:
            shifted[:dy, :] = e[:dy, :]
        elif dy < 0:
            shifted[dy:, :] = e[dy:, :]
        if dx > 0:
            shifted[:, :dx] = e[:, :dx]
        elif dx < 0:
            shifted[:, dx:] = e[:, dx:]

        tri += np.abs(e - shifted)

    return (tri / 8.0).astype(np.float32)


def calculate_curvature(elevation, cellsize):
    """
    Calculates profile curvature using Zevenbergen and Thorne (1987) method.
    Profile curvature is the rate of change of slope along the direction of steepest descent.
    """
    L = cellsize
    Z = elevation
    
    # 3x3 window extraction using shifts
    Z5 = Z
    Z1 = np.roll(Z, shift=(1, 1), axis=(0, 1))
    Z2 = np.roll(Z, shift=(1, 0), axis=(0, 1))
    Z3 = np.roll(Z, shift=(1, -1), axis=(0, 1))
    Z4 = np.roll(Z, shift=(0, 1), axis=(0, 1))
    Z6 = np.roll(Z, shift=(0, -1), axis=(0, 1))
    Z7 = np.roll(Z, shift=(-1, 1), axis=(0, 1))
    Z8 = np.roll(Z, shift=(-1, 0), axis=(0, 1))
    Z9 = np.roll(Z, shift=(-1, -1), axis=(0, 1))
    
    # Polynomial coefficients
    D = ((Z4 + Z6) / 2 - Z5) / (L**2)
    E = ((Z2 + Z8) / 2 - Z5) / (L**2)
    F = (Z3 - Z1 + Z7 - Z9) / (4 * L**2)
    G = (Z6 - Z4) / (2 * L)
    H = (Z2 - Z8) / (2 * L)
    
    # Profile Curvature
    # Avoid division by zero
    denominator = (G**2 + H**2)
    # Add small epsilon to prevent warning, or mask
    denominator = np.where(denominator == 0, 1e-10, denominator)
    
    # Zevenbergen and Thorne formula for profile curvature
    curvature = -2 * (D * G**2 + E * H**2 + F * G * H) / denominator
    # Zero out curvature where slope is effectively zero
    curvature = np.where(G**2 + H**2 == 0, 0, curvature)
    
    return curvature.astype(np.float32)


def calculate_hillshade(elevation, cellsize_x, cellsize_y, azimuth=315, altitude=45):
    """
    Calculates hillshade for visualization.
    azimuth: sun direction in degrees (0=N, 315=NW default)
    altitude: sun elevation angle in degrees
    """
    kernel_x = np.array([[-1, 0, 1],
                         [-2, 0, 2],
                         [-1, 0, 1]], dtype=float) / (8.0 * cellsize_x)
    kernel_y = np.array([[ 1,  2,  1],
                         [ 0,  0,  0],
                         [-1, -2, -1]], dtype=float) / (8.0 * cellsize_y)

    dzdx = convolve(elevation, kernel_x, mode='nearest')
    dzdy = convolve(elevation, kernel_y, mode='nearest')

    slope_rad = np.arctan(np.sqrt(dzdx**2 + dzdy**2))
    aspect_rad = np.arctan2(-dzdx, dzdy)

    az_rad = np.radians(azimuth)
    alt_rad = np.radians(altitude)

    hillshade = (
        np.sin(alt_rad) * np.cos(slope_rad) +
        np.cos(alt_rad) * np.sin(slope_rad) * np.cos(az_rad - aspect_rad)
    )
    hillshade = np.clip(hillshade * 255, 0, 255).astype(np.uint8)
    return hillshade


def calculate_suitability(slope, aspect, tri, config, latitude):
    """
    Computes a solar suitability score raster (0–100).
    Higher = more suitable for solar PV.

    Scoring:
    - Slope: 0–5% = 100, 5–10% = linear 100→30, >10% = 0
    - Aspect penalty: north-facing penalized in northern hemisphere, south-facing in southern.
    - TRI penalty: high ruggedness reduces score
    """
    terrain_cfg = config.get("terrain", {})
    preferred_slope = terrain_cfg.get("preferred_slope_percent", 5)
    max_slope = terrain_cfg.get("max_slope_percent", 10)
    apply_aspect_penalty = terrain_cfg.get("aspect_penalty", True)

    # --- Slope score (weight: 60%) ---
    slope_score = np.zeros_like(slope)
    # Preferred: full score
    mask_good = slope <= preferred_slope
    slope_score[mask_good] = 100.0
    # Acceptable: linear ramp
    mask_ok = (slope > preferred_slope) & (slope <= max_slope)
    slope_score[mask_ok] = 100.0 - 70.0 * (slope[mask_ok] - preferred_slope) / (max_slope - preferred_slope)
    # Excluded: zero
    slope_score[slope > max_slope] = 0.0

    # --- Aspect score (weight: 20%) ---
    aspect_score = np.full_like(aspect, 100.0)
    if apply_aspect_penalty:
        if latitude >= 0:
            # Northern Hemisphere: Penalize north-facing slopes (315°–45°)
            penalty_mask = (aspect <= 45) | (aspect >= 315)
            # Angle diff from due North (0)
            angle_diff = np.where(aspect <= 180, aspect, 360 - aspect)
        else:
            # Southern Hemisphere: Penalize south-facing slopes (135°–225°)
            penalty_mask = (aspect >= 135) & (aspect <= 225)
            # Angle diff from due South (180)
            angle_diff = np.abs(aspect - 180)
            
        penalty = np.where(penalty_mask, (1 - angle_diff / 45.0) * 60.0, 0)
        aspect_score -= np.clip(penalty, 0, 60)

    # --- TRI score (weight: 20%) ---
    # Normalize TRI: 0 = smooth (100 score), >5m = rough (0 score)
    tri_score = np.clip(100.0 - (tri / 5.0) * 100.0, 0, 100)

    # --- Weighted combination ---
    suitability = (0.60 * slope_score + 0.20 * aspect_score + 0.20 * tri_score).astype(np.float32)

    # Hard exclusion: slopes beyond max get 0 regardless
    suitability[slope > max_slope] = 0.0

    return suitability


def _save_raster(data, profile, output_path, dtype=rasterio.float32):
    """Helper to save a single-band raster."""
    out_profile = profile.copy()
    out_profile.update(dtype=dtype, count=1, compress='deflate')
    with rasterio.open(output_path, 'w', **out_profile) as dst:
        dst.write(data.astype(dtype), 1)


def process_terrain(dem_path, output_dir, config):
    """
    Full terrain analysis pipeline:
    1. Reprojects DEM to UTM
    2. Calculates slope, aspect, TRI, curvature, hillshade
    3. Generates solar suitability score raster
    4. Saves all outputs as GeoTIFFs
    """
    logger.info(f"Processing terrain analysis from {dem_path}")
    os.makedirs(output_dir, exist_ok=True)

    # Step 1: Reproject DEM to UTM for accurate metric calculations
    utm_dem_path = os.path.join(output_dir, "dem_utm.tif")
    reprojected_path, utm_crs = reproject_dem_to_utm(dem_path, utm_dem_path)

    # Step 2: Read the projected DEM
    with rasterio.open(reprojected_path) as src:
        elevation = src.read(1).astype(np.float64)
        transform = src.transform
        crs = src.crs
        profile = src.profile

        cellsize_x = abs(transform.a)
        cellsize_y = abs(transform.e)
        cellsize = (cellsize_x + cellsize_y) / 2.0

        logger.info(f"  UTM cell size: {cellsize_x:.2f} × {cellsize_y:.2f} m")

    # Step 3: Compute terrain derivatives
    logger.info("  Calculating Slope (Horn's method)...")
    slope = calculate_slope(elevation, cellsize_x, cellsize_y)

    logger.info("  Calculating Aspect...")
    aspect = calculate_aspect(elevation, cellsize_x, cellsize_y)

    logger.info("  Calculating TRI...")
    tri = calculate_tri(elevation)

    logger.info("  Calculating Curvature...")
    curvature = calculate_curvature(elevation, cellsize)

    logger.info("  Calculating Hillshade...")
    hillshade = calculate_hillshade(elevation, cellsize_x, cellsize_y)

    logger.info("  Calculating Solar Suitability Score...")
    
    # Extract latitude from the DEM center to inform the hemisphere-aware aspect penalty
    with rasterio.open(dem_path) as src_wgs84:
         center_lat = (src_wgs84.bounds.bottom + src_wgs84.bounds.top) / 2
    
    suitability = calculate_suitability(slope, aspect, tri, config, center_lat)

    # Step 4: Save all outputs
    slope_path = os.path.join(output_dir, "slope.tif")
    _save_raster(slope, profile, slope_path)

    aspect_path = os.path.join(output_dir, "aspect.tif")
    _save_raster(aspect, profile, aspect_path)

    tri_path = os.path.join(output_dir, "tri.tif")
    _save_raster(tri, profile, tri_path)

    curvature_path = os.path.join(output_dir, "curvature.tif")
    _save_raster(curvature, profile, curvature_path)

    hillshade_path = os.path.join(output_dir, "hillshade.tif")
    _save_raster(hillshade, profile, hillshade_path, dtype=rasterio.uint8)

    suitability_path = os.path.join(output_dir, "suitability.tif")
    _save_raster(suitability, profile, suitability_path)

    # Step 5: Log terrain statistics
    valid_slope = slope[slope <= 100]  # Filter outliers
    logger.info(f"  --- Terrain Summary ---")
    logger.info(f"  Mean slope: {np.nanmean(valid_slope):.1f}%")
    logger.info(f"  Max slope:  {np.nanmax(valid_slope):.1f}%")
    logger.info(f"  Std slope:  {np.nanstd(valid_slope):.1f}%")
    logger.info(f"  Mean TRI:   {np.nanmean(tri):.2f} m")
    logger.info(f"  Suitability: mean={np.nanmean(suitability):.1f}, "
                f"area>50: {np.sum(suitability > 50) / suitability.size * 100:.1f}%")

    logger.info(f"Terrain analysis outputs saved to {output_dir}")

    return {
        "dem_utm": reprojected_path,
        "slope": slope_path,
        "aspect": aspect_path,
        "tri": tri_path,
        "curvature": curvature_path,
        "hillshade": hillshade_path,
        "suitability": suitability_path,
        "transform": transform,
        "crs": crs,
        "utm_epsg": utm_crs,
        "stats": {
            "mean_slope": float(np.nanmean(valid_slope)),
            "max_slope": float(np.nanmax(valid_slope)),
            "std_slope": float(np.nanstd(valid_slope)),
            "mean_tri": float(np.nanmean(tri)),
            "mean_suitability": float(np.nanmean(suitability)),
        }
    }
