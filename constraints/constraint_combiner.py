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
        "rivers_m":    50,
        "roads_m":     20,
        "railways_m":  50,
        "power_lines_m": 30,
    }

    for layer_name, gdf in osm_dict.items():
        if gdf.empty:
            continue

        if gdf.crs != site_crs:
            gdf = gdf.to_crs(site_crs)

        config_key = buffer_map.get(layer_name, None)
        if config_key:
            buffer_dist = buffers.get(config_key, default_buffers.get(config_key, 30))
        else:
            buffer_dist = 30

        logger.info(f"  Applying {buffer_dist}m buffer to {layer_name} ({len(gdf)} features)")
        buffered_gdf = gdf.copy()
        buffered_gdf["geometry"] = buffered_gdf.geometry.buffer(buffer_dist)
        buffered_gdf["constraint_type"] = f"osm_{layer_name}"
        all_exclusions.append(buffered_gdf[["geometry", "constraint_type"]])

    if all_exclusions:
        return gpd.GeoDataFrame(pd.concat(all_exclusions, ignore_index=True), crs=site_crs)
    else:
        return gpd.GeoDataFrame(columns=["geometry", "constraint_type"], crs=site_crs)


def raster_to_polygons(raster_path, threshold, crs, constraint_name="slope", above=True):
    """
    Converts a thresholded raster into exclusion polygons.
    above=True  → exclude pixels where data > threshold
    above=False → exclude pixels where data < threshold
    """
    logger.info(f"  Converting {constraint_name} raster to exclusion polygons (threshold={threshold})...")
    with rasterio.open(raster_path) as src:
        data = src.read(1)
        transform = src.transform

        mask = (data > threshold) if above else (data < threshold)

        excluded_pct = np.sum(mask) / mask.size * 100
        logger.info(f"  {constraint_name}: {excluded_pct:.1f}% of raster pixels excluded")

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


def _aspect_exclusion_mask(slope_path, aspect_path, config, site_crs):
    """
    Generates a hard exclusion polygon for north-facing terrain steeper than
    the configured threshold (north_facing_exclusion_slope_deg).

    Northern hemisphere: exclude aspect in [292.5°, 360°] ∪ [0°, 67.5°]
                         where slope ≥ north_facing_exclusion_slope_deg
    Southern hemisphere: exclude aspect in [112.5°, 247.5°] (S-facing)
                         where slope ≥ threshold
    """
    tc = config.get("terrain", {})
    nf_slope_thresh = tc.get("north_facing_exclusion_slope_deg", 5.0)
    site_lat = config.get("_site_latitude", 15.0)
    is_northern = site_lat >= 0

    if not (slope_path and os.path.exists(slope_path) and
            aspect_path and os.path.exists(aspect_path)):
        logger.warning("  Aspect exclusion skipped — slope or aspect raster missing.")
        return gpd.GeoDataFrame(columns=["geometry", "constraint_type"], crs=site_crs)

    logger.info(f"  Generating aspect hard exclusion "
                f"(north-facing, slope≥{nf_slope_thresh}°, lat={site_lat:.1f}°)...")

    with rasterio.open(slope_path) as s_src, rasterio.open(aspect_path) as a_src:
        slope_deg  = s_src.read(1).astype(np.float32)
        aspect_deg = a_src.read(1).astype(np.float32)
        transform  = s_src.transform
        crs_raster = s_src.crs

    steep_enough = slope_deg >= nf_slope_thresh

    if is_northern:
        # North-facing: 292.5°–360° or 0°–67.5°
        north_facing = ((aspect_deg >= 292.5) | (aspect_deg < 67.5))
    else:
        # South-facing (unfavourable in southern hemisphere): 112.5°–247.5°
        north_facing = ((aspect_deg >= 112.5) & (aspect_deg < 247.5))

    excl_mask = (steep_enough & north_facing).astype(np.uint8)

    excluded_pct = np.sum(excl_mask) / excl_mask.size * 100
    logger.info(f"  North-facing exclusion: {excluded_pct:.1f}% of raster")

    if excl_mask.max() == 0:
        return gpd.GeoDataFrame(columns=["geometry", "constraint_type"], crs=site_crs)

    results = [
        {"properties": {"constraint_type": "terrain_north_facing"}, "geometry": s}
        for s, v in shapes(excl_mask, mask=excl_mask, transform=transform)
        if v == 1
    ]

    if not results:
        return gpd.GeoDataFrame(columns=["geometry", "constraint_type"], crs=site_crs)

    gdf = gpd.GeoDataFrame.from_features(results, crs=crs_raster)
    if str(gdf.crs) != str(site_crs):
        gdf = gdf.to_crs(site_crs)
    logger.info(f"  North-facing exclusion: {len(gdf)} polygons generated.")
    return gdf


def combine_constraints(site_gdf, osm_exclusions, terrain_analysis_paths, config):
    """
    Combines all constraints and subtracts them from the site boundary to yield
    the buildable area.

    Exclusion layers applied (in order):
      1. OSM infrastructure buffers (roads, water, buildings, power, railways)
      2. LULC exclusions (merged in main_pipeline before this call)
      3. Slope > max_slope_deg (hard terrain exclusion)
      4. North-facing slope ≥ north_facing_exclusion_slope_deg (BD-01 fix)
      5. TRI > max_tri_m (rough terrain)
      6. TPI < max_tpi_valley_m (deep valleys, ravines, drainage channels)
      7. |Curvature| > max_curvature (very concave/convex micro-terrain)
      8. Site boundary inward setback
      9. Forest edge buffer (applied to lulc_Tree cover exclusion polygons)
    """
    logger.info("Combining all constraints...")
    site_crs = site_gdf.crs
    tc = config.get("terrain", {})
    buffers_cfg = config.get("buffers", {})

    all_exclusions = [osm_exclusions] if not osm_exclusions.empty else []

    # 1. Slope exclusion  (slope in DEGREES now — audit BD-02 fix)
    slope_path = terrain_analysis_paths.get("slope")
    max_slope_deg = tc.get("max_slope_deg", 15.0)
    if slope_path and os.path.exists(slope_path):
        slope_exclusions = raster_to_polygons(
            slope_path, max_slope_deg, site_crs, "steep_slope", above=True
        )
        if not slope_exclusions.empty:
            all_exclusions.append(slope_exclusions)

    # 2. North-facing slope exclusion  (BD-01 — new)
    aspect_path = terrain_analysis_paths.get("aspect")
    nf_exclusions = _aspect_exclusion_mask(slope_path, aspect_path, config, site_crs)
    if not nf_exclusions.empty:
        all_exclusions.append(nf_exclusions)

    # 3. TRI (Terrain Ruggedness Index) exclusion
    tri_path = terrain_analysis_paths.get("tri")
    max_tri = tc.get("max_tri_m", 1.5)
    if tri_path and os.path.exists(tri_path):
        tri_exclusions = raster_to_polygons(
            tri_path, max_tri, site_crs, "roughness_tri", above=True
        )
        if not tri_exclusions.empty:
            all_exclusions.append(tri_exclusions)

    # 3.5 TPI (Topographic Position Index) valley extraction (drainage/waterways)
    tpi_path = terrain_analysis_paths.get("tpi")
    # Negative TPI means valley. The threshold should be a negative number, e.g. -2.0m
    max_tpi_valley = tc.get("max_tpi_valley_m", -2.0)
    if tpi_path and os.path.exists(tpi_path):
        # We want to exclude areas where TPI < threshold (i.e. very negative -> deep valley)
        tpi_exclusions = raster_to_polygons(
            tpi_path, max_tpi_valley, site_crs, "terrain_valley_tpi", above=False
        )
        if not tpi_exclusions.empty:
            all_exclusions.append(tpi_exclusions)

    # 4. Curvature exclusion  (BD-05 — was only in block_generator before)
    curvature_path = terrain_analysis_paths.get("curvature")
    max_curv = tc.get("max_curvature", 0.4)
    if curvature_path and os.path.exists(curvature_path):
        # Exclude both very concave and very convex terrain
        # We build two masks and merge them
        with rasterio.open(curvature_path) as src:
            curv_data  = src.read(1).astype(np.float32)
            curv_transform = src.transform
            curv_raster_crs = src.crs

        curv_mask = (np.abs(curv_data) > max_curv).astype(np.uint8)
        curv_pct  = np.sum(curv_mask) / curv_mask.size * 100
        logger.info(f"  Curvature exclusion: {curv_pct:.1f}% of raster pixels excluded (|curv|>{max_curv})")

        if curv_mask.max() > 0:
            curv_results = [
                {"properties": {"constraint_type": "terrain_curvature"}, "geometry": s}
                for s, v in shapes(curv_mask, mask=curv_mask, transform=curv_transform)
                if v == 1
            ]
            if curv_results:
                curv_gdf = gpd.GeoDataFrame.from_features(curv_results, crs=curv_raster_crs)
                if str(curv_gdf.crs) != str(site_crs):
                    curv_gdf = curv_gdf.to_crs(site_crs)
                all_exclusions.append(curv_gdf)

    # 5. Site boundary inward setback  (BD-03 — now reads from config)
    setback_m = buffers_cfg.get("site_boundary_m", 10)
    if setback_m > 0:
        logger.info(f"  Applying {setback_m}m inward setback from site boundary...")
        site_geom = site_gdf.geometry.unary_union
        inward_geom = site_geom.buffer(-setback_m)
        setback_exclusion = site_geom.difference(inward_geom)
        if not setback_exclusion.is_empty:
            setback_gdf = gpd.GeoDataFrame(
                {"geometry": [setback_exclusion], "constraint_type": ["site_setback"]},
                crs=site_crs
            )
            all_exclusions.append(setback_gdf)

    # 6. Forest edge buffer  (BD-06 — buffer lulc_Tree cover exclusions)
    forest_buffer_m = buffers_cfg.get("forest_buffer_m", 50)
    if forest_buffer_m > 0 and all_exclusions:
        forest_layers = [
            ex for ex in all_exclusions
            if "constraint_type" in ex.columns and
               ex["constraint_type"].str.contains("Tree cover", na=False).any()
        ]
        if forest_layers:
            logger.info(f"  Applying {forest_buffer_m}m edge buffer to forest exclusions (BD-06)...")
            for fl in forest_layers:
                extra = fl.copy()
                extra["geometry"] = extra.geometry.buffer(forest_buffer_m)
                extra["constraint_type"] = "forest_edge_buffer"
                all_exclusions.append(extra)

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
    buildable_geom = site_geom.difference(total_exclusion_geom)

    # Explode multi-part polygons
    buildable_gdf = gpd.GeoDataFrame(
        {"geometry": [buildable_geom]}, crs=site_crs
    ).explode(index_parts=False).reset_index(drop=True)

    # Minimum patch size filter  (raised from 0.5 ha to 5 ha — industry standard)
    min_patch_ha = config.get("buildable_area", {}).get("min_patch_ha", 5.0)
    min_area_sqm = min_patch_ha * 10000
    buildable_gdf = buildable_gdf[buildable_gdf.geometry.area > min_area_sqm].copy()
    buildable_gdf["area_ha"] = buildable_gdf.geometry.area / 10000

    total_buildable = buildable_gdf["area_ha"].sum()
    total_site = site_gdf.geometry.area.sum() / 10000
    buildable_pct = total_buildable / total_site * 100 if total_site > 0 else 0

    logger.info(f"Buildable area: {total_buildable:.2f} ha "
                f"({buildable_pct:.1f}% of site, min patch ≥{min_patch_ha} ha)")

    # Log exclusion breakdown
    for ctype in dissolved_exclusions["constraint_type"].unique():
        subset = dissolved_exclusions[dissolved_exclusions["constraint_type"] == ctype]
        area_ha = subset.geometry.area.sum() / 10000
        logger.info(f"  Exclusion — {ctype}: {area_ha:.2f} ha")

    return buildable_gdf, dissolved_exclusions
