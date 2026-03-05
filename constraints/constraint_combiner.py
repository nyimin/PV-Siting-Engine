import os
import logging
import geopandas as gpd
import pandas as pd
from shapely.geometry import box
import rasterio
from rasterio.features import shapes
import numpy as np

logger = logging.getLogger("PVLayoutEngine.constraints")


def process_osm_constraints(osm_dict, site_crs, config):
    """
    Applies buffers to OSM constraints based on the config.
    Returns a unified exclusion GeoDataFrame.
    """
    logger.info("Processing OSM constraints...")

    buffers = config.get("buffers", {})
    all_exclusions = []

    buffer_map = {
        "buildings": "buildings_m",
        "water":     "rivers_m",
        "roads":     "roads_m",
        "railways":  "railways_m",
        "power":     "power_lines_m",
    }

    default_buffers = {
        "buildings_m": 30,
        "rivers_m": 50,
        "roads_m": 20,
        "railways_m": 50,
        "power_lines_m": 30,
    }

    for layer_name, gdf in osm_dict.items():
        if gdf.empty:
            continue

        # Ensure CRS matches site
        if gdf.crs != site_crs:
            gdf = gdf.to_crs(site_crs)

        # Get buffer distance from config
        config_key = buffer_map.get(layer_name, None)
        if config_key:
            buffer_dist = buffers.get(config_key, default_buffers.get(config_key, 30))
        else:
            buffer_dist = 30

        logger.info(f"  Applying {buffer_dist}m buffer to {layer_name} ({len(gdf)} features)")
        buffered_gdf = gdf.copy()
        buffered_gdf["geometry"] = buffered_gdf.geometry.buffer(buffer_dist)

        # Add metadata
        buffered_gdf["constraint_type"] = f"osm_{layer_name}"
        all_exclusions.append(buffered_gdf[["geometry", "constraint_type"]])

    if all_exclusions:
        return gpd.GeoDataFrame(pd.concat(all_exclusions, ignore_index=True), crs=site_crs)
    else:
        return gpd.GeoDataFrame(columns=["geometry", "constraint_type"], crs=site_crs)


def raster_to_polygons(raster_path, threshold, crs, constraint_name="slope"):
    """
    Converts binary/thresholded raster (e.g., slope > max_slope) into exclusion polygons.
    """
    logger.info(f"  Converting {constraint_name} raster to exclusion polygons (threshold={threshold})...")
    with rasterio.open(raster_path) as src:
        data = src.read(1)
        transform = src.transform

        # Create mask where pixels EXCEED threshold
        mask = data > threshold

        excluded_pct = np.sum(mask) / mask.size * 100
        logger.info(f"  {constraint_name}: {excluded_pct:.1f}% of raster pixels exceed threshold")

        # Extract shapes
        results = (
            {"properties": {"constraint_type": f"terrain_{constraint_name}"}, "geometry": s}
            for i, (s, v) in enumerate(shapes(mask.astype('uint8'), mask=mask, transform=transform))
            if v == 1
        )

        polygons = list(results)
        if polygons:
            gdf = gpd.GeoDataFrame.from_features(polygons, crs=src.crs)
            if gdf.crs != crs:
                gdf = gdf.to_crs(crs)
            return gdf
        else:
            return gpd.GeoDataFrame(columns=["geometry", "constraint_type"], crs=crs)


def combine_constraints(site_gdf, osm_exclusions, terrain_analysis_paths, config):
    """
    Combines all constraints and subtracts them from the site boundary to yield the buildable area.
    """
    logger.info("Combining all constraints...")
    site_crs = site_gdf.crs

    all_exclusions = [osm_exclusions] if not osm_exclusions.empty else []

    # 1. Terrain Exclusions (Slope)
    slope_path = terrain_analysis_paths.get("slope")
    max_slope = config.get("terrain", {}).get("max_slope_percent", 10)

    if slope_path and os.path.exists(slope_path):
        slope_exclusions = raster_to_polygons(slope_path, max_slope, site_crs, "slope")
        if not slope_exclusions.empty:
            all_exclusions.append(slope_exclusions)

    # 1.5 Terrain Exclusions (TRI / Ruggedness)
    tri_path = terrain_analysis_paths.get("tri")
    max_tri = config.get("terrain", {}).get("max_tri_m", 3.0)  # over 3m difference is unbuildable without mass grading
    
    if tri_path and os.path.exists(tri_path):
        tri_exclusions = raster_to_polygons(tri_path, max_tri, site_crs, "roughness_tri")
        if not tri_exclusions.empty:
            all_exclusions.append(tri_exclusions)

    # 2. Site Boundary Setback / Buffer
    setback_m = config.get("buffers", {}).get("site_boundary_m", 15)
    if setback_m > 0:
        logger.info(f"Applying {setback_m}m setback from site boundary...")
        # The exclusion is the area between the original boundary and the buffered inwards boundary
        site_geom = site_gdf.geometry.unary_union
        inward_geom = site_geom.buffer(-setback_m)
        setback_exclusion = site_geom.difference(inward_geom)
        if not setback_exclusion.is_empty:
            setback_gdf = gpd.GeoDataFrame(
                {"geometry": [setback_exclusion], "constraint_type": ["site_setback"]}, 
                crs=site_crs
            )
            all_exclusions.append(setback_gdf)

    if not all_exclusions:
        logger.info("No exclusions found. Entire site is buildable.")
        buildable = site_gdf.copy()
        buildable["area_ha"] = buildable.geometry.area / 10000
        return buildable, gpd.GeoDataFrame(columns=["geometry", "constraint_type"], crs=site_crs)

    # Merge all exclusions
    logger.info("Merging exclusion geometries...")
    combined_gdf = gpd.GeoDataFrame(pd.concat(all_exclusions, ignore_index=True), crs=site_crs)

    # Dissolve by constraint type for reporting
    dissolved_exclusions = combined_gdf.dissolve(by="constraint_type").reset_index()

    # Unified blocking geometry
    total_exclusion_geom = combined_gdf.geometry.unary_union

    logger.info("Subtracting exclusions from site boundary...")
    site_geom = site_gdf.geometry.unary_union

    # Calculate buildable area
    buildable_geom = site_geom.difference(total_exclusion_geom)

    # Explode multi-part polygons into individual areas
    buildable_gdf = gpd.GeoDataFrame(
        {"geometry": [buildable_geom]}, crs=site_crs
    ).explode(index_parts=False).reset_index(drop=True)

    # Remove tiny slivers (< 0.5 ha = 5000 sqm, too small for even 1 block)
    min_area_sqm = 5000
    buildable_gdf = buildable_gdf[buildable_gdf.geometry.area > min_area_sqm].copy()
    buildable_gdf["area_ha"] = buildable_gdf.geometry.area / 10000

    total_buildable = buildable_gdf["area_ha"].sum()
    total_site = site_gdf.geometry.area.sum() / 10000
    logger.info(f"Buildable area: {total_buildable:.2f} ha ({total_buildable/total_site*100:.1f}% of site)")

    return buildable_gdf, dissolved_exclusions
