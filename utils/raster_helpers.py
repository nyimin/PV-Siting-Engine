"""
Shared raster helpers used by layout and routing modules.
"""
import numpy as np


def sample_raster_mean(geom, src):
    """Samples the mean value of a raster within a given geometry.

    Parameters
    ----------
    geom : shapely.geometry.BaseGeometry
        Polygon or buffered point to sample.
    src : str or rasterio.DatasetReader
        Path to raster file or an open rasterio dataset.

    Returns
    -------
    float or None
        Mean raster value inside *geom*, or ``None`` on failure.
    """
    try:
        from rasterio.mask import mask
        import rasterio
        
        if isinstance(src, str):
            with rasterio.open(src) as dataset:
                out_image, _ = mask(dataset, [geom], crop=True)
                nodata = dataset.nodata
        else:
            out_image, _ = mask(src, [geom], crop=True)
            nodata = src.nodata

        valid = out_image[out_image != nodata]
        if valid.size > 0:
            return float(np.nanmean(valid))
    except Exception:
        pass
    return None


def sample_raster_point(point, src):
    """Samples the raster value at a specific point geometry.

    Parameters
    ----------
    point : shapely.geometry.Point
        Point to sample.
    src : str or rasterio.DatasetReader
        Path to raster file or an open rasterio dataset.
        
    Returns
    -------
    float or None
        Raster value at the point, or ``None`` on failure.
    """
    try:
        import rasterio
        coords = [(point.x, point.y)]
        
        if isinstance(src, str):
            with rasterio.open(src) as dataset:
                val = next(dataset.sample(coords))[0]
                nodata = dataset.nodata
        else:
            val = next(src.sample(coords))[0]
            nodata = src.nodata

        if val != nodata and not np.isnan(val):
            return float(val)
    except Exception:
        pass
    return None
