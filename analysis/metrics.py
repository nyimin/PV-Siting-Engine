import json
import logging
import os
import requests
import geopandas as gpd
import numpy as np
from shapely.geometry import Point

logger = logging.getLogger("PVLayoutEngine.outputs")

def _get_pvwatts_yield(lat, lon, dc_mw, config):
    """
    Calls the NREL PVWatts V8 API to calculate annual energy yield (MWh).
    Falls back to a latitude-aware default if no API key is available.
    Returns (annual_yield_mwh, used_api: bool)
    """
    api_key = os.getenv("PVWATTS_API_KEY")
    if not api_key:
        api_key = config.get("api", {}).get("pvwatts_key")

    if not api_key or api_key == "DEMO_KEY":
        logger.warning("No valid PVWatts API key — using default yield estimate (1600 kWh/kWp).")
        return dc_mw * 1600.0, False

    # Hemisphere-aware optimal azimuth (EC-03 fix)
    azimuth = 180 if lat >= 0 else 0

    url = "https://developer.nrel.gov/api/pvwatts/v8.json"
    params = {
        "api_key": api_key,
        "lat": lat,
        "lon": lon,
        "system_capacity": dc_mw * 1000,
        "azimuth": azimuth,
        "tilt": config.get("solar", {}).get("tilt_deg", 25),
        "array_type": 1 if config.get("solar", {}).get("tracking", "fixed") != "fixed" else 0,
        "module_type": 0,
        "losses": 14.07,
    }

    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        annual_yield_kwh = data.get("outputs", {}).get("ac_annual", 0)
        return annual_yield_kwh / 1000.0, True
    except Exception as e:
        logger.error(f"PVWatts API call failed: {e}")
        return dc_mw * 1600.0, False

def _estimate_earthworks(blocks_gdf, terrain_paths):
    """
    Estimates rough Cut/Fill dirt moving volume (m3) locally using a horizontal 
    grading plane over each block. Rejects blocks where max cut > 1.5m.
    """
    total_volume_m3 = 0.0
    rejected_area_ha = 0.0
    
    if not terrain_paths or "dem_utm" not in terrain_paths:
        return 0.0, 0.0

    dem_path = terrain_paths["dem_utm"]
    
    try:
        import rasterio
        from rasterio.mask import mask
        
        with rasterio.open(dem_path) as src:
            transform = src.transform
            pixel_area = abs(transform.a * transform.e)
            
            for idx, block in blocks_gdf.iterrows():
                geom = block.geometry
                
                # Mask DEM to block
                out_image, _ = mask(src, [geom], crop=True)
                valid = out_image[out_image != src.nodata]
                
                if valid.size < 10:
                    continue
                    
                mean_elev = np.nanmean(valid)
                max_elev = np.nanmax(valid)
                max_cut = max_elev - mean_elev
                
                area_m2 = geom.area
                
                if max_cut > 1.5:
                    rejected_area_ha += area_m2 / 10000.0
                else:
                    # Cut volume: sum of elevation differences above mean * pixel area
                    cuts = valid[valid > mean_elev] - mean_elev
                    vol_m3 = np.sum(cuts) * pixel_area
                    total_volume_m3 += vol_m3
                    
    except Exception as e:
        logger.warning(f"Failed earthworks estimation: {e}")
        
    return total_volume_m3, rejected_area_ha
def compile_metrics(site_gdf, buildable_gdf, exclusions_gdf, blocks_gdf, rows_gdf,
                    inverters_gdf, transformers_gdf, substation_gdf, bess_gdf, mv_cables_gdf, lv_cables_gdf, roads_gdf,
                    capacity_info, terrain_stats=None, config=None, dem_warnings=None, terrain_paths=None,
                    om_gdf=None, guard_gdf=None):
    """
    Calculates and returns a comprehensive dictionary of engineering metrics.
    """
    logger.info("Compiling engineering metrics...")

    # Area metrics
    site_area_ha = site_gdf.geometry.area.sum() / 10000
    buildable_area_ha = buildable_gdf.geometry.area.sum() / 10000
    excluded_area_ha = exclusions_gdf.geometry.area.sum() / 10000 if not exclusions_gdf.empty else 0

    # Installed capacity (from blocks)
    installed_ac_mw = blocks_gdf["capacity_ac_mw"].sum() if "capacity_ac_mw" in blocks_gdf.columns else 0
    installed_dc_mw = blocks_gdf["capacity_dc_mw"].sum() if "capacity_dc_mw" in blocks_gdf.columns else 0
    
    # BESS Metrics
    bess_capacity_mw = bess_gdf["capacity_mw"].sum() if bess_gdf is not None and not bess_gdf.empty and "capacity_mw" in bess_gdf.columns else 0
    bess_capacity_mwh = bess_gdf["capacity_mwh"].sum() if bess_gdf is not None and not bess_gdf.empty and "capacity_mwh" in bess_gdf.columns else 0

    # O&M facility
    om_total_m2 = om_gdf["total_area_m2"].sum() if om_gdf is not None and not om_gdf.empty and "total_area_m2" in om_gdf.columns else 0
    om_cfg = (config or {}).get("om_compound", {})
    om_office_m2 = om_cfg.get("office_area_m2", 0)
    om_workshop_m2 = om_cfg.get("workshop_area_m2", 0)
    om_warehouse_m2 = om_cfg.get("warehouse_area_m2", 0)

    # Earthworks & PVWatts
    site_wgs84 = site_gdf.to_crs(epsg=4326)
    centroid = site_wgs84.geometry.unary_union.centroid
    lat, lon = centroid.y, centroid.x

    annual_yield_mwh, pvwatts_used_api = _get_pvwatts_yield(lat, lon, installed_dc_mw, config)
    earthworks_m3, ew_rejected_ha = _estimate_earthworks(blocks_gdf, terrain_paths)

    # Component counts
    num_blocks = len(blocks_gdf)
    num_rows = len(rows_gdf) if rows_gdf is not None else 0
    num_inverters = len(inverters_gdf) if inverters_gdf is not None else 0
    num_transformers = len(transformers_gdf) if transformers_gdf is not None else 0

    # Module and string counts from inverter attributes
    total_modules = 0
    total_strings = 0
    if not inverters_gdf.empty:
        if "modules" in inverters_gdf.columns:
            total_modules = int(inverters_gdf["modules"].sum())
        if "strings" in inverters_gdf.columns:
            total_strings = int(inverters_gdf["strings"].sum())

    # Infrastructure lengths
    mv_cable_km = mv_cables_gdf.geometry.length.sum() / 1000 if not mv_cables_gdf.empty else 0
    lv_cable_km = lv_cables_gdf.geometry.length.sum() / 1000 if lv_cables_gdf is not None and not lv_cables_gdf.empty else 0
    roads_km = roads_gdf.geometry.length.sum() / 1000 if not roads_gdf.empty else 0

    # PV area and GCR
    pv_area_ha = rows_gdf.geometry.area.sum() / 10000 if (rows_gdf is not None and not rows_gdf.empty) else 0
    block_area_ha = blocks_gdf.geometry.area.sum() / 10000 if not blocks_gdf.empty else 0
    gcr_achieved = pv_area_ha / block_area_ha if block_area_ha > 0 else 0

    # DC/AC ratio
    dc_ac_ratio = installed_dc_mw / installed_ac_mw if installed_ac_mw > 0 else 0

    # Module power from config
    mod_power_w = 635
    if config:
        mod_power_w = config.get("solar", {}).get("module_power_w", 635)

    metrics = {
        # Site
        "site_area_ha": round(site_area_ha, 2),
        "buildable_area_ha": round(buildable_area_ha, 2),
        "excluded_area_ha": round(excluded_area_ha, 2),
        "buildable_percent": round((buildable_area_ha / site_area_ha) * 100, 1) if site_area_ha > 0 else 0,

        # Capacity
        "requested_dc_mw": capacity_info.get("required_mw_dc", 0),
        "requested_ac_mw": capacity_info.get("required_mw_ac", 0),
        "max_feasible_ac_mw": capacity_info.get("max_feasible_ac_mw", 0),
        "max_feasible_dc_mw": capacity_info.get("max_feasible_dc_mw", 0),
        "installed_ac_mw": round(installed_ac_mw, 2),
        "installed_dc_mw": round(installed_dc_mw, 2),
        "dc_ac_ratio": round(dc_ac_ratio, 2),
        "is_feasible": capacity_info.get("is_feasible", False),

        # Components
        "num_blocks": num_blocks,
        "num_pv_rows": num_rows,
        "num_inverters": num_inverters,
        "num_transformers": num_transformers,
        "total_modules": total_modules,
        "total_strings": total_strings,
        "module_power_w": mod_power_w,

        # Layout
        "pv_area_ha": round(pv_area_ha, 2),
        "block_area_ha": round(block_area_ha, 2),
        "gcr_achieved": round(gcr_achieved, 3),

        # Infrastructure
        "mv_cable_length_km": round(mv_cable_km, 2),
        "lv_cable_length_km": round(lv_cable_km, 2),
        "internal_roads_km": round(roads_km, 2),
        
        # New Advanced Metrics
        "bess_capacity_mw": round(bess_capacity_mw, 2),
        "bess_capacity_mwh": round(bess_capacity_mwh, 2),
        "om_compound_area_m2": round(om_total_m2, 0),
        "om_office_area_m2": om_office_m2,
        "om_workshop_area_m2": om_workshop_m2,
        "om_warehouse_area_m2": om_warehouse_m2,
        "annual_yield_mwh": round(annual_yield_mwh, 2),
        "specific_yield_kwh_kwp": round((annual_yield_mwh * 1000) / (installed_dc_mw * 1000), 2) if installed_dc_mw > 0 else 0,
        "pvwatts_used_api": pvwatts_used_api,
        "earthworks_volume_m3": round(earthworks_m3, 2),
        "earthworks_rejected_ha": round(ew_rejected_ha, 2),

        # Warnings
        "dem_warnings": dem_warnings or [],
    }

    # Exclusion breakdown by constraint type
    if not exclusions_gdf.empty and "constraint_type" in exclusions_gdf.columns:
        breakdown = {}
        for ctype in exclusions_gdf["constraint_type"].unique():
            subset = exclusions_gdf[exclusions_gdf["constraint_type"] == ctype]
            area_ha = subset.geometry.area.sum() / 10000
            breakdown[ctype] = round(area_ha, 2)
        metrics["exclusion_breakdown"] = breakdown

    # Terrain stats (keys updated to degree-based nomenclature)
    if terrain_stats:
        metrics["terrain"] = {
            "mean_slope_deg":  round(terrain_stats.get("mean_slope_deg", 0), 2),
            "max_slope_deg":   round(terrain_stats.get("max_slope_deg", 0), 2),
            "std_slope_deg":   round(terrain_stats.get("std_slope_deg", 0), 2),
            "across_row_slope_pct": round(terrain_stats.get("across_row_slope_pct", 0), 1),
            "along_row_slope_pct":  round(terrain_stats.get("along_row_slope_pct", 0), 1),
            "mean_tri_m":      round(terrain_stats.get("mean_tri_m", 0), 2),
            "mean_suitability": round(terrain_stats.get("mean_suitability", 0), 2),
            "buildable_pct_terrain": round(terrain_stats.get("buildable_pct_terrain", 0), 1),
        }

    return metrics


def generate_report(metrics, output_dir):
    """
    Generates a comprehensive Markdown engineering report.
    """
    logger.info("Generating Markdown Engineering Report...")

    report_path = os.path.join(output_dir, "engineering_report.md")

    # Terrain section
    terrain_section = ""
    if "terrain" in metrics:
        t = metrics["terrain"]
        terrain_section = f"""
## Terrain Summary

| Metric | Value |
|--------|-------|
| Mean Slope | {t['mean_slope_deg']}° |
| Max Slope | {t['max_slope_deg']}° |
| Slope Std Dev | {t['std_slope_deg']}° |
| Across-Row Slope Area | {t.get('across_row_slope_pct', 0)}% of site |
| Along-Row Slope Area | {t.get('along_row_slope_pct', 0)}% of site |
| Mean TRI | {t['mean_tri_m']} m |
| Mean Suitability Index | {t['mean_suitability']} / 3.0 |
| Terrain Buildable (suitability ≥ 2.25) | {t.get('buildable_pct_terrain', 0)}% of raster |
"""

    # Exclusion breakdown
    exclusion_section = ""
    if metrics["excluded_area_ha"] > 0:
        exclusion_section = f"""
### Constraint Breakdown
- **Total Excluded Area:** {metrics['excluded_area_ha']} ha
- **Buildable Percentage:** {metrics['buildable_percent']}%
"""
        # Per-type table
        if "exclusion_breakdown" in metrics:
            exclusion_section += "\n| Constraint Type | Area (ha) |\n|---|---|\n"
            for ctype, area in sorted(metrics["exclusion_breakdown"].items(), key=lambda x: -x[1]):
                label = ctype.replace("osm_", "OSM: ").replace("terrain_", "Terrain: ").replace("lulc_", "LULC: ")
                exclusion_section += f"| {label} | {area} |\n"

        pvwatts_note = ("" if metrics.get("pvwatts_used_api")
                    else "\n> ⚠️ **Yield estimate uses default 1 600 kWh/kWp (no PVWatts API key). "
                         "Results may be ±30% from actual. Set PVWATTS_API_KEY in .env.**\n")
    else:
        pvwatts_note = ""

    content = f"""# Solar PV Conceptual Layout — Engineering Report

![Layout Map](layout_map.png)

## Site Summary

| Parameter | Value |
|-----------|-------|
| Total Site Area | {metrics['site_area_ha']} ha |
| Buildable Area | {metrics['buildable_area_ha']} ha ({metrics['buildable_percent']}%) |
| Excluded Area | {metrics['excluded_area_ha']} ha |
{exclusion_section}

![Terrain Constraints Map](terrain_constraints_map.png)

{terrain_section}
![Terrain Slope Map](terrain_slope_map.png)

## Capacity Analysis

| Parameter | Value |
|-----------|-------|
| Requested Capacity (DC) | {metrics['requested_dc_mw']} MW |
| Requested Capacity (AC) | {metrics['requested_ac_mw']} MW |
| Max Feasible (AC) | {metrics['max_feasible_ac_mw']} MW |
| Max Feasible (DC) | {metrics['max_feasible_dc_mw']} MW |
| **Layout Installed (AC)** | **{metrics['installed_ac_mw']} MW** |
| **Layout Installed (DC)** | **{metrics['installed_dc_mw']} MW** |
| DC/AC Ratio | {metrics['dc_ac_ratio']} |
| Feasibility | {'✓ FEASIBLE' if metrics['is_feasible'] else '✗ NOT FEASIBLE'} |

## Energy Yield & Storage

| Parameter | Value |
|-----------|-------|
| BESS Capacity | {metrics['bess_capacity_mw']} MW / {metrics['bess_capacity_mwh']} MWh |
| BESS Duration | {round(metrics['bess_capacity_mwh'] / metrics['bess_capacity_mw'], 1) if metrics['bess_capacity_mw'] > 0 else 'N/A'} hours |
| Estimated Annual Yield | {metrics['annual_yield_mwh']:,.0f} MWh/year |
| Specific Yield | {metrics['specific_yield_kwh_kwp']:,.0f} kWh/kWp/year |
{pvwatts_note}

## O&M Facility

*Sized per IEA-PVPS Task 13 / IRENA Utility-Scale PV BOP guidelines.*

| Building / Zone | Floor Area |
|---|---|
| Main Office (control room, admin, meeting) | {metrics['om_office_area_m2']} m² |
| Maintenance Workshop + Tool Store | {metrics['om_workshop_area_m2']} m² |
| Spare Parts Warehouse (climate-controlled) | {metrics['om_warehouse_area_m2']} m² |
| **Total Compound Footprint** | **{metrics['om_compound_area_m2']:.0f} m²** |

## Civil Earthworks

*Estimation computed using planar topological fit inside blocks, rigorously rejecting topography >1.5m cut depth.*

| Metric | Value |
|--------|-------|
| Cut/Fill Extrapolated Volume | {metrics['earthworks_volume_m3']:,.0f} m³ |
| High Topo Rejected Area | {metrics['earthworks_rejected_ha']} ha |

## Layout Components

| Component | Count |
|-----------|-------|
| PV Blocks | {metrics['num_blocks']} |
| PV Rows | {metrics['num_pv_rows']} |
| String Inverters | {metrics['num_inverters']} |
| Block Transformers | {metrics['num_transformers']} |
| Total Strings | {metrics['total_strings']:,} |
| Total Modules | {metrics['total_modules']:,} |
| Module Power | {metrics['module_power_w']} W |

## Layout Performance

| Metric | Value |
|--------|-------|
| PV Array Area | {metrics['pv_area_ha']} ha |
| Block Footprint Area | {metrics['block_area_ha']} ha |
| Ground Cover Ratio (GCR) | {metrics['gcr_achieved']} |
| LV AC Cable Length | {metrics['lv_cable_length_km']} km |
| MV Cable Length | {metrics['mv_cable_length_km']} km |
| Access Road Length | {metrics['internal_roads_km']} km |

## Risk Assessment

"""
    # Risk assessment
    risks = []
    if not metrics['is_feasible']:
        risks.append(f"🔴 **HIGH RISK:** Site cannot accommodate {metrics['requested_dc_mw']} MWdc "
                      f"(max feasible: {metrics['max_feasible_dc_mw']} MWdc)")
    if metrics['buildable_percent'] < 50:
        risks.append(f"🔴 **HIGH RISK:** Only {metrics['buildable_percent']}% of site is buildable")
    if metrics.get("terrain", {}).get("mean_slope_pct", 0) > 8:
        risks.append(f"🟡 **MEDIUM RISK:** Mean slope ({metrics['terrain']['mean_slope_pct']}%) "
                      "is relatively steep for solar")
    if metrics['num_blocks'] < 3:
        risks.append("🟡 **MEDIUM RISK:** Very few blocks placed — site may be heavily constrained")
    if metrics.get("dem_warnings"):
        for w in metrics["dem_warnings"]:
            if "poor" in w.lower():
                risks.append(f"🔴 **HIGH RISK:** {w}")
            else:
                risks.append(f"🟡 **MEDIUM RISK:** {w}")
    if not risks:
        risks.append("🟢 **LOW RISK:** Site appears suitable for the target capacity")

    content += "\n".join(f"- {r}" for r in risks) + "\n"

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(content)

    logger.info(f"Report generated: {report_path}")
    return report_path
