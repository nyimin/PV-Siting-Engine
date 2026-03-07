"""
Shared raster helpers used by layout and routing modules.
"""
import numpy as np


def sample_raster_mean(geom, raster_path):
    """Samples the mean value of a raster within a given geometry.

    Parameters
    ----------
    geom : shapely.geometry.BaseGeometry
        Polygon or buffered point to sample.
    raster_path : str
        Path to a single-band GeoTIFF.

    Returns
    -------
    float or None
        Mean raster value inside *geom*, or ``None`` on failure.
    """
    try:
        import rasterio
        from rasterio.mask import mask
        with rasterio.open(raster_path) as src:
            out_image, _ = mask(src, [geom], crop=True)
            valid = out_image[out_image != src.nodata]
            if valid.size > 0:
                return float(np.nanmean(valid))
    except Exception:
        pass
    return None
