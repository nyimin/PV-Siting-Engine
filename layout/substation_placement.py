import logging
import numpy as np
import geopandas as gpd
from shapely.geometry import Point, box, Polygon, MultiPolygon
from shapely.ops import linemerge, unary_union
from shapely.affinity import translate as _translate

from utils.raster_helpers import sample_raster_mean

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


def _compute_compound_footprint(centre_pt, ux, uy, config):
    """Build compound rectangles around *centre_pt* with inward direction (ux, uy).

    Returns the buffered union of all compound polygons (the BOP footprint)
    WITHOUT the 20 m fence buffer — that's added later.
    """
    px, py = -uy, ux  # perpendicular axis

    sub_cfg = config.get("substation", {})
    sub_w, sub_h = sub_cfg.get("compound_width_m", 80), sub_cfg.get("compound_height_m", 60)

    bess_cfg = config.get("bess", {})
    bess_w = bess_cfg.get("compound_width_m", 60)
    bess_h = bess_cfg.get("compound_height_m", 30)

    om_cfg = config.get("om_compound", {})
    om_w, om_h = om_cfg.get("width_m", 100), om_cfg.get("height_m", 50)

    gap = 10.0
    cx, cy = centre_pt.x, centre_pt.y

    def _rect(ox, oy, depth, half_width):
        corners = [
            (ox - half_width * px, oy - half_width * py),
            (ox + half_width * px, oy + half_width * py),
            (ox + half_width * px + depth * ux, oy + half_width * py + depth * uy),
            (ox - half_width * px + depth * ux, oy - half_width * py + depth * uy),
        ]
        return Polygon(corners)

    sub_geom = _rect(cx, cy, sub_h, sub_w / 2)

    bess_offset = sub_w / 2 + gap + bess_w / 2
    bess_cx = cx + bess_offset * px
    bess_cy = cy + bess_offset * py
    bess_geom = _rect(bess_cx, bess_cy, bess_h, bess_w / 2)

    om_offset = sub_w / 2 + gap + om_w / 2
    om_cx = cx - om_offset * px
    om_cy = cy - om_offset * py
    om_geom = _rect(om_cx, om_cy, om_h, om_w / 2)

    gw, gh = 6.0, 5.0
    guard_geom = _rect(om_cx, om_cy, gh, gw / 2)

    footprint = unary_union([sub_geom, bess_geom, om_geom, guard_geom])
    return footprint


def _score_candidate(pt, footprint_geom, buildable_geom, slope_path,
                     roads_geom, water_geom, reference_pt,
                     boundary_length, weights, max_compound_slope_deg):
    """Score a single candidate substation point.

    Returns (total_score, local_slope) or (None, None) if candidate is
    disqualified (e.g. footprint mostly outside buildable).
    """
    w = weights

    # ── 1. Terrain slope score (scored over compound footprint, not just a point) ──
    footprint_slope = None
    if slope_path:
        footprint_slope = sample_raster_mean(footprint_geom, slope_path)
    if footprint_slope is None:
        footprint_slope = _sample_slope_at_point(pt, slope_path) if slope_path else None

    score_slope = 100.0
    if footprint_slope is not None:
        # Hard reject if mean slope exceeds configured maximum
        if footprint_slope > max_compound_slope_deg:
            return None, footprint_slope
        if footprint_slope <= 3:
            score_slope = 100.0
        elif footprint_slope <= 8:
            score_slope = 100.0 - (footprint_slope - 3) * 12
        else:
            score_slope = max(0, 40 - (footprint_slope - 8) * 8)

    # ── 2. Proximity to POI / centroid ──
    dist_ref = pt.distance(reference_pt)
    normaliser = max(boundary_length / 4, 500)  # avoid division by tiny number
    score_proximity = max(0, 100 * (1 - dist_ref / normaliser))

    # ── 3. Road proximity ──
    score_roads = 50.0
    if roads_geom is not None:
        score_roads = max(0, 100 - roads_geom.distance(pt) * 0.5)

    # ── 4. Water avoidance ──
    score_water = 100.0
    if water_geom is not None:
        dw = water_geom.distance(pt)
        score_water = 0.0 if dw < 100 else min(100.0, (dw - 100) * 0.5)

    # ── 5. Buildable coverage over compound footprint (Task 2.2) ──
    inside_area = footprint_geom.intersection(buildable_geom).area
    total_area = footprint_geom.area
    coverage_frac = inside_area / total_area if total_area > 0 else 0
    score_buildable = coverage_frac * 100.0

    # Hard reject if <50% of footprint sits inside buildable area
    if coverage_frac < 0.50:
        return None, footprint_slope

    # ── Weighted total ──
    total = (
        w["terrain_slope"] * score_slope +
        w["proximity_poi"] * score_proximity +
        w["road_access"] * score_roads +
        w["water_avoidance"] * score_water +
        w["buildable_coverage"] * score_buildable
    )

    return total, footprint_slope


def _select_substation_point(site_gdf, buildable_area_gdf, exclusions_gdf,
                             config, slope_path=None, poi_coord=None):
    """Select the best substation location using multi-criteria evaluation.

    Phase 2 improvements over the original:
      - Task 2.1: Samples interior grid points AND boundary points.
      - Task 2.2: Validates the compound footprint (slope + containment).
      - Task 2.3: Uses configurable scoring weights from config.yaml.
      - Task 2.4: Orients compounds based on terrain aspect at each candidate.
    """
    site_geom = site_gdf.geometry.union_all()
    site_centroid = site_geom.centroid

    # ── Read configurable weights (Task 2.3) ──
    bop_cfg = config.get("bop_siting", {})
    weights_cfg = bop_cfg.get("weights", {})
    weights = {
        "terrain_slope":      weights_cfg.get("terrain_slope", 0.30),
        "proximity_poi":      weights_cfg.get("proximity_poi", 0.20),
        "road_access":        weights_cfg.get("road_access", 0.15),
        "water_avoidance":    weights_cfg.get("water_avoidance", 0.15),
        "buildable_coverage": weights_cfg.get("buildable_coverage", 0.20),
    }
    # Normalise weights to sum to 1
    w_sum = sum(weights.values())
    if w_sum > 0:
        weights = {k: v / w_sum for k, v in weights.items()}

    max_compound_slope = bop_cfg.get("max_compound_slope_deg", 5.0)
    grid_spacing = bop_cfg.get("interior_grid_spacing_m", 80)

    # ── Extract constraint geometries ──
    roads_geom = None
    water_geom = None
    if exclusions_gdf is not None and not exclusions_gdf.empty and "constraint_type" in exclusions_gdf.columns:
        roads_subset = exclusions_gdf[exclusions_gdf["constraint_type"] == "osm_roads"]
        if not roads_subset.empty:
            roads_geom = roads_subset.geometry.union_all()
        water_subset = exclusions_gdf[exclusions_gdf["constraint_type"].isin(
            ["osm_water", "lulc_Permanent water bodies"])]
        if not water_subset.empty:
            water_geom = water_subset.geometry.union_all()

    if buildable_area_gdf is None or buildable_area_gdf.empty:
        return site_centroid, None

    buildable_geom = buildable_area_gdf.geometry.union_all()

    # ── Reference point (POI or centroid) ──
    if poi_coord is not None:
        reference_pt = Point(poi_coord)
    else:
        reference_pt = site_centroid

    # ── Task 2.1: Generate candidate points — boundary AND interior grid ──
    candidates = []

    # Boundary candidates (as before)
    boundary = buildable_geom.boundary
    if not boundary.is_empty:
        if boundary.geom_type == "MultiLineString":
            boundary = linemerge(boundary)
            if boundary.geom_type == "MultiLineString":
                boundary = max(list(boundary.geoms), key=lambda g: g.length)
        boundary_length = boundary.length
        n_boundary = max(30, int(boundary_length / 50))
        for i in range(n_boundary):
            try:
                pt = boundary.interpolate(i / n_boundary, normalized=True)
                candidates.append(pt)
            except Exception:
                continue
    else:
        boundary_length = 1000  # fallback

    # Interior grid candidates (Task 2.1)
    minx, miny, maxx, maxy = buildable_geom.bounds
    x_coords = np.arange(minx + grid_spacing / 2, maxx, grid_spacing)
    y_coords = np.arange(miny + grid_spacing / 2, maxy, grid_spacing)
    for x in x_coords:
        for y in y_coords:
            pt = Point(x, y)
            # Only keep points inside buildable area
            if buildable_geom.contains(pt):
                candidates.append(pt)

    logger.info(f"  BOP siting: evaluating {len(candidates)} candidates "
                f"({n_boundary if not boundary.is_empty else 0} boundary + "
                f"{len(candidates) - (n_boundary if not boundary.is_empty else 0)} interior)")

    if not candidates:
        return buildable_geom.centroid, None

    # ── Evaluate each candidate (Tasks 2.2, 2.3, 2.4) ──
    best_score = -1
    best_pt = None
    best_slope = None

    for pt in candidates:
        # Task 2.4: Terrain-aware orientation — compute aspect at candidate
        # to orient compounds perpendicular to steepest descent.
        ux, uy = _compute_inward_direction(pt, buildable_geom, slope_path, site_centroid)

        # Build the compound footprint for this candidate
        footprint = _compute_compound_footprint(pt, ux, uy, config)

        score, local_slope = _score_candidate(
            pt, footprint, buildable_geom, slope_path,
            roads_geom, water_geom, reference_pt,
            boundary_length, weights, max_compound_slope
        )

        if score is not None and score > best_score:
            best_score = score
            best_pt = pt
            best_slope = local_slope

    if best_pt is None:
        # All candidates rejected — fall back to lowest-slope boundary point
        logger.warning("  All BOP candidates rejected by footprint validation. "
                       "Falling back to lowest-slope buildable point.")
        best_pt = buildable_geom.centroid
        best_slope = _sample_slope_at_point(best_pt, slope_path) if slope_path else None

    return best_pt, best_slope


def _compute_inward_direction(pt, buildable_geom, slope_path, site_centroid):
    """Determine the inward direction for compound orientation.

    Task 2.4: If slope data is available, orient perpendicular to the steepest
    descent direction (so compounds run along contour lines, minimising grading).
    Falls back to pointing toward the buildable centroid.
    """
    # Default: point toward buildable area centroid
    dx = site_centroid.x - pt.x
    dy = site_centroid.y - pt.y
    mag = np.hypot(dx, dy)
    if mag < 1.0:
        return 0.0, -1.0

    ux_default, uy_default = dx / mag, dy / mag

    if slope_path is None:
        return ux_default, uy_default

    # Sample aspect at candidate to get slope direction
    try:
        import os
        aspect_path = slope_path.replace("slope.tif", "aspect.tif")
        if not os.path.exists(aspect_path):
            return ux_default, uy_default

        aspect_val = sample_raster_mean(pt.buffer(30), aspect_path)
        if aspect_val is None:
            return ux_default, uy_default

        # Aspect is in degrees clockwise from north.
        # Steepest descent direction = aspect direction.
        # We want compounds to extend PERPENDICULAR to steepest descent
        # (along the contour), so rotate aspect by 90°.
        # But the compound should still generally point inward,
        # so we pick the perpendicular direction closest to the inward default.
        aspect_rad = np.radians(aspect_val)
        # Steepest descent unit vector (aspect = clockwise from north)
        sdx = np.sin(aspect_rad)
        sdy = -np.cos(aspect_rad)  # negative because y-axis is northward in UTM

        # Two perpendicular options
        perp1_x, perp1_y = -sdy, sdx
        perp2_x, perp2_y = sdy, -sdx

        # Pick the one closest to the default inward direction
        dot1 = perp1_x * ux_default + perp1_y * uy_default
        dot2 = perp2_x * ux_default + perp2_y * uy_default

        if dot1 >= dot2:
            return perp1_x, perp1_y
        else:
            return perp2_x, perp2_y

    except Exception:
        return ux_default, uy_default


def _build_compound_polygons(centre_pt, crs, config, slope, site_centroid=None,
                             slope_path=None, buildable_geom=None):
    """Builds the four BOP compound polygons oriented to sit INSIDE the site.

    Layout (viewed from outside, perpendicular to boundary):
      O&M compound | substation compound | BESS compound
      [guard gate at O&M entrance]

    Phase 2: uses terrain-aware orientation when slope data is available.
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

    # Task 2.4: Terrain-aware orientation
    if buildable_geom is not None and site_centroid is not None:
        ux, uy = _compute_inward_direction(centre_pt, buildable_geom, slope_path, site_centroid)
    elif site_centroid is not None:
        dx = site_centroid.x - cx
        dy = site_centroid.y - cy
        mag = np.hypot(dx, dy)
        if mag > 0:
            ux, uy = dx / mag, dy / mag
        else:
            ux, uy = 0.0, -1.0
    else:
        ux, uy = 0.0, -1.0

    px, py = -uy, ux  # perpendicular axis

    def _rect(ox, oy, depth, half_width):
        """Rectangle in rotated frame: origin (ox,oy), extends `depth` outward."""
        corners = [
            (ox - half_width * px, oy - half_width * py),
            (ox + half_width * px, oy + half_width * py),
            (ox + half_width * px + depth * ux, oy + half_width * py + depth * uy),
            (ox - half_width * px + depth * ux, oy - half_width * py + depth * uy),
        ]
        return Polygon(corners)

    sub_geom = _rect(cx, cy, sub_h, sub_w / 2)

    bess_offset = sub_w / 2 + gap + bess_w / 2
    bess_cx = cx + bess_offset * px
    bess_cy = cy + bess_offset * py
    bess_geom = _rect(bess_cx, bess_cy, bess_h, bess_w / 2)

    om_offset = sub_w / 2 + gap + om_w / 2
    om_cx = cx - om_offset * px
    om_cy = cy - om_offset * py
    om_geom = _rect(om_cx, om_cy, om_h, om_w / 2)

    gw, gh = 6.0, 5.0
    guard_geom = _rect(om_cx, om_cy, gh, gw / 2)

    sub_gdf = gpd.GeoDataFrame([{
        "compound_id": "MAIN_SUBSTATION",
        "type": "33/66kV Step-up Substation",
        "area_m2": sub_w * sub_h,
        "slope_deg": round(slope, 1) if slope else None,
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

def reserve_bop_zone(site_gdf, buildable_area_gdf, exclusions_gdf, config,
                     slope_path=None, poi_coord=None):
    """**Must run BEFORE generate_solar_blocks().**

    Workflow (mirrors PVcase / Helioscope BOP-first design):
      1. Select best substation point via multi-criteria scoring
         (boundary + interior grid candidates, compound footprint validation).
      2. Build compound polygons: substation, BESS, O&M, guard house.
      3. Merge all compounds + 20 m buffer into a single BOP zone polygon.
      4. Subtract the BOP zone from buildable area.

    Returns
    -------
    sub_pt, sub_gdf, bess_gdf, om_gdf, guard_gdf, bop_zone_gdf, reduced_buildable
    """
    logger.info("=== BOP Zone Reservation (runs BEFORE panel layout) ===")

    crs = site_gdf.crs

    # Step 1: Select substation point
    sub_pt, slope = _select_substation_point(
        site_gdf, buildable_area_gdf, exclusions_gdf, config, slope_path, poi_coord
    )
    slope_str = f", slope={slope:.1f}°" if slope else ""
    logger.info(f"  Substation sited at ({sub_pt.x:.1f}, {sub_pt.y:.1f}){slope_str}")

    # Step 2: Build compound polygons
    site_centroid = site_gdf.geometry.union_all().centroid
    buildable_geom = buildable_area_gdf.geometry.union_all()
    sub_gdf, bess_gdf, om_gdf, guard_gdf = _build_compound_polygons(
        sub_pt, crs, config, slope,
        site_centroid=site_centroid,
        slope_path=slope_path,
        buildable_geom=buildable_geom
    )

    sub_cfg = config.get("substation", {})
    bess_cfg = config.get("bess", {})
    om_cfg = config.get("om_compound", {})
    logger.info(f"  Substation compound: {sub_cfg.get('compound_width_m',80)}m × "
                f"{sub_cfg.get('compound_height_m',60)}m")
    logger.info(f"  BESS compound:       {bess_cfg.get('compound_width_m',60)}m × "
                f"{bess_cfg.get('compound_height_m',30)}m "
                f"({bess_cfg.get('capacity_mw',0)} MW / {bess_cfg.get('capacity_mwh',0)} MWh)")
    logger.info(f"  O&M compound:        {om_cfg.get('width_m',100)}m × "
                f"{om_cfg.get('height_m',50)}m")

    # Step 3: Merge all compounds into one BOP zone polygon (+ 20 m access buffer)
    all_compound_geoms = (
        list(sub_gdf.geometry) +
        list(bess_gdf.geometry) +
        list(om_gdf.geometry) +
        list(guard_gdf.geometry)
    )
    bop_raw_union = unary_union(all_compound_geoms).buffer(20.0)

    # Enforce compound boundary containment (Phase 1, Task 1.5)
    buildable_union = buildable_area_gdf.geometry.union_all()

    initial_inside = bop_raw_union.intersection(buildable_union).area
    coverage_pct = (initial_inside / bop_raw_union.area * 100) if bop_raw_union.area > 0 else 0

    if coverage_pct < 80.0:
        logger.warning(f"  BOP zone only {coverage_pct:.1f}% inside buildable area "
                       "— attempting inward shift...")
        buildable_centroid = buildable_union.centroid
        bop_centroid = bop_raw_union.centroid
        dx = (buildable_centroid.x - bop_centroid.x) * 0.3
        dy = (buildable_centroid.y - bop_centroid.y) * 0.3
        bop_raw_union = _translate(bop_raw_union, xoff=dx, yoff=dy)

        revised_inside = bop_raw_union.intersection(buildable_union).area
        revised_coverage = (revised_inside / bop_raw_union.area * 100) if bop_raw_union.area > 0 else 0
        logger.info(f"  After shift: BOP zone now {revised_coverage:.1f}% inside buildable area")

        if revised_coverage < 60.0:
            logger.warning("  BOP zone has poor boundary fit even after shift — "
                           "compound shapes may be clipped.")

    bop_union = bop_raw_union.intersection(buildable_union)

    bop_zone_gdf = gpd.GeoDataFrame([{
        "compound_id": "BOP_ZONE",
        "type": "BOP Zone (substation + BESS + O&M + buffer)",
        "area_ha": bop_union.area / 10000,
        "geometry": bop_union,
    }], crs=crs)
    logger.info(f"  BOP zone total area: {bop_union.area/10000:.2f} ha "
                f"(including 20 m access buffer)")

    # Step 4: Subtract BOP zone from buildable area
    if buildable_area_gdf.empty:
        return sub_pt, sub_gdf, bess_gdf, om_gdf, guard_gdf, bop_zone_gdf, buildable_area_gdf

    try:
        reduced_geom = buildable_union.difference(bop_union)

        if reduced_geom.is_empty:
            logger.warning("  BOP zone consumed entire buildable area — using original.")
            reduced_buildable = buildable_area_gdf.copy()
        else:
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
    """DEPRECATED: use reserve_bop_zone() instead."""
    sub_pt, sub_gdf, bess_gdf, om_gdf, guard_gdf, _, _ = reserve_bop_zone(
        site_gdf, buildable_area_gdf, exclusions_gdf, config, slope_path
    )
    return sub_gdf, sub_pt, bess_gdf, om_gdf, guard_gdf
