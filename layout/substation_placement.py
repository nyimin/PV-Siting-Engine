import logging
import numpy as np
import geopandas as gpd
from shapely.geometry import Point, box, Polygon
from shapely.ops import linemerge, unary_union

logger = logging.getLogger("PVLayoutEngine.bop")


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _sample_slope_at_point(pt, slope_path):
    """Samples slope value at a point from the slope raster."""
    try:
        import rasterio
        with rasterio.open(slope_path) as src:
            row, col = src.index(pt.x, pt.y)
            if 0 <= row < src.height and 0 <= col < src.width:
                val = src.read(1, window=rasterio.windows.Window(col, row, 1, 1))[0, 0]
                return float(val) if not np.isnan(val) else None
    except Exception:
        pass
    return None


def _select_substation_point(site_gdf, buildable_area_gdf, exclusions_gdf, config, slope_path=None):
    """
    Selects the best substation location on the site boundary using multi-criteria
    evaluation. Five equally-weighted criteria (20% each):
      1. Flat terrain (IEC 61936-1 grading requirement)
      2. Proximity to site centroid (shorter MV homerun distances)
      3. Proximity to existing roads (cheaper sealed access road tie-in)
      4. Distance from water / flood risk (ADB environmental safeguard)
      5. Located on or very near buildable area (to avoid complex unbuildable terrains)
    Returns: best_pt (Point), best_slope (float or None)
    """
    site_geom = site_gdf.geometry.unary_union
    site_centroid = site_geom.centroid
    
    # We want points along the site boundary that also overlap the buildable area.
    # To have candidates to evaluate, we will sample the boundary.
    boundary = site_geom.boundary

    roads_geom = None
    water_geom = None
    if exclusions_gdf is not None and not exclusions_gdf.empty and "constraint_type" in exclusions_gdf.columns:
        roads_subset = exclusions_gdf[exclusions_gdf["constraint_type"] == "osm_roads"]
        if not roads_subset.empty:
            roads_geom = roads_subset.geometry.unary_union
        water_subset = exclusions_gdf[exclusions_gdf["constraint_type"].isin(
            ["osm_water", "lulc_Permanent water bodies"])]
        if not water_subset.empty:
            water_geom = water_subset.geometry.unary_union

    if boundary.is_empty:
        return site_centroid, None

    if boundary.geom_type == "MultiLineString":
        boundary = linemerge(boundary)
        if boundary.geom_type == "MultiLineString":
            boundary = max(list(boundary.geoms), key=lambda g: g.length)

    boundary_length = boundary.length
    n_candidates = max(30, int(boundary_length / 50))

    candidates = []
    for i in range(n_candidates):
        try:
            pt = boundary.interpolate(i / n_candidates, normalized=True)
            candidates.append(pt)
        except Exception:
            continue

    if not candidates:
        return boundary.interpolate(0.5, normalized=True), None

    buildable_geom = None
    if buildable_area_gdf is not None and not buildable_area_gdf.empty:
        buildable_geom = buildable_area_gdf.geometry.unary_union

    best_score = -1
    best_pt = None
    best_slope = None

    for pt in candidates:
        # Slope score
        score_slope = 100.0
        local_slope = None
        if slope_path:
            local_slope = _sample_slope_at_point(pt, slope_path)
            if local_slope is not None:
                if local_slope <= 3:
                    score_slope = 100.0
                elif local_slope <= 8:
                    score_slope = 100.0 - (local_slope - 3) * 12
                else:
                    score_slope = max(0, 40 - (local_slope - 8) * 8)

        # Centroid proximity score
        dist_c = pt.distance(site_centroid)
        score_centroid = max(0, 100 * (1 - dist_c / (boundary_length / 4)))

        # Road proximity score
        score_roads = 50.0
        if roads_geom:
            score_roads = max(0, 100 - roads_geom.distance(pt) * 0.5)

        # Water distance score
        score_water = 100.0
        if water_geom:
            dw = water_geom.distance(pt)
            score_water = 0.0 if dw < 100 else min(100.0, (dw - 100) * 0.5)

        # Buildable area proximity score
        score_buildable = 100.0
        if buildable_geom:
            db = buildable_geom.distance(pt)
            # Severe penalty if not right next to buildable terrain
            score_buildable = 0.0 if db > 50 else max(0, 100 - db * 2)

        total = (score_slope + score_centroid + score_roads + score_water + score_buildable) / 5
        if total > best_score:
            best_score = total
            best_pt = pt
            best_slope = local_slope

    if best_pt is None:
        best_pt = candidates[0]
        
    return best_pt, best_slope


def _build_compound_polygons(centre_pt, crs, config, slope, site_centroid=None):
    """
    Builds the four BOP compound polygons oriented to sit INSIDE the site,
    on the interior side of the selected boundary point.

    Layout (viewed from outside, perpendicular to boundary):
      O&M compound | substation compound | BESS compound
      [guard gate at O&M entrance]

    The compounds extend TOWARDS the site centroid.
    reserve_bop_zone() subtracts them from the buildable area so no PV rows
    are placed there.
    """
    sub_cfg = config.get("substation", {})
    sub_w = sub_cfg.get("compound_width_m", 80)
    sub_h = sub_cfg.get("compound_height_m", 60)

    bess_cfg = config.get("bess", {})
    bess_w = bess_cfg.get("compound_width_m", 60)
    bess_h = bess_cfg.get("compound_height_m", 30)
    bess_mw = bess_cfg.get("capacity_mw", 0)
    bess_mwh = bess_cfg.get("capacity_mwh", 0)

    om_cfg = config.get("om_compound", {})
    om_w = om_cfg.get("width_m", 100)
    om_h = om_cfg.get("height_m", 50)

    gap = 10.0
    cx, cy = centre_pt.x, centre_pt.y

    # ── Determine INWARD direction: towards site centroid ──
    # The inward unit vector points from boundary point → centroid.
    # We place compounds by extending in that direction (inside site).
    if site_centroid is not None:
        dx = site_centroid.x - cx
        dy = site_centroid.y - cy
        mag = np.hypot(dx, dy)
        if mag > 0:
            ux, uy = dx / mag, dy / mag   # unit vector pointing INWARD
        else:
            ux, uy = 0.0, -1.0
    else:
        ux, uy = 0.0, -1.0  # default: extend south

    # ── Perpendicular axis (along the boundary, for E-W spread of compounds) ──
    # Rotate outward vector +90° for perpendicular
    px, py = -uy, ux

    # ── Substation compound ──
    # Centred on boundary point, extending `sub_h` metres outward
    # Build as axis-aligned box using the rotated coordinate frame:
    #   origin at boundary point, outward axis = (ux,uy), sideways axis = (px,py)
    def _rect(ox, oy, depth, half_width):
        """Rectangle in rotated frame: origin (ox,oy), extends `depth` outward."""
        corners = [
            (ox - half_width * px, oy - half_width * py),          # SW
            (ox + half_width * px, oy + half_width * py),          # SE
            (ox + half_width * px + depth * ux,
             oy + half_width * py + depth * uy),                   # NE
            (ox - half_width * px + depth * ux,
             oy - half_width * py + depth * uy),                   # NW
        ]
        return Polygon(corners)

    sub_geom = _rect(cx, cy, sub_h, sub_w / 2)

    # BESS — offset along +perpendicular from substation
    bess_offset = sub_w / 2 + gap + bess_w / 2
    bess_cx = cx + bess_offset * px
    bess_cy = cy + bess_offset * py
    bess_geom = _rect(bess_cx, bess_cy, bess_h, bess_w / 2)

    # O&M — offset along −perpendicular from substation
    om_offset = sub_w / 2 + gap + om_w / 2
    om_cx = cx - om_offset * px
    om_cy = cy - om_offset * py
    om_geom = _rect(om_cx, om_cy, om_h, om_w / 2)

    # Guard house — centred at gateway of O&M compound (at boundary baseline)
    gw, gh = 6.0, 5.0
    guard_geom = _rect(om_cx, om_cy, gh, gw / 2)

    sub_gdf = gpd.GeoDataFrame([{
        "compound_id": "MAIN_SUBSTATION",
        "type": "33/66kV Step-up Substation",
        "area_m2": sub_w * sub_h,
        "slope_pct": round(slope, 1) if slope else None,
        "geometry": sub_geom,
    }], crs=crs)

    bess_gdf = gpd.GeoDataFrame([{
        "bess_id": "MAIN_BESS",
        "type": "BESS Compound",
        "capacity_mw": bess_mw,
        "capacity_mwh": bess_mwh,
        "area_m2": bess_w * bess_h,
        "geometry": bess_geom,
    }], crs=crs)

    om_gdf = gpd.GeoDataFrame([{
        "compound_id": "OM_FACILITY",
        "type": "O&M Office, Workshop & Warehouse",
        "office_area_m2": om_cfg.get("office_area_m2", 175),
        "workshop_area_m2": om_cfg.get("workshop_area_m2", 250),
        "warehouse_area_m2": om_cfg.get("warehouse_area_m2", 125),
        "total_area_m2": om_w * om_h,
        "geometry": om_geom,
    }], crs=crs)

    guard_gdf = gpd.GeoDataFrame([{
        "compound_id": "GUARD_HOUSE",
        "type": "Security Guard Post",
        "area_m2": gw * gh,
        "geometry": guard_geom,
    }], crs=crs)

    return sub_gdf, bess_gdf, om_gdf, guard_gdf


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def reserve_bop_zone(site_gdf, buildable_area_gdf, exclusions_gdf, config, slope_path=None):
    """
    **Must run BEFORE generate_solar_blocks().**

    Workflow (mirrors PVcase / Helioscope BOP-first design):
      1. Select best substation point on site boundary (multi-criteria terrain scoring).
      2. Build compound polygons: substation (80×60m), BESS (60×30m),
         O&M facility (100×50m), guard house (6×5m).
      3. Merge all compounds + a 20m access buffer into a single BOP zone polygon.
      4. **Subtract** the BOP zone from buildable_area_gdf so the panel layout
         engine never places PV rows inside the BOP zone.

    Returns
    -------
    sub_pt             : shapely.Point          substation centre point
    sub_gdf            : GeoDataFrame           substation compound polygon
    bess_gdf           : GeoDataFrame           BESS compound polygon
    om_gdf             : GeoDataFrame           O&M facility compound polygon
    guard_gdf          : GeoDataFrame           guard house polygon
    bop_zone_gdf       : GeoDataFrame           merged BOP zone (for map display)
    reduced_buildable  : GeoDataFrame           buildable area with BOP zone removed
    """
    logger.info("=== BOP Zone Reservation (runs BEFORE panel layout) ===")

    crs = site_gdf.crs

    # Step 1: Select substation point
    sub_pt, slope = _select_substation_point(site_gdf, buildable_area_gdf, exclusions_gdf, config, slope_path)
    slope_str = f", slope={slope:.1f}%" if slope else ""
    logger.info(f"  Substation sited at ({sub_pt.x:.1f}, {sub_pt.y:.1f}){slope_str}")

    # Step 2: Build compound polygons
    site_centroid = site_gdf.geometry.union_all().centroid
    sub_gdf, bess_gdf, om_gdf, guard_gdf = _build_compound_polygons(
        sub_pt, crs, config, slope, site_centroid=site_centroid
    )

    sub_cfg = config.get("substation", {})
    bess_cfg = config.get("bess", {})
    om_cfg = config.get("om_compound", {})
    logger.info(f"  Substation compound: {sub_cfg.get('compound_width_m',80)}m × {sub_cfg.get('compound_height_m',60)}m")
    logger.info(f"  BESS compound:       {bess_cfg.get('compound_width_m',60)}m × {bess_cfg.get('compound_height_m',30)}m "
                f"({bess_cfg.get('capacity_mw',0)} MW / {bess_cfg.get('capacity_mwh',0)} MWh)")
    logger.info(f"  O&M compound:        {om_cfg.get('width_m',100)}m × {om_cfg.get('height_m',50)}m")

    # Step 3: Merge all compounds into one BOP zone polygon (+ 20m access road buffer)
    # We will iteratively shift the compounds inwards towards the site centroid 
    # until they are strictly contained inside the site boundary.
    site_geom = site_gdf.geometry.unary_union
    
    max_shifts = 30
    shift_step = 10.0
    dx = site_centroid.x - sub_pt.x
    dy = site_centroid.y - sub_pt.y
    mag = np.hypot(dx, dy)
    ux, uy = (dx/mag, dy/mag) if mag > 0 else (0.0, -1.0)

    shifts = 0
    while shifts < max_shifts:
        all_compound_geoms = (
            list(sub_gdf.geometry) +
            list(bess_gdf.geometry) +
            list(om_gdf.geometry) +
            list(guard_gdf.geometry)
        )
        bop_union = unary_union(all_compound_geoms).buffer(20.0)  # 20m = fence + access road
        
        if site_geom.contains(bop_union):
            break
            
        # Shift everything inwards
        sub_gdf.geometry = sub_gdf.geometry.translate(xoff=ux*shift_step, yoff=uy*shift_step)
        bess_gdf.geometry = bess_gdf.geometry.translate(xoff=ux*shift_step, yoff=uy*shift_step)
        om_gdf.geometry = om_gdf.geometry.translate(xoff=ux*shift_step, yoff=uy*shift_step)
        guard_gdf.geometry = guard_gdf.geometry.translate(xoff=ux*shift_step, yoff=uy*shift_step)
        shifts += 1

    if shifts > 0:
        logger.info(f"  Shifted BOP zone inwards {shifts * shift_step}m to eliminate boundary violations.")

    bop_zone_gdf = gpd.GeoDataFrame([{
        "compound_id": "BOP_ZONE",
        "type": "BOP Zone (substation + BESS + O&M + buffer)",
        "area_ha": bop_union.area / 10000,
        "geometry": bop_union,
    }], crs=crs)
    logger.info(f"  BOP zone total area: {bop_union.area/10000:.2f} ha (including 20m access buffer)")

    # Step 4: Subtract BOP zone from buildable area
    if buildable_area_gdf.empty:
        return sub_pt, sub_gdf, bess_gdf, om_gdf, guard_gdf, bop_zone_gdf, buildable_area_gdf

    try:
        buildable_union = buildable_area_gdf.geometry.unary_union
        reduced_geom = buildable_union.difference(bop_union)

        if reduced_geom.is_empty:
            logger.warning("  BOP zone consumed entire buildable area — using original buildable area.")
            reduced_buildable = buildable_area_gdf.copy()
        else:
            # Rebuild as GeoDataFrame preserving columns
            if reduced_geom.geom_type == "Polygon":
                geoms = [reduced_geom]
            else:
                geoms = list(reduced_geom.geoms)

            rows = []
            for geom in geoms:
                if geom.area > 100:
                    rows.append({
                        col: buildable_area_gdf.iloc[0][col]
                        for col in buildable_area_gdf.columns
                        if col != "geometry"
                    } | {"geometry": geom, "area_ha": geom.area / 10000})

            reduced_buildable = gpd.GeoDataFrame(rows, crs=crs)
            removed_ha = (buildable_union.area - reduced_geom.area) / 10000
            logger.info(f"  Removed {removed_ha:.2f} ha from buildable area for BOP zone → "
                        f"{reduced_buildable.geometry.area.sum()/10000:.2f} ha remaining for PV layout")
    except Exception as e:
        logger.warning(f"  BOP zone subtraction failed ({e}). Using original buildable area.")
        reduced_buildable = buildable_area_gdf.copy()

    return sub_pt, sub_gdf, bess_gdf, om_gdf, guard_gdf, bop_zone_gdf, reduced_buildable


# Keep the old entry point as a thin wrapper for backward-compatibility
def place_substation(site_gdf, buildable_area_gdf, exclusions_gdf, config, slope_path=None):
    """
    DEPRECATED: use reserve_bop_zone() instead.
    Kept for backward compatibility — returns (sub_gdf, sub_pt, bess_gdf, om_gdf, guard_gdf).
    """
    sub_pt, sub_gdf, bess_gdf, om_gdf, guard_gdf, _, _ = reserve_bop_zone(
        site_gdf, buildable_area_gdf, exclusions_gdf, config, slope_path
    )
    return sub_gdf, sub_pt, bess_gdf, om_gdf, guard_gdf
