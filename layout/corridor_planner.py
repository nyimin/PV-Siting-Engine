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
from shapely.geometry import LineString, Point, Polygon, MultiPolygon, MultiLineString, box
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


import math
import numpy as np

class AStarTerrainGrid:
    """Terrain-aware pathfinder that strictly restricts routing to the buildable area."""
    def __init__(self, buildable_geom, cell_size_m=10):
        minx, miny, maxx, maxy = buildable_geom.bounds
        self.ox = minx - cell_size_m*2
        self.oy = miny - cell_size_m*2
        self.cs = cell_size_m
        self.ncols = int(math.ceil((maxx + cell_size_m*2 - self.ox) / cell_size_m)) + 1
        self.nrows = int(math.ceil((maxy + cell_size_m*2 - self.oy) / cell_size_m)) + 1
        
        import rasterio.features
        from rasterio.transform import from_origin
        
        transform = from_origin(self.ox, maxy + cell_size_m*2, self.cs, self.cs)
        try:
            geom_list = list(buildable_geom.geoms) if hasattr(buildable_geom, 'geoms') else [buildable_geom]
            shapes = [(geom, 1) for geom in geom_list]
        except Exception:
            shapes = [(buildable_geom, 1)]
            
        buildable_mask = rasterio.features.rasterize(
            shapes,
            out_shape=(self.nrows, self.ncols),
            transform=transform,
            fill=0,
            dtype='uint8'
        )
        buildable_mask = np.flipud(buildable_mask)
        self.obstacle = (buildable_mask == 0)

    def world_to_cell(self, x, y):
        c = int((x - self.ox) / self.cs)
        r = int((y - self.oy) / self.cs)
        return max(0, min(self.ncols - 1, c)), max(0, min(self.nrows - 1, r))

    def cell_to_world(self, r, c):
        return self.ox + (c + 0.5) * self.cs, self.oy + (r + 0.5) * self.cs

    def astar(self, src_pt, dst_pt):
        sc, sr = self.world_to_cell(src_pt.x, src_pt.y)
        dc, dr = self.world_to_cell(dst_pt.x, dst_pt.y)

        # Clear a 60m radius around start and end points to escape BOP holes
        import math
        for r in range(max(0, sr-6), min(self.nrows, sr+7)):
            for c in range(max(0, sc-6), min(self.ncols, sc+7)):
                cx, cy = self.cell_to_world(r, c)
                if math.hypot(cx - src_pt.x, cy - src_pt.y) < 60:
                    self.obstacle[r, c] = False
                    
        for r in range(max(0, dr-6), min(self.nrows, dr+7)):
            for c in range(max(0, dc-6), min(self.ncols, dc+7)):
                cx, cy = self.cell_to_world(r, c)
                if math.hypot(cx - dst_pt.x, cy - dst_pt.y) < 60:
                    self.obstacle[r, c] = False
        
        try:
            import skimage.graph
            # Free space = 1.0 cost, Obstacle = 1e9 cost
            costs = np.where(self.obstacle, 1e9, 1.0).astype(np.float32)
            mcp = skimage.graph.MCP_Geometric(costs)
            
            costs_to_target, traceback = mcp.find_costs(starts=[(sr, sc)], ends=[(dr, dc)])
            if costs_to_target[dr, dc] >= 1e8:
                return None  # No valid path found through free space
                
            path_indices = mcp.traceback((dr, dc))
            
            coords = [self.cell_to_world(r, c) for r, c in path_indices]
            coords[0] = (src_pt.x, src_pt.y)
            coords[-1] = (dst_pt.x, dst_pt.y)
            
            return LineString(coords).simplify(5.0, preserve_topology=True)
        except Exception as e:
            logger.warning(f"  AStarTerrainGrid routing failed: {e}")
            return None

def _extend_line_to_boundary(start_pt, direction, buildable_geom, max_extend=5000):
    """Extend a ray from start_pt in +direction and -direction
    and perfectly clip it to the true buildable_geom (never crossing exclusions).
    Returns a single continuous LineString originating at start_pt.
    """
    ux, uy = direction

    far_fwd = Point(start_pt.x + ux * max_extend, start_pt.y + uy * max_extend)
    far_bwd = Point(start_pt.x - ux * max_extend, start_pt.y - uy * max_extend)
    long_line = LineString([
        (far_bwd.x, far_bwd.y),
        (start_pt.x, start_pt.y),
        (far_fwd.x, far_fwd.y),
    ])

    clipped = long_line.intersection(buildable_geom)
    
    if clipped.is_empty:
        return None
        
    def _get_connected_segment(multi_line, origin):
        if multi_line.geom_type == "LineString":
            return multi_line
        if multi_line.is_empty:
            return None
        best_seg = None
        min_dist = float("inf")
        for geom in multi_line.geoms:
            dist = geom.distance(origin)
            if dist < min_dist:
                min_dist = dist
                best_seg = geom
        if min_dist < 1.0:
            return best_seg
        return None

    return _get_connected_segment(clipped, start_pt)


def _generate_tertiary_aisles(buildable_geom, terrain_paths, config):
    """Generate terrain-guided tertiary aisle polygons (6 m wide) inside candidate
    block cells, placing each aisle along the lowest-slope N-S axis rather than
    the geometric centroid.

    The function tiles the buildable area with a coarse cell grid matching the
    approximate block footprint, then for each cell samples the slope raster at
    a set of candidate E-W positions and picks the axis with the lowest mean slope.

    Parameters
    ----------
    buildable_geom : shapely geometry
        Union of the reduced buildable area (after BOP + main/branch corridors subtracted).
    terrain_paths : dict
        Must contain ``'slope'`` key pointing to a slope raster (values in DEGREES).
    config : dict
        Full pipeline config.  Reads ``roads.tertiary_aisle_width_m``,
        ``roads.tertiary_aisle_slope_search_step_m``, ``block.strings_per_table``,
        ``solar.*``.

    Returns
    -------
    list of shapely.Polygon
        Aisle exclusion polygons ready to union into the corridor set.
    list of shapely.LineString
        Corresponding aisle centrelines (stored in corridor_info).
    """
    from utils.raster_helpers import sample_raster_mean as _srm

    roads_cfg = config.get("roads", {})
    aisle_width = roads_cfg.get("tertiary_aisle_width_m", 6)
    search_step = roads_cfg.get("tertiary_aisle_slope_search_step_m", 10)

    solar_cfg = config.get("solar", {})
    block_cfg = config.get("block", {})
    mod_w = solar_cfg.get("module_width_m", 1.303)
    mod_l = solar_cfg.get("module_length_m", 2.384)
    mods_per_string = solar_cfg.get("modules_per_string", 28)
    strings_per_table = block_cfg.get("strings_per_table", 2)
    gcr = solar_cfg.get("gcr", 0.38)

    # Table geometry (2P portrait)
    n_high = 2
    mods_ew = mods_per_string // n_high
    table_ew_width = mods_ew * mod_w * strings_per_table  # E-W width of one row
    table_hgt = n_high * mod_l                            # tilted table depth
    flat_pitch = table_hgt / gcr                          # N-S row pitch

    # Approximate block cell size: use row E-W width × estimated N-S depth of one block
    # (target_strings_per_block / strings_per_table) rows × flat_pitch
    strings_per_inv = solar_cfg.get("strings_per_inverter", 22)
    invs_per_block = block_cfg.get("inverters_per_block", 10)
    target_strings = strings_per_inv * invs_per_block
    rows_per_block = max(1, target_strings // strings_per_table)
    cell_ns_depth = rows_per_block * flat_pitch          # approx N-S extent of one block

    minx, miny, maxx, maxy = buildable_geom.bounds
    slope_path = terrain_paths.get("slope") if terrain_paths else None

    aisle_polys = []
    aisle_lines = []

    x = minx
    while x < maxx:
        y = miny
        while y < maxy:
            # Candidate block cell bounding box
            cell = box(x, y, x + table_ew_width, y + cell_ns_depth)
            cell_clipped = cell.intersection(buildable_geom)
            if cell_clipped.is_empty or cell_clipped.area < (table_ew_width * cell_ns_depth * 0.2):
                y += cell_ns_depth
                continue

            cx_min, cy_min, cx_max, cy_max = cell_clipped.bounds
            cell_width = cx_max - cx_min

            # Sample slope raster at candidate N-S axes within this cell
            best_slope = float("inf")
            best_cx = cx_min + cell_width / 2  # centroid fallback

            if slope_path:
                n_candidates = max(1, int(cell_width / search_step))
                for k in range(n_candidates):
                    cand_x = cx_min + (k + 0.5) * (cell_width / n_candidates)
                    # Sample a thin N-S strip at this x position
                    strip = box(
                        cand_x - aisle_width / 2, cy_min,
                        cand_x + aisle_width / 2, cy_max
                    ).intersection(buildable_geom)
                    if strip.is_empty:
                        continue
                    slope_val = _srm(strip, slope_path)
                    if slope_val is not None and slope_val < best_slope:
                        best_slope = slope_val
                        best_cx = cand_x

            # Create aisle polygon and centreline at best_cx
            aisle_line = LineString(
                [(best_cx, cy_min - 5), (best_cx, cy_max + 5)]
            ).intersection(buildable_geom)
            if aisle_line.is_empty or aisle_line.length < 20:
                y += cell_ns_depth
                continue

            aisle_poly = aisle_line.buffer(aisle_width / 2, cap_style="flat")
            aisle_poly = aisle_poly.intersection(buildable_geom)
            if not aisle_poly.is_empty:
                aisle_polys.append(aisle_poly)
                if aisle_line.geom_type == "LineString":
                    aisle_lines.append(aisle_line)

            y += cell_ns_depth
        x += table_ew_width

    logger.info(
        f"  Tertiary aisles: {len(aisle_polys)} terrain-guided aisles generated "
        f"(width {aisle_width}m, slope-search step {search_step}m)"
    )
    return aisle_polys, aisle_lines


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def plan_corridors(buildable_area_gdf, substation_point, config, terrain_paths=None):
    """Generate infrastructure corridors before block layout.

    Parameters
    ----------
    buildable_area_gdf : GeoDataFrame
        Buildable area (already has BOP zone removed).
    substation_point : shapely.Point
        Substation location (origin of main collector road).
    config : dict
        Full pipeline config including ``roads`` and ``mv_cables`` sections.
    terrain_paths : dict, optional
        Terrain raster paths.  When provided and
        ``roads.tertiary_aisles_enabled: true`` in config, terrain-guided
        tertiary aisles are generated as pre-layout exclusions inside block
        cells (R2 — Road-First Tessellation).

    Returns
    -------
    corridor_gdf : GeoDataFrame
        Corridor polygons (for map display and GIS export).
    reduced_buildable : GeoDataFrame
        Buildable area with corridor zones subtracted.
    corridor_info : dict
        Metadata for downstream routing:
        ``spine_line``, ``branch_lines``, ``branch_spacing_m``,
        ``spine_direction``, ``main_collector_width_m``,
        ``tertiary_aisle_lines``.
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
    logger.info("  Generating terrain-aware spine road...")
    
    # Calculate the overall long axis (needed for downstream orientation)
    angle_deg, (ux, uy) = _long_axis_direction(buildable_geom)
    
    # 1. Find the furthest point in the buildable area from the substation
    import math
    pts = []
    if buildable_geom.geom_type == 'Polygon':
        pts = list(buildable_geom.exterior.coords)
    elif hasattr(buildable_geom, 'geoms'):
        for g in buildable_geom.geoms:
            if hasattr(g, 'exterior'):
                pts.extend(list(g.exterior.coords))
                
    best_pt = None
    best_d = -1
    for coord in pts:
        d = math.hypot(coord[0]-substation_point.x, coord[1]-substation_point.y)
        if d > best_d:
            best_d = d
            best_pt = Point(coord)

    spine_line = None
    if best_pt:
        try:
            grid = AStarTerrainGrid(buildable_geom, cell_size_m=10)
            spine_line = grid.astar(substation_point, best_pt)
        except Exception as e:
            logger.warning(f"  AStar terrain routing failed: {e}")

    # Fallback to straight line axis
    if spine_line is None or spine_line.is_empty:
        centroid = buildable_geom.centroid
        if (centroid.x - substation_point.x) * ux + (centroid.y - substation_point.y) * uy < 0:
            ux, uy = -ux, -uy
        spine_line = _extend_line_to_boundary(substation_point, (ux, uy), buildable_geom)

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

    branch_lines = []
    branch_corridors = []

    # Walk along spine and create branches at regular intervals
    n_branches = max(1, int(spine_length / branch_spacing))
    for i in range(1, n_branches + 1):
        dist_along = i * branch_spacing
        if dist_along >= spine_length - branch_spacing / 4:
            break

        try:
            branch_origin = spine_line.interpolate(dist_along)
            
            # Calculate local tangent to ensure branches are perfectly perpendicular to the curving spine
            delta = 1.0
            pt1 = spine_line.interpolate(max(0, dist_along - delta))
            pt2 = spine_line.interpolate(min(spine_length, dist_along + delta))
            dx = pt2.x - pt1.x
            dy = pt2.y - pt1.y
            length = math.hypot(dx, dy)
            if length == 0:
                continue
            ux, uy = dx/length, dy/length
            px, py = -uy, ux  # Local perpendicular
            
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

    # ── Task 3.2b: Tertiary aisles (R2 — Road-First Tessellation) ──
    # Generate terrain-guided intra-block access aisles using the slope raster.
    # These replace the post-generation centroid canyon previously carved in _finalize_block.
    roads_cfg = config.get("roads", {})
    tertiary_enabled = roads_cfg.get("tertiary_aisles_enabled", False)
    tertiary_aisle_polys = []
    tertiary_aisle_lines = []

    # We sample aisles against the buildable area post main/branch corridor subtraction.
    # Use a provisional difference to get a realistic cell extent.
    provisional_reduced = buildable_geom.difference(
        unary_union([spine_corridor] + branch_corridors)
    ).buffer(0)

    if tertiary_enabled and not provisional_reduced.is_empty:
        tertiary_aisle_polys, tertiary_aisle_lines = _generate_tertiary_aisles(
            provisional_reduced, terrain_paths, config
        )
    elif not tertiary_enabled:
        logger.info("  Tertiary aisles disabled (roads.tertiary_aisles_enabled: false). "
                    "Intra-block roads will be carved geometrically post-layout.")

    # ── Merge all corridors ──
    all_corridor_geoms = [spine_corridor] + branch_corridors + tertiary_aisle_polys
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
    # Record tertiary aisles in corridor GDF for GIS export / map display
    aisle_width_cfg = roads_cfg.get("tertiary_aisle_width_m", 6)
    for ap in tertiary_aisle_polys:
        clipped_a = ap.intersection(buildable_geom)
        if not clipped_a.is_empty:
            corridor_records.append({
                "corridor_type": "tertiary_aisle",
                "width_m": aisle_width_cfg,
                "length_m": round(clipped_a.length, 1),
                "geometry": clipped_a,
            })

    corridor_gdf = gpd.GeoDataFrame(corridor_records, crs=crs)

    # Subtract corridors from buildable area
    corridor_union = corridor_gdf.geometry.union_all().buffer(0)
    
    total_corridor_ha = corridor_union.area / 10000
    logger.info(f"  Total corridor area: {total_corridor_ha:.2f} ha")

    # ── Task 3.3: Subtract corridors from buildable area ──
    reduced_geom = buildable_geom.difference(corridor_union).buffer(0)

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
        # R2: terrain-guided tertiary aisle centrelines for downstream road snapping
        "tertiary_aisle_lines": tertiary_aisle_lines,
    }

    return corridor_gdf, reduced_buildable, corridor_info
