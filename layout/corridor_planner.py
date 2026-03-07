"""
corridor_planner.py  —  Phase 3: Infrastructure Corridor Planning
==================================================================
Generates road and cable corridors BEFORE block tessellation so that
infrastructure has dedicated, reserved space.

Pipeline insertion point:
    Phase 5: BOP Zone Reservation
    **Phase 5.5: Corridor Planning** ← this module
    Phase 6: Layout Generation (on corridor-free buildable area)

Corridor hierarchy
------------------
• main_collector   — Straight road from substation to opposite site edge,
                     aligned to the long axis of the buildable area.
                     Width: 10 m (6 m road + 2 m trench + 2 m shoulders).
• secondary_branch — Perpendicular corridors at regular intervals along
                     the main collector, forming a herringbone / comb pattern.
                     Width: 8 m (4 m aisle + 2 m trench + 2 m shoulders).

Returns
-------
corridor_gdf       : GeoDataFrame  — corridor polygons for map display.
reduced_buildable  : GeoDataFrame  — buildable area with corridors removed.
corridor_info      : dict          — metadata for downstream routing
                     (spine_line, branch_lines, spacing, etc.)
"""

import logging
import numpy as np
import geopandas as gpd
from shapely.geometry import LineString, Point, Polygon, MultiPolygon, MultiLineString
from shapely.ops import unary_union, nearest_points, split
from shapely.affinity import rotate

logger = logging.getLogger("PVLayoutEngine.corridors")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _long_axis_direction(buildable_geom):
    """Compute the orientation of the longest axis of the buildable area.

    Returns the angle in degrees (clockwise from north / positive-Y axis)
    and the unit direction vector (dx, dy).
    """
    # Use the minimum bounding rectangle to find the long axis
    mbr = buildable_geom.minimum_rotated_rectangle
    coords = list(mbr.exterior.coords)

    # MBR has 5 coords (closed ring); pick the two longest edges
    edges = []
    for i in range(4):
        dx = coords[i + 1][0] - coords[i][0]
        dy = coords[i + 1][1] - coords[i][1]
        length = np.hypot(dx, dy)
        edges.append((length, dx, dy))

    # Longest edge = long axis direction
    edges.sort(key=lambda e: e[0], reverse=True)
    _, dx, dy = edges[0]
    mag = np.hypot(dx, dy)
    if mag < 1e-6:
        return 0.0, (0.0, 1.0)

    ux, uy = dx / mag, dy / mag
    angle_deg = np.degrees(np.arctan2(ux, uy))  # from +Y axis
    return angle_deg, (ux, uy)


def _extend_line_to_boundary(start_pt, direction, buildable_geom, max_extend=5000):
    """Extend a ray from start_pt in +direction and -direction
    until it exits the buildable geometry. Returns a LineString clipped to boundary.
    """
    ux, uy = direction

    # Create a very long line through the start point
    far_fwd = Point(start_pt.x + ux * max_extend, start_pt.y + uy * max_extend)
    far_bwd = Point(start_pt.x - ux * max_extend, start_pt.y - uy * max_extend)
    long_line = LineString([
        (far_bwd.x, far_bwd.y),
        (start_pt.x, start_pt.y),
        (far_fwd.x, far_fwd.y),
    ])

    # Clip to buildable boundary
    clipped = long_line.intersection(buildable_geom)
    if clipped.is_empty:
        return None

    # If intersection produces multiple segments, take the longest one
    # that passes through/near the start point
    if clipped.geom_type == "MultiLineString":
        best = None
        best_dist = float("inf")
        for seg in clipped.geoms:
            d = seg.distance(start_pt)
            if d < best_dist:
                best_dist = d
                best = seg
        clipped = best if best else clipped.geoms[0]

    return clipped


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def plan_corridors(buildable_area_gdf, substation_point, config):
    """Generate infrastructure corridors before block layout.

    Parameters
    ----------
    buildable_area_gdf : GeoDataFrame
        Buildable area (already has BOP zone removed).
    substation_point : shapely.Point
        Substation location (origin of main collector road).
    config : dict
        Full pipeline config including ``roads`` and ``mv_cables`` sections.

    Returns
    -------
    corridor_gdf : GeoDataFrame
        Corridor polygons (for map display and GIS export).
    reduced_buildable : GeoDataFrame
        Buildable area with corridor zones subtracted.
    corridor_info : dict
        Metadata for downstream routing:
        ``spine_line``, ``branch_lines``, ``branch_spacing_m``,
        ``spine_direction``, ``main_collector_width_m``.
    """
    logger.info("=== Infrastructure Corridor Planning ===")

    roads_cfg = config.get("roads", {})
    main_width = roads_cfg.get("main_collector_width_m", 6)
    aisle_width = roads_cfg.get("maintenance_aisle_width_m", 4)

    # Corridor widths: road + cable trench + shoulders
    main_corridor_width = main_width + 4  # 6m road + 2m trench + 2m shoulder = 10m
    branch_corridor_width = aisle_width + 4  # 4m aisle + 2m trench + 2m shoulder = 8m

    buildable_geom = buildable_area_gdf.geometry.union_all()
    crs = buildable_area_gdf.crs

    # ── Task 3.1: Main collector corridor ──
    # Straight line from substation along the long axis of buildable area.
    angle_deg, (ux, uy) = _long_axis_direction(buildable_geom)
    logger.info(f"  Buildable area long axis: {angle_deg:.1f}° from north")

    # Decide which direction along the long axis to extend:
    # pick the direction that goes away from substation toward the far end
    centroid = buildable_geom.centroid
    dot_test = (centroid.x - substation_point.x) * ux + \
               (centroid.y - substation_point.y) * uy
    if dot_test < 0:
        ux, uy = -ux, -uy  # flip to point toward buildable centre

    spine_line = _extend_line_to_boundary(substation_point, (ux, uy), buildable_geom)

    if spine_line is None or spine_line.is_empty:
        logger.warning("  Could not generate main collector corridor — falling back to centroid line")
        spine_line = LineString([
            (substation_point.x, substation_point.y),
            (centroid.x, centroid.y),
        ])

    spine_length = spine_line.length
    logger.info(f"  Main collector: {spine_length:.0f}m, "
                f"corridor width: {main_corridor_width}m")

    # Create corridor polygon
    spine_corridor = spine_line.buffer(main_corridor_width / 2, cap_style="flat")

    # ── Task 3.2: Secondary branch corridors ──
    # Perpendicular to main collector at regular intervals.
    # Spacing should match target block cell width for Task 3.4.
    block_cfg = config.get("block", {})
    target_block_ha = block_cfg.get("target_block_area_ha", 2.5)
    # Derive branch spacing from block geometry:
    # blocks are tessellation cells (E-W width × N-S depth).
    # depth is ~600m (from row count × pitch). Spacing along collector ≈ E-W width.
    # For 2.5 ha target with ~600m depth → width ≈ 2.5e4/600 ≈ 42m
    # But we also need road access, so space branches ~2× block width apart
    # so one branch serves blocks on both sides.
    # Use configurable spacing, defaulting to a reasonable interval.
    branch_spacing = roads_cfg.get("branch_spacing_m", None)
    if branch_spacing is None:
        # Heuristic: space branches every ~150-200m along collector
        # (each branch serves ~2 blocks on each side)
        branch_spacing = max(100, min(250, spine_length / 8))

    # Perpendicular direction
    px, py = -uy, ux

    branch_lines = []
    branch_corridors = []

    # Walk along spine and create branches at regular intervals
    n_branches = max(1, int(spine_length / branch_spacing))
    for i in range(1, n_branches + 1):
        dist_along = i * branch_spacing
        if dist_along >= spine_length - branch_spacing / 4:
            break

        # Point on spine at this distance
        try:
            branch_origin = spine_line.interpolate(dist_along)
        except Exception:
            continue

        # Extend perpendicular in both directions until hitting boundary
        branch_line = _extend_line_to_boundary(
            branch_origin, (px, py), buildable_geom
        )

        if branch_line is not None and branch_line.length > 20:
            branch_lines.append(branch_line)
            branch_corridor = branch_line.buffer(
                branch_corridor_width / 2, cap_style="flat"
            )
            branch_corridors.append(branch_corridor)

    logger.info(f"  Secondary branches: {len(branch_lines)} corridors, "
                f"spacing: {branch_spacing:.0f}m, width: {branch_corridor_width}m")

    # ── Merge all corridors ──
    all_corridor_geoms = [spine_corridor] + branch_corridors
    corridor_union = unary_union(all_corridor_geoms)

    # Clip to buildable area
    corridor_union = corridor_union.intersection(buildable_geom)

    corridor_records = [{
        "corridor_type": "main_collector",
        "width_m": main_corridor_width,
        "length_m": round(spine_length, 1),
        "geometry": spine_corridor.intersection(buildable_geom),
    }]
    for j, bc in enumerate(branch_corridors):
        clipped = bc.intersection(buildable_geom)
        if not clipped.is_empty:
            corridor_records.append({
                "corridor_type": "secondary_branch",
                "width_m": branch_corridor_width,
                "length_m": round(branch_lines[j].length, 1) if j < len(branch_lines) else 0,
                "geometry": clipped,
            })

    corridor_gdf = gpd.GeoDataFrame(corridor_records, crs=crs)

    total_corridor_ha = corridor_union.area / 10000
    logger.info(f"  Total corridor area: {total_corridor_ha:.2f} ha")

    # ── Task 3.3: Subtract corridors from buildable area ──
    reduced_geom = buildable_geom.difference(corridor_union)

    if reduced_geom.is_empty:
        logger.warning("  Corridors consumed entire buildable area — "
                       "using original. Check spacing/width config.")
        reduced_buildable = buildable_area_gdf.copy()
    else:
        if reduced_geom.geom_type == "Polygon":
            geoms = [reduced_geom]
        else:
            geoms = list(reduced_geom.geoms)

        rows = []
        for geom in geoms:
            if geom.area > 50:  # skip tiny slivers
                rows.append({
                    col: buildable_area_gdf.iloc[0][col]
                    for col in buildable_area_gdf.columns
                    if col != "geometry"
                } | {"geometry": geom, "area_ha": geom.area / 10000})

        reduced_buildable = gpd.GeoDataFrame(rows, crs=crs)
        removed_ha = (buildable_geom.area - reduced_geom.area) / 10000
        logger.info(f"  Subtracted {removed_ha:.2f} ha corridors from buildable area → "
                    f"{reduced_buildable.geometry.area.sum()/10000:.2f} ha for PV layout")

    # ── Task 3.4: Block tessellation alignment info ──
    # Provide downstream block_generator with corridor alignment metadata
    corridor_info = {
        "spine_line": spine_line,
        "spine_direction": (ux, uy),
        "spine_angle_deg": angle_deg,
        "branch_lines": branch_lines,
        "branch_spacing_m": branch_spacing,
        "main_collector_width_m": main_corridor_width,
        "branch_corridor_width_m": branch_corridor_width,
    }

    return corridor_gdf, reduced_buildable, corridor_info
