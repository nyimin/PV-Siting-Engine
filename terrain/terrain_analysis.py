import os
import numpy as np
import rasterio
from rasterio.warp import calculate_default_transform, reproject, Resampling
import logging
from scipy.ndimage import convolve, gaussian_filter

logger = logging.getLogger("PVLayoutEngine.terrain")


def _auto_utm_epsg(longitude, latitude):
    """Returns the EPSG code for the UTM zone at the given lon/lat."""
    zone_number = int((longitude + 180) / 6) + 1
    if latitude >= 0:
        return 32600 + zone_number  # Northern hemisphere
    else:
        return 32700 + zone_number  # Southern hemisphere


def reproject_dem_to_utm(dem_path, output_path, config=None):
    """
    Reprojects a DEM from geographic CRS (WGS84) to the appropriate UTM zone.
    Returns the path to the reprojected DEM and the UTM EPSG code.
    Optionally applies cubic sub-grid resampling if configured.
    """
    if config is None: config = {}
    with rasterio.open(dem_path) as src:
        if not src.crs.is_geographic:
            logger.info(f"  DEM already in projected CRS: {src.crs}")
            return dem_path, src.crs

        center_lon = (src.bounds.left + src.bounds.right) / 2
        center_lat = (src.bounds.bottom + src.bounds.top) / 2
        utm_epsg = _auto_utm_epsg(center_lon, center_lat)
        dst_crs = f"EPSG:{utm_epsg}"

        target_res = config.get("terrain", {}).get("resample_resolution_m", None)
        if target_res:
            logger.info(f"  Reprojecting DEM from {src.crs} to {dst_crs} at {target_res}m resolution (cubic resampling)")
            transform, width, height = calculate_default_transform(
                src.crs, dst_crs, src.width, src.height, *src.bounds,
                resolution=(target_res, target_res)
            )
        else:
            logger.info(f"  Reprojecting DEM from {src.crs} to {dst_crs} at native resolution")
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
    Calculates slope in DEGREES using Horn's method with proper cell sizes.
    Returns an array of slope values in degrees (0–90°).
    """
    kernel_x = np.array([[-1, 0, 1],
                          [-2, 0, 2],
                          [-1, 0, 1]], dtype=float) / (8.0 * cellsize_x)
    kernel_y = np.array([[ 1,  2,  1],
                          [ 0,  0,  0],
                          [-1, -2, -1]], dtype=float) / (8.0 * cellsize_y)

    dzdx = convolve(elevation, kernel_x, mode='nearest')
    dzdy = convolve(elevation, kernel_y, mode='nearest')
    slope_deg = np.degrees(np.arctan(np.sqrt(dzdx**2 + dzdy**2)))
    return slope_deg.astype(np.float32)


def calculate_aspect(elevation, cellsize_x, cellsize_y):
    """
    Calculates aspect in degrees (0=North, 90=East, 180=South, 270=West),
    measured clockwise. Uses Horn's method for consistent derivatives.
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
    return aspect.astype(np.float32)


def calculate_tri(elevation):
    """
    Calculates Terrain Ruggedness Index (TRI).
    TRI = mean absolute difference between centre pixel and its 8 neighbours.
    """
    e = elevation.astype(np.float64)
    tri = np.zeros_like(e)

    shifts = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]
    for dy, dx in shifts:
        shifted = np.roll(e, shift=(dy, dx), axis=(0, 1))
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
    Calculates profile curvature using Zevenbergen & Thorne (1987) method.
    Uses scipy.ndimage.convolve (mode='nearest') to avoid np.roll edge artefacts.
    """
    L = cellsize
    # 3×3 second-derivative kernels via convolve (no toroidal wrap artefacts)
    # D coefficient: (Z4 + Z6)/2 - Z5  / L²  → kernel on Z
    kernel_D = np.array([[0, 0, 0],
                          [1, -2, 1],
                          [0, 0, 0]], dtype=float) / (2.0 * L**2)
    # E coefficient: (Z2 + Z8)/2 - Z5  / L²
    kernel_E = np.array([[0, 1, 0],
                          [0, -2, 0],
                          [0, 1, 0]], dtype=float) / (2.0 * L**2)
    # F coefficient: (Z3 - Z1 + Z7 - Z9) / 4L²
    kernel_F = np.array([[-1, 0, 1],
                          [0, 0, 0],
                          [1, 0, -1]], dtype=float) / (4.0 * L**2)
    # G coefficient: (Z6 - Z4) / 2L   (dz/dx)
    kernel_G = np.array([[0, 0, 0],
                          [-1, 0, 1],
                          [0, 0, 0]], dtype=float) / (2.0 * L)
    # H coefficient: (Z2 - Z8) / 2L   (dz/dy)
    kernel_H = np.array([[0, -1, 0],
                          [0, 0, 0],
                          [0, 1, 0]], dtype=float) / (2.0 * L)

    elev = elevation.astype(np.float64)
    D = convolve(elev, kernel_D, mode='nearest')
    E = convolve(elev, kernel_E, mode='nearest')
    F = convolve(elev, kernel_F, mode='nearest')
    G = convolve(elev, kernel_G, mode='nearest')
    H = convolve(elev, kernel_H, mode='nearest')

    denominator = G**2 + H**2
    denominator = np.where(denominator == 0, 1e-10, denominator)
    curvature = -2 * (D * G**2 + E * H**2 + F * G * H) / denominator
    curvature = np.where(G**2 + H**2 < 1e-10, 0.0, curvature)

    return curvature.astype(np.float32)
def classify_slope_direction(slope_deg, aspect_deg, row_azimuth_deg=180):
    """
    Returns 'favourable', 'neutral', or 'unfavourable' slope aspect 
    relative to the panel row orientation.
    For N-S sloped terrain (N-facing or S-facing), the slope is ACROSS the rows.
    For E-W sloped terrain, the slope is ALONG the rows (less critical).
    """
    delta = np.abs(((aspect_deg - row_azimuth_deg + 180) % 360) - 180)
    across_row = delta < 45  # slope runs N-S, perpendicular to E-W rows
    along_row  = (delta >= 45) & (delta < 135)  # slope runs E-W, along rows
    return across_row, along_row


def calculate_flow_accumulation(dem_path):
    """
    Calculates D8 flow accumulation using pysheds to identify natural drainage
    and compute Topographic Wetness Index (TWI).
    Returns the flow accumulation array (cells) and a boolean mask of stream channels.
    """
    try:
        from pysheds.grid import Grid
    except ImportError:
        logger.warning("pysheds not installed. Skipping D8 flow accumulation.")
        return None, None

    logger.info(f"    Setting up PySheds grid from: {dem_path}")
    
    grid = Grid.from_raster(dem_path)
    dem = grid.read_raster(dem_path)
    
    # Ensure dem is proper Raster type
    if not hasattr(dem, 'nodata'):
        dem.nodata = 0
        
    logger.info("    Resolving depressions and flats...")
    try:
        flooded_dem = grid.fill_depressions(dem)
        inflated_dem = grid.resolve_flats(flooded_dem)
    except Exception as e:
        logger.warning(f"Failed to resolve depressions/flats: {e}. Using raw DEM.")
        inflated_dem = dem

    logger.info("    Calculating D8 flow direction...")
    # Standard D8 directional mapping
    dirmap = (64, 128, 1, 2, 4, 8, 16, 32)
    fdir = grid.flowdir(inflated_dem, dirmap=dirmap)

    logger.info("    Calculating D8 flow accumulation...")
    acc = grid.accumulation(fdir, dirmap=dirmap)

    # Clean up output
    acc = np.where(acc < 0, 0, acc)
    
    # Identify stream channels (e.g. collecting > 500 up-slope cells)
    # 500 cells @ 10x10m = 50,000 m2 catchment
    stream_mask = acc > 500
    
    return acc.astype(np.float32), stream_mask
        

def calculate_twi(slope_deg, flow_accumulation, cell_area_m2=100.0):
    """
    Topographic Wetness Index (TWI) = ln(a / tan(β))
    a = specific catchment area (accumulated valid area per unit contour length)
    β = slope in radians
    
    Returns array of TWI values.
    """
    if flow_accumulation is None:
        logger.debug("TWI received no flow accumulation, returning zeros.")
        return np.zeros_like(slope_deg, dtype=np.float32)
        
    slope_rad = np.radians(np.clip(slope_deg, 0.1, 89.0))  # avoid log(0)
    tan_beta = np.tan(slope_rad)
    tan_beta = np.where(tan_beta < 1e-6, 1e-6, tan_beta)
    
    # Specific catchment area = (flow_acc * cell_area) / cell_width
    # Assuming roughly square cells
    cell_width = np.sqrt(cell_area_m2)
    sca = (flow_accumulation * cell_area_m2) / cell_width
    sca = np.where(sca < cell_width, cell_width, sca) # Min area is 1 cell
    
    twi = np.log(sca / tan_beta)
    return twi.astype(np.float32)




def calculate_tpi(elevation, radius_pixels=10):
    """
    Calculates Topographic Position Index (TPI) to identify valleys and ridges.
    TPI = Elevation - Mean(Neighborhood).
    Negative TPI indicates valleys, ravines, and likely drainage channels.
    Positive TPI indicates ridges and peaks.
    """
    # Create circular kernel
    y, x = np.ogrid[-radius_pixels:radius_pixels+1, -radius_pixels:radius_pixels+1]
    kernel = x**2 + y**2 <= radius_pixels**2
    kernel = kernel.astype(float)
    kernel /= kernel.sum()

    elev = elevation.astype(np.float32)
    # Handle NaN/NoData to avoid spreading NaNs
    mask = ~np.isnan(elev) & (elev != 0)
    elev_safe = np.where(mask, elev, np.mean(elev[mask]))

    mean_elev = convolve(elev_safe, kernel, mode='nearest')
    tpi = elev - mean_elev
    
    # Mask out non-data areas
    tpi = np.where(mask, tpi, 0.0)
    return tpi.astype(np.float32)


def calculate_hillshade(elevation, cellsize_x, cellsize_y, azimuth=315, altitude=45):
    """
    Calculates hillshade for visualisation.
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


# ─────────────────────────────────────────────────────────────────────────────
# Suitability scoring  (0–3 integer scale — Myanmar GIS SOP methodology)
# ─────────────────────────────────────────────────────────────────────────────

def calculate_slope_suitability(slope_deg, config):
    """
    5-class slope suitability score (0–3) based on slope in degrees.

    Class 1:  0 – c1   → score 3  (flat / ideal)
    Class 2: c1 – c2   → score 2  (gentle, some grading)
    Class 3: c2 – c3   → score 1  (moderate, significant grading)
    Class 4: c3 – max  → score 0  (unsuitable / hard exclusion)
    Class 5: > max     → score 0  (very steep, hard exclusion)

    Thresholds are read from config['terrain']:
        slope_class1_max_deg: 3
        slope_class2_max_deg: 7
        slope_class3_max_deg: 12
        slope_class4_max_deg: 15   (= max_slope_deg)
    """
    tc = config.get("terrain", {})
    c1 = tc.get("slope_class1_max_deg", 3)
    c2 = tc.get("slope_class2_max_deg", 7)
    c3 = tc.get("slope_class3_max_deg", 12)

    score = np.zeros_like(slope_deg, dtype=np.float32)
    score[slope_deg < c1]                          = 3.0
    score[(slope_deg >= c1) & (slope_deg < c2)]   = 2.0
    score[(slope_deg >= c2) & (slope_deg < c3)]   = 1.0
    # Class 4 & 5 remain 0 (hard exclusion at constraint stage)

    return score


def calculate_aspect_suitability(slope_deg, aspect_deg, config):
    """
    8-direction aspect suitability score (0–3) for fixed-tilt PV in Northern Hemisphere.
    Flat terrain (slope < class1 threshold) automatically receives score 3
    regardless of aspect direction.

    Score 3 — Very Good:
        slope < c1 (flat, aspect irrelevant)  OR  135° ≤ aspect < 225° (S-facing)
    Score 2 — Good:
        112.5° ≤ aspect < 135° (SE)  OR  225° ≤ aspect < 247.5° (SW)  [slope ≥ c1]
    Score 1 — Moderate:
        67.5° ≤ aspect < 112.5° (E)  OR  247.5° ≤ aspect < 292.5° (W)  [slope ≥ c1]
    Score 0 — Poor (N-facing):
        0° ≤ aspect < 67.5°  OR  292.5° ≤ aspect ≤ 360°  [slope ≥ c1]

    Southern hemisphere: scoring is mirrored (0° = N = best, 180° = S = worst).
    """
    tc = config.get("terrain", {})
    c1 = tc.get("slope_class1_max_deg", 3)

    # Derive hemisphere from config or default to northern
    # (will be set by process_terrain using site latitude)
    is_northern = config.get("_site_latitude", 15.0) >= 0

    score = np.zeros_like(aspect_deg, dtype=np.float32)
    flat  = slope_deg < c1
    steep = ~flat

    if is_northern:
        # Northern Hemisphere — south-facing preferred
        south  = (aspect_deg >= 135) & (aspect_deg < 225)
        se     = (aspect_deg >= 112.5) & (aspect_deg < 135)
        sw     = (aspect_deg >= 225) & (aspect_deg < 247.5)
        east   = (aspect_deg >= 67.5) & (aspect_deg < 112.5)
        west   = (aspect_deg >= 247.5) & (aspect_deg < 292.5)
        # Score 3: flat OR south
        score[flat]                        = 3.0
        score[steep & south]               = 3.0
        # Score 2: SE or SW (only on sloped terrain)
        score[steep & (se | sw)]           = 2.0
        # Score 1: E or W
        score[steep & (east | west)]       = 1.0
        # Score 0: N-facing — remains 0 (already initialised)
    else:
        # Southern Hemisphere — north-facing preferred
        north  = (aspect_deg < 45) | (aspect_deg >= 315)
        ne     = (aspect_deg >= 45) & (aspect_deg < 67.5)
        nw     = (aspect_deg >= 292.5) & (aspect_deg < 315)
        east   = (aspect_deg >= 67.5) & (aspect_deg < 112.5)
        west   = (aspect_deg >= 247.5) & (aspect_deg < 292.5)
        score[flat]                        = 3.0
        score[steep & north]               = 3.0
        score[steep & (ne | nw)]           = 2.0
        score[steep & (east | west)]       = 1.0
        # S-facing remains 0

    return score


def calculate_suitability(slope_deg, aspect_deg, tri, config):
    """
    Computes the combined terrain suitability index (0–3) using:
        Index = slope_weight × slope_score + aspect_weight × aspect_score

    TRI is used as a hard-exclusion constraint (in constraint_combiner.py),
    NOT as a weighted score component — keeps the index interpretable on 0–3.

    Returns:
        suitability  : float32 array, 0–3 scale
        slope_score  : float32 array (0–3) for export
        aspect_score : float32 array (0–3) for export
    """
    tc = config.get("terrain", {})
    ws = float(tc.get("slope_weight", 0.60))
    wa = float(tc.get("aspect_weight", 0.40))

    # Ensure weights sum to 1
    total = ws + wa
    ws, wa = ws / total, wa / total

    slope_score  = calculate_slope_suitability(slope_deg, config)
    aspect_score = calculate_aspect_suitability(slope_deg, aspect_deg, config)

    suitability = (ws * slope_score + wa * aspect_score).astype(np.float32)

    # Hard floor: pixels above max_slope_deg get 0
    max_deg = tc.get("max_slope_deg", 15)
    suitability[slope_deg > max_deg] = 0.0
    slope_score[slope_deg > max_deg] = 0.0
    aspect_score[slope_deg > max_deg] = 0.0

    return suitability, slope_score, aspect_score


def _save_raster(data, profile, output_path, dtype=rasterio.float32):
    """Helper to save a single-band raster."""
    out_profile = profile.copy()
    out_profile.update(dtype=dtype, count=1, compress='deflate')
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with rasterio.open(output_path, 'w', **out_profile) as dst:
        dst.write(data.astype(dtype), 1)


def process_terrain(dem_path, output_dir, config):
    """
    Full terrain analysis pipeline:
    1. Reprojects DEM to UTM
    2. Calculates slope (degrees), aspect, TRI, curvature, hillshade
    3. Generates slope suitability, aspect suitability, and combined index (0–3 scale)
    4. Saves all outputs as GeoTIFFs

    Phase 1 required outputs:
        slope.tif            — slope in degrees
        aspect.tif           — aspect 0–360°
        tri.tif              — Terrain Ruggedness Index (m)
        curvature.tif        — profile curvature
        hillshade.tif        — visualisation only
        slope_suitability.tif   — 0–3 score
        aspect_suitability.tif  — 0–3 score
        terrain_suitability.tif — combined 0–3 index (weighted)
    """
    logger.info(f"Processing terrain analysis from {dem_path}")
    os.makedirs(output_dir, exist_ok=True)

    # Step 1: Reproject DEM to UTM (with optional cubic resampling)
    utm_dem_path = os.path.join(output_dir, "dem_utm.tif")
    reprojected_path, utm_crs = reproject_dem_to_utm(dem_path, utm_dem_path, config=config)

    # Step 2: Read projected DEM
    with rasterio.open(reprojected_path) as src:
        elevation = src.read(1).astype(np.float64)
        transform = src.transform
        crs = src.crs
        profile = src.profile

        cellsize_x = abs(transform.a)
        cellsize_y = abs(transform.e)
        cellsize = (cellsize_x + cellsize_y) / 2.0

        logger.info(f"  UTM cell size: {cellsize_x:.2f} × {cellsize_y:.2f} m")

        # Optional: Gaussian smoothing to remove radar noise (trees/artefacts)
        sigma = config.get("terrain", {}).get("gaussian_smooth_sigma", 0)
        if sigma > 0:
            logger.info(f"  Applying Gaussian smoothing (sigma={sigma}) to remove high-frequency DEM noise...")
            # We must handle NoData so smoothing doesn't pull in edge zeroes
            nodata = src.nodata
            if nodata is not None:
                mask = (elevation == nodata)
                elevation[mask] = np.mean(elevation[~mask])  # temporary fill
                elevation = gaussian_filter(elevation, sigma=sigma)
                elevation[mask] = nodata
            else:
                elevation = gaussian_filter(elevation, sigma=sigma)

    # Step 3: Extract site latitude from UTM DEM centre (no re-read of raw DEM)
    try:
        from pyproj import Transformer
        centre_x = transform.c + cellsize_x * elevation.shape[1] / 2
        centre_y = transform.f - cellsize_y * elevation.shape[0] / 2
        transformer = Transformer.from_crs(str(crs), "EPSG:4326", always_xy=True)
        centre_lon, centre_lat = transformer.transform(centre_x, centre_y)
    except Exception:
        # Fallback: read from raw DEM (original behaviour)
        with rasterio.open(dem_path) as src_wgs84:
            centre_lat = (src_wgs84.bounds.bottom + src_wgs84.bounds.top) / 2
    logger.info(f"  Site latitude: {centre_lat:.3f}°")

    # Inject into config so aspect suitability knows hemisphere
    config["_site_latitude"] = centre_lat

    # Step 4: Compute terrain derivatives (slope in DEGREES now)
    logger.info("  Calculating Slope (degrees, Horn's method)...")
    slope_deg = calculate_slope(elevation, cellsize_x, cellsize_y)

    logger.info("  Calculating Aspect...")
    aspect_deg = calculate_aspect(elevation, cellsize_x, cellsize_y)

    logger.info("  Calculating TRI...")
    tri = calculate_tri(elevation)

    logger.info("  Calculating Curvature...")
    curvature = calculate_curvature(elevation, cellsize)

    # Calculate TPI with a ~150m radius (good for identifying main valleys/ravines)
    tpi_radius = max(1, int(150 / cellsize))
    logger.info(f"  Calculating TPI (radius={tpi_radius} pixels)...")
    tpi = calculate_tpi(elevation, radius_pixels=tpi_radius)

    logger.info("  Calculating Hillshade...")
    hillshade = calculate_hillshade(elevation, cellsize_x, cellsize_y)

    logger.info("  Calculating D8 Flow Accumulation (PySheds)...")
    flow_acc, streams = calculate_flow_accumulation(reprojected_path)
    
    logger.info("  Calculating Topographic Wetness Index (TWI)...")
    cell_area = cellsize_x * cellsize_y
    twi = calculate_twi(slope_deg, flow_acc, cell_area_m2=cell_area)

    # Step 5: Suitability scores
    logger.info("  Calculating Suitability Scores (0–3 scale)...")
    suitability, slope_score, aspect_score = calculate_suitability(
        slope_deg, aspect_deg, tri, config
    )

    # Step 6: Save rasters
    slope_path     = os.path.join(output_dir, "slope.tif")
    aspect_path    = os.path.join(output_dir, "aspect.tif")
    tri_path       = os.path.join(output_dir, "tri.tif")
    curv_path      = os.path.join(output_dir, "curvature.tif")
    tpi_path       = os.path.join(output_dir, "tpi.tif")
    twi_path       = os.path.join(output_dir, "twi.tif")
    streams_path   = os.path.join(output_dir, "streams_d8.tif")
    hillshade_path = os.path.join(output_dir, "hillshade.tif")
    slope_suit_path   = os.path.join(output_dir, "slope_suitability.tif")
    aspect_suit_path  = os.path.join(output_dir, "aspect_suitability.tif")
    suitability_path  = os.path.join(output_dir, "terrain_suitability.tif")

    _save_raster(slope_deg,    profile, slope_path)
    _save_raster(aspect_deg,   profile, aspect_path)
    _save_raster(tri,          profile, tri_path)
    _save_raster(curvature,    profile, curv_path)
    _save_raster(tpi,          profile, tpi_path)
    _save_raster(twi,          profile, twi_path)
    if streams is not None:
        _save_raster(streams,  profile, streams_path, dtype=rasterio.uint8)
    _save_raster(hillshade,    profile, hillshade_path, dtype=rasterio.uint8)
    _save_raster(slope_score,  profile, slope_suit_path)
    _save_raster(aspect_score, profile, aspect_suit_path)
    _save_raster(suitability,  profile, suitability_path)

    # Step 7: Log terrain stats
    valid_mask = slope_deg <= 90
    valid_slope = slope_deg[valid_mask]
    tc = config.get("terrain", {})
    threshold = tc.get("buildable_index_threshold", 2.25)

    logger.info("  --- Terrain Summary ---")
    logger.info(f"  Mean slope:      {np.nanmean(valid_slope):.2f}°")
    logger.info(f"  Max slope:       {np.nanmax(valid_slope):.2f}°")
    logger.info(f"  Std slope:       {np.nanstd(valid_slope):.2f}°")
    logger.info(f"  Mean TRI:        {np.nanmean(tri):.2f} m")
    logger.info(f"  Mean suitability index:  {np.nanmean(suitability):.2f} / 3.0")
    logger.info(f"  Buildable area (index≥{threshold}): "
                f"{np.sum(suitability >= threshold) / suitability.size * 100:.1f}% of raster")

    # Calculate across-row and along-row slope percentages
    across_mask, along_mask = classify_slope_direction(slope_deg, aspect_deg, row_azimuth_deg=180)
    
    # Only count these on slopes > 5 degrees so we don't flag flat land
    significant_slope = slope_deg > 5.0
    across_row_area = np.sum(across_mask & significant_slope)
    along_row_area = np.sum(along_mask & significant_slope)
    total_valid_area = np.sum(~np.isnan(slope_deg) & (slope_deg >= 0))
    
    across_row_pct = (across_row_area / total_valid_area * 100) if total_valid_area > 0 else 0
    along_row_pct = (along_row_area / total_valid_area * 100) if total_valid_area > 0 else 0

    terrain_stats = {
        "mean_slope_deg": float(np.nanmean(valid_slope)),
        "max_slope_deg":  float(np.nanmax(valid_slope)),
        "std_slope_deg":  float(np.nanstd(valid_slope)),
        "across_row_slope_pct": float(across_row_pct),
        "along_row_slope_pct": float(along_row_pct),
        "mean_tri_m":     float(np.nanmean(tri)),
        "mean_suitability": float(np.nanmean(suitability)),
        "buildable_pct_terrain": float((np.sum(suitability >= threshold) / suitability.size) * 100)
    }

    logger.info(f"Terrain analysis outputs saved to {output_dir}")

    return {
        "dem_utm":          reprojected_path,
        "slope":            slope_path,
        "aspect":           aspect_path,
        "tri":              tri_path,
        "curvature":        curv_path,
        "tpi":              tpi_path,
        "twi":              twi_path,
        "streams":          streams_path if streams is not None else None,
        "hillshade":        hillshade_path,
        "slope_suitability":  slope_suit_path,
        "aspect_suitability": aspect_suit_path,
        "suitability":      suitability_path,
        "transform":        transform,
        "crs":              crs,
        "utm_epsg":         utm_crs,
        "site_latitude":    centre_lat,
        "stats": terrain_stats
    }
