import os
import sys
import logging
import argparse
import geopandas as gpd
from pyproj import CRS

from utils.config_loader import load_config, setup_logging
from terrain.dem_downloader import fetch_dem
from constraints.worldcover_downloader import fetch_worldcover, worldcover_exclusion_mask
from constraints.osm_downloader import fetch_osm_constraints
from terrain.terrain_analysis import process_terrain, _auto_utm_epsg
from constraints.constraint_combiner import process_osm_constraints as process_osm, combine_constraints
from analysis.capacity_estimator import calculate_feasible_capacity
from layout.block_generator import generate_solar_blocks
from layout.bop_placement import place_inverters_and_transformers
from layout.routing import route_mv_cables_and_roads
from layout.substation_placement import reserve_bop_zone
from analysis.metrics import compile_metrics, generate_report
from visualization.map_generator import save_gis_layers, create_layout_map, create_interactive_map, create_terrain_maps


def _determine_utm_crs(site_gdf):
    """Auto-detect the appropriate UTM CRS from site centroid."""
    site_wgs84 = site_gdf.to_crs(epsg=4326)
    centroid = site_wgs84.geometry.unary_union.centroid
    utm_epsg = _auto_utm_epsg(centroid.x, centroid.y)
    return f"EPSG:{utm_epsg}"


def run_pipeline(input_boundary_path, requested_mw, config_path="config/config.yaml"):
    """Runs the end-to-end solar layout engine pipeline."""

    # 0. Setup
    config = load_config(config_path)
    logger = setup_logging()
    logger.info("=" * 60)
    logger.info("  PV Layout Engine Pipeline — Starting")
    logger.info("=" * 60)

    cache_dir = config.get("data", {}).get("cache_dir", "data/cache")
    output_dir = "outputs"
    os.makedirs(output_dir, exist_ok=True)

    from utils.caching import log_cache_status
    log_cache_status(cache_dir)

    # 1. Load Input Boundary
    logger.info(f"Loading site boundary from {input_boundary_path}")
    try:
        site_gdf = gpd.read_file(input_boundary_path)
        if site_gdf.crs is None:
            raise ValueError("Input boundary missing CRS. Please provide a valid georeferenced file.")
    except Exception as e:
        logger.error(f"Failed to load input boundary: {e}")
        sys.exit(1)

    # Auto-detect UTM CRS
    output_crs_setting = config.get("project", {}).get("output_crs", "auto")
    if output_crs_setting == "auto":
        output_crs = _determine_utm_crs(site_gdf)
        logger.info(f"Auto-detected UTM CRS: {output_crs}")
    else:
        output_crs = output_crs_setting

    if str(site_gdf.crs) != output_crs:
        logger.info(f"Reprojecting site from {site_gdf.crs} to {output_crs}")
        site_gdf = site_gdf.to_crs(output_crs)

    site_area_ha = site_gdf.geometry.area.sum() / 10000
    logger.info(f"Site area: {site_area_ha:.2f} ha")

    # ========== PHASE 1: DATA ACQUISITION ==========
    logger.info("=" * 50)
    logger.info("  PHASE 1: DATA ACQUISITION")
    logger.info("=" * 50)

    dem_path, dem_warnings = fetch_dem(site_gdf, cache_dir=os.path.join(cache_dir, "dem"), config=config)
    lulc_path = fetch_worldcover(site_gdf, cache_dir=os.path.join(cache_dir, "worldcover"))
    osm_raw = fetch_osm_constraints(site_gdf, cache_dir=os.path.join(cache_dir, "osm"))
    osm_exclusions = process_osm(osm_raw, site_gdf.crs, config)

    # ========== PHASE 2: TERRAIN ANALYSIS ==========
    logger.info("=" * 50)
    logger.info("  PHASE 2: TERRAIN ANALYSIS")
    logger.info("=" * 50)

    terrain_outputs_dir = os.path.join("data", "processed", "terrain")
    terrain_paths = {}
    terrain_stats = None

    if dem_path:
        terrain_paths = process_terrain(dem_path, terrain_outputs_dir, config)
        terrain_stats = terrain_paths.get("stats")

        # Reproject site to terrain CRS if different
        terrain_crs = terrain_paths.get("utm_epsg")
        if terrain_crs and str(site_gdf.crs) != str(terrain_crs):
            logger.info(f"Reprojecting site to terrain UTM CRS: {terrain_crs}")
            site_gdf = site_gdf.to_crs(terrain_crs)
            output_crs = str(terrain_crs)
            if not osm_exclusions.empty:
                osm_exclusions = osm_exclusions.to_crs(terrain_crs)
    else:
        logger.warning("No DEM retrieved. Skipping terrain analysis constraints.")

    # ========== PHASE 3: CONSTRAINTS ==========
    logger.info("=" * 50)
    logger.info("  PHASE 3: CONSTRAINTS & BUILDABLE AREA")
    logger.info("=" * 50)

    # Extract WorldCover LULC exclusion polygons
    lulc_exclusions = worldcover_exclusion_mask(
        lulc_path, site_gdf, output_path=os.path.join("data", "processed", "lulc_exclusions.gpkg")
    )

    # Merge OSM + LULC exclusions
    import pandas as pd
    if not lulc_exclusions.empty and not osm_exclusions.empty:
        merged_exclusions = gpd.GeoDataFrame(
            pd.concat([osm_exclusions, lulc_exclusions], ignore_index=True), crs=site_gdf.crs
        )
    elif not lulc_exclusions.empty:
        merged_exclusions = lulc_exclusions
    else:
        merged_exclusions = osm_exclusions

    buildable_gdf, exclusions_gdf = combine_constraints(site_gdf, merged_exclusions, terrain_paths, config)

    if buildable_gdf.empty:
        logger.error("Buildable area is empty after applying constraints. Cannot proceed.")
        sys.exit(0)

    # ========== PHASE 4: CAPACITY FEASIBILITY ==========
    logger.info("=" * 50)
    logger.info("  PHASE 4: CAPACITY FEASIBILITY")
    logger.info("=" * 50)

    total_buildable_ha = buildable_gdf["area_ha"].sum()
    capacity_info = calculate_feasible_capacity(total_buildable_ha, requested_mw, config)

    # ========== PHASE 5: BOP ZONE RESERVATION (must run before panel layout) ==========
    logger.info("=" * 50)
    logger.info("  PHASE 5: BOP ZONE RESERVATION")
    logger.info("=" * 50)

    # Reserve land for substation, BESS, O&M compound BEFORE placing any PV panels.
    # This mirrors industry tools (PVcase, Helioscope): select substation point →
    # carve BOP zone → pass remaining buildable area to the block generator.
    slope_path = terrain_paths.get("slope") if terrain_paths else None
    sub_pt, substation_gdf, bess_gdf, om_gdf, guard_gdf, bop_zone_gdf, reduced_buildable_gdf = reserve_bop_zone(
        site_gdf, buildable_gdf, exclusions_gdf, config, slope_path=slope_path
    )

    # ========== PHASE 6: LAYOUT GENERATION (on BOP-free buildable area) ==========
    logger.info("=" * 50)
    logger.info("  PHASE 6: LAYOUT GENERATION")
    logger.info("=" * 50)

    blocks_gdf, rows_gdf = generate_solar_blocks(reduced_buildable_gdf, config, terrain_paths)

    # ── Post-generation BOP guard: drop any PV rows whose centroid falls inside
    #    the BOP zone. The block generator works at polygon level and edge rows
    #    can straddle the BOP boundary even after buildable area subtraction.
    try:
        from shapely.ops import unary_union as _uu
        bop_union = _uu(
            list(substation_gdf.geometry) +
            list(bess_gdf.geometry) +
            list(om_gdf.geometry) +
            list(guard_gdf.geometry)
        )
        before = len(rows_gdf)
        rows_gdf = rows_gdf[~rows_gdf.geometry.centroid.intersects(bop_union)].copy()
        removed = before - len(rows_gdf)
        if removed > 0:
            logger.info(f"  Removed {removed} PV rows that overlapped BOP zone.")
            # Also purge orphaned blocks (blocks with no rows left)
            valid_blocks = set(rows_gdf["block_id"].unique())
            blocks_gdf = blocks_gdf[blocks_gdf["block_id"].isin(valid_blocks)].copy()
    except Exception as e:
        logger.warning(f"  BOP row guard failed: {e}")


    # ========== PHASE 7: BALANCE OF PLANT EQUIPMENT ==========
    logger.info("=" * 50)
    logger.info("  PHASE 7: BALANCE OF PLANT EQUIPMENT")
    logger.info("=" * 50)

    inverters_gdf, transformers_gdf, lv_cables_gdf = place_inverters_and_transformers(blocks_gdf, rows_gdf, config)

    roads_gdf, mv_cables_gdf = route_mv_cables_and_roads(
        inverters_gdf, transformers_gdf, sub_pt, blocks_gdf, config,
        terrain_paths=terrain_paths, exclusions_gdf=exclusions_gdf
    )

    # ========== PHASE 8: EXPORTS & REPORTING ==========
    logger.info("=" * 50)
    logger.info("  PHASE 7: EXPORTS & REPORTING")
    logger.info("=" * 50)

    # Save GIS layers
    save_gis_layers(output_dir,
                    site_boundary=site_gdf,
                    buildable_area=buildable_gdf,
                    exclusions=exclusions_gdf,
                    solar_blocks=blocks_gdf,
                    pv_rows=rows_gdf,
                    inverters=inverters_gdf,
                    transformers=transformers_gdf,
                    substation=substation_gdf,
                    bess=bess_gdf,
                    om_compound=om_gdf,
                    guard_house=guard_gdf,
                    internal_roads=roads_gdf,
                    mv_cables=mv_cables_gdf,
                    lv_cables=lv_cables_gdf)

    # Generate Layout Map
    create_layout_map(site_gdf, buildable_gdf, blocks_gdf, rows_gdf,
                      inverters_gdf, transformers_gdf, substation_gdf, bess_gdf,
                      roads_gdf, mv_cables_gdf, lv_cables_gdf, output_dir)

    # Generate Interactive HTML Map
    create_interactive_map(site_gdf, buildable_gdf, blocks_gdf, rows_gdf,
                           inverters_gdf, transformers_gdf, substation_gdf, bess_gdf,
                           roads_gdf, mv_cables_gdf, lv_cables_gdf, output_dir,
                           om_gdf=om_gdf, guard_gdf=guard_gdf)

    # Generate Terrain Maps
    if terrain_paths:
        create_terrain_maps(terrain_paths, site_gdf, output_dir)

    metrics = compile_metrics(
        site_gdf, buildable_gdf, exclusions_gdf, blocks_gdf, rows_gdf,
        inverters_gdf, transformers_gdf, substation_gdf, bess_gdf, mv_cables_gdf, lv_cables_gdf, roads_gdf,
        capacity_info, terrain_stats=terrain_stats, config=config,
        dem_warnings=dem_warnings, terrain_paths=terrain_paths,
        om_gdf=om_gdf, guard_gdf=guard_gdf
    )

    generate_report(metrics, output_dir)

    logger.info("=" * 60)
    logger.info("  PV Layout Engine Pipeline — COMPLETE")
    logger.info(f"  Outputs: {output_dir}/")
    logger.info(f"  Blocks: {len(blocks_gdf)}, Rows: {len(rows_gdf)},")
    logger.info(f"  Installed: {metrics.get('installed_ac_mw', 0)} MWac / {metrics.get('installed_dc_mw', 0)} MWdc")
    logger.info("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Solar PV conceptual layout engine.")
    parser.add_argument("boundary", help="Path to input site boundary file (GeoJSON/Shapefile/GPKG).")
    parser.add_argument("capacity_mw", type=float, help="Requested capacity in MW DC.")
    parser.add_argument("--config", default="config/config.yaml", help="Path to config file.")

    args = parser.parse_args()

    run_pipeline(args.boundary, args.capacity_mw, args.config)
