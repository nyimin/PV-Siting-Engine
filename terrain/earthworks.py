import logging
import numpy as np

logger = logging.getLogger("PVLayoutEngine.terrain")

def calculate_earthworks(blocks_gdf, terrain_paths, config):
    """
    Calculates Cut/Fill earthworks volume (m3) by integrating DEM elevation under each block.
    Fits a best-fit 3D plane (grading plane) to the terrain points within the block,
    then sums the volume above (cut) and below (fill) the plane.
    
    Returns:
        total_cut_m3 (float): Total high dirt removed
        total_fill_m3 (float): Total low dirt filled
        total_cost_usd (float): Estimated grading CapEx
        rejected_area_ha (float): Area rejected due to excessive max cut (> 1.5m)
    """
    total_cut_m3 = 0.0
    total_fill_m3 = 0.0
    rejected_area_ha = 0.0
    
    grading_cost_per_m3 = config.get("costs", {}).get("grading_usd_per_m3", 5.0)
    max_cut_threshold = config.get("terrain", {}).get("max_cut_m", 1.5)
    
    if blocks_gdf is None or blocks_gdf.empty or not terrain_paths or "dem_utm" not in terrain_paths:
        return 0.0, 0.0, 0.0, 0.0

    dem_path = terrain_paths["dem_utm"]
    
    try:
        import rasterio
        from rasterio.mask import mask
        
        with rasterio.open(dem_path) as src:
            transform = src.transform
            # Area of one DEM pixel
            pixel_area = abs(transform.a * transform.e)
            
            for idx, block in blocks_gdf.iterrows():
                geom = block.geometry
                
                # Mask DEM to block polygon
                try:
                    out_image, out_transform = mask(src, [geom], crop=True)
                except ValueError:
                    continue # Polygon falls outside DEM
                
                valid = out_image[0]
                nodata = src.nodata if src.nodata is not None else -9999
                mask_valid = (valid != nodata) & ~np.isnan(valid)
                
                valid_elevs = valid[mask_valid]
                if valid_elevs.size < 10:
                    continue
                    
                # Create coordinate grids for the valid pixels
                rows, cols = np.where(mask_valid)
                xs, ys = rasterio.transform.xy(out_transform, rows, cols)
                xs = np.array(xs)
                ys = np.array(ys)
                zs = valid_elevs
                
                # ── Fit best-fit plane: z = A*x + B*y + C ──
                # We solve: [x, y, 1] * [A, B, C]^T = z
                A_matrix = np.c_[xs, ys, np.ones(xs.shape[0])]
                # Least squares solution
                C, _, _, _ = np.linalg.lstsq(A_matrix, zs, rcond=None)
                
                # Plane elevations for all points
                plane_zs = C[0]*xs + C[1]*ys + C[2]
                
                # Difference: natural ground minus proposed grade
                diff = zs - plane_zs
                
                max_cut = np.max(diff) # Max height above plane
                # If block requires too much cutting, reject it
                if max_cut > max_cut_threshold:
                    rejected_area_ha += geom.area / 10000.0
                    continue
                
                # Cut is where zs > plane_zs (diff > 0)
                cut_depths = diff[diff > 0]
                # Fill is where zs < plane_zs (diff < 0)
                fill_depths = -diff[diff < 0]
                
                block_cut_m3 = np.sum(cut_depths) * pixel_area
                block_fill_m3 = np.sum(fill_depths) * pixel_area
                
                total_cut_m3 += block_cut_m3
                total_fill_m3 += block_fill_m3
                
    except Exception as e:
        logger.warning(f"Failed rigorous earthworks estimation: {e}")
        
    # Assume we pay per m3 of material moved (max of cut or fill, since usually cut is used for fill if balanced, but we just bill total moved roughly or max)
    # Simple estimate: price per m3 placed or dug. Let's just use (cut + fill) / 2 for balanced, or total cut + total borrowed fill.
    # To keep it simple: bill for both cut and fill operations, maybe at different rates, but we just use one rate for total volume moved.
    moved_volume = total_cut_m3 + total_fill_m3
    total_cost_usd = moved_volume * grading_cost_per_m3

    return total_cut_m3, total_fill_m3, total_cost_usd, rejected_area_ha
