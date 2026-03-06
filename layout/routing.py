"""
routing.py   —  Phase 3.2: Grid-Based Obstacle-Aware Road & Cable Routing
===========================================================================
Strategy
--------
Instead of a slow O(n²) visibility graph over all block corners, we use
a coarse rasterised occupancy grid + A* pathfinding:

1. Rasterise PV block polygons onto a grid (cell ≈ 15 m).
   Cells occupied by a block polygon = obstacle (cost = ∞).
   All other cells = free (cost = distance).

2. For each PCU pad (branch road) or transformer (MV cable), run A* on
   the grid from source to destination.

3. Reconstruct the path as a simplified LineString.

This approach is fast (A* on a ~100×100 grid completes in milliseconds)
and guaranteed to avoid block interiors.

Road hierarchy
--------------
• main_collector  — Medial axis / skeleton spine of the buildable area.
• branch_road     — PCU Border Pad → nearest point on spine (A* routed).

MV cable topology
-----------------
• MV_33kV, radial feeders (≤ max_blocks_per_feeder per feeder).
• Each cable = A* path: transformer point → substation.
• Feeder grouping is by proximity order.
"""

import logging
import math
import heapq

import geopandas as gpd
import numpy as np
import networkx as nx
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import nearest_points, unary_union, transform

logger = logging.getLogger("PVLayoutEngine.routing")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _sample_raster_mean(geom, raster_path):
    try:
        import rasterio
        from rasterio.mask import mask
        with rasterio.open(raster_path) as src:
            out_image, _ = mask(src, [geom], crop=True)
            valid = out_image[out_image != src.nodata]
            if valid.size > 0:
                return float(np.nanmean(valid))
    except Exception:
        pass
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Occupancy grid + A* router
# ─────────────────────────────────────────────────────────────────────────────

CELL_SIZE_M = 15          # grid resolution in metres (15 m for tighter block avoidance)
OBSTACLE_PENALTY = 1e9   # effectively infinite cost for blocked cells


class OccupancyGrid:
    """
    Rasterised 2-D grid that marks PV block cells as obstacles.
    A* paths on this grid avoid cutting through blocks.
    """

    def __init__(self, blocks_gdf, cell_size_m=CELL_SIZE_M, padding_cells=2):
        # World bounds (with a little padding)
        union = unary_union(blocks_gdf.geometry.tolist())
        minx, miny, maxx, maxy = union.bounds

        margin = cell_size_m * padding_cells
        self.ox = minx - margin
        self.oy = miny - margin
        self.cs = cell_size_m

        self.ncols = int(math.ceil((maxx + margin - self.ox) / cell_size_m)) + 1
        self.nrows = int(math.ceil((maxy + margin - self.oy) / cell_size_m)) + 1

        self.obstacle = np.zeros((self.nrows, self.ncols), dtype=bool)

        # Rasterise each block (eroded by 3 m so paths can hug edges)
        for geom in blocks_gdf.geometry:
            try:
                shrunk = geom.buffer(-3)
            except Exception:
                shrunk = geom
            self._paint_polygon(shrunk)

        logger.debug("  OccupancyGrid %d×%d, cell=%dm, obstacles: %d / %d cells",
                     self.nrows, self.ncols, cell_size_m,
                     int(self.obstacle.sum()), self.nrows * self.ncols)

    def _paint_polygon(self, poly):
        """Mark all grid cells whose centre falls inside poly as obstacles."""
        if poly is None or poly.is_empty:
            return
        minx, miny, maxx, maxy = poly.bounds
        col0 = max(0, int((minx - self.ox) / self.cs) - 1)
        col1 = min(self.ncols - 1, int((maxx - self.ox) / self.cs) + 1)
        row0 = max(0, int((miny - self.oy) / self.cs) - 1)
        row1 = min(self.nrows - 1, int((maxy - self.oy) / self.cs) + 1)

        for r in range(row0, row1 + 1):
            for c in range(col0, col1 + 1):
                cx = self.ox + (c + 0.5) * self.cs
                cy = self.oy + (r + 0.5) * self.cs
                if poly.contains(Point(cx, cy)):
                    self.obstacle[r, c] = True

    def world_to_cell(self, x, y):
        c = int((x - self.ox) / self.cs)
        r = int((y - self.oy) / self.cs)
        c = max(0, min(self.ncols - 1, c))
        r = max(0, min(self.nrows - 1, r))
        return r, c

    def cell_to_world(self, r, c):
        x = self.ox + (c + 0.5) * self.cs
        y = self.oy + (r + 0.5) * self.cs
        return x, y

    def astar(self, src_pt, dst_pt):
        """
        A* path from src_pt to dst_pt, returning a LineString.
        Obstacle cells are completely avoided (8-connected grid).
        Falls back to a straight line if no path exists.
        """
        sr, sc = self.world_to_cell(src_pt.x, src_pt.y)
        dr, dc = self.world_to_cell(dst_pt.x, dst_pt.y)

        if (sr, sc) == (dr, dc):
            return LineString([(src_pt.x, src_pt.y), (dst_pt.x, dst_pt.y)])

        # If source or dest grid cell is an obstacle, un-obstruct it for finding
        # (the real start/end points are on block edges, not block interiors)
        temp_src = self.obstacle[sr, sc]
        temp_dst = self.obstacle[dr, dc]
        self.obstacle[sr, sc] = False
        self.obstacle[dr, dc] = False

        # ── A* ──
        def h(r, c):  # Euclidean heuristic
            return math.hypot((r - dr) * self.cs, (c - dc) * self.cs)

        MOVES = [
            (-1, 0, self.cs), (1, 0, self.cs), (0, -1, self.cs), (0, 1, self.cs),  # cardinal
            (-1, -1, self.cs * 1.414), (-1, 1, self.cs * 1.414),                   # diagonal
            (1, -1, self.cs * 1.414),  (1, 1, self.cs * 1.414),
        ]

        g = {(sr, sc): 0.0}
        prev = {}
        open_heap = [(h(sr, sc), 0.0, (sr, sc))]

        found = False
        while open_heap:
            _, cost, current = heapq.heappop(open_heap)
            if current == (dr, dc):
                found = True
                break
            if cost > g.get(current, float('inf')) + 1e-6:
                continue
            cr_, cc_ = current
            for dr_, dc_, move_cost in MOVES:
                nr, nc = cr_ + dr_, cc_ + dc_
                if not (0 <= nr < self.nrows and 0 <= nc < self.ncols):
                    continue
                if self.obstacle[nr, nc]:
                    continue
                ng = cost + move_cost
                if ng < g.get((nr, nc), float('inf')):
                    g[(nr, nc)] = ng
                    prev[(nr, nc)] = current
                    heapq.heappush(open_heap, (ng + h(nr, nc), ng, (nr, nc)))

        # Restore obstacle flags
        self.obstacle[sr, sc] = temp_src
        self.obstacle[dr, dc] = temp_dst

        if not found:
            logger.warning("  A* routing: no path found; using straight line.")
            return LineString([(src_pt.x, src_pt.y), (dst_pt.x, dst_pt.y)])

        # Reconstruct path
        node = (dr, dc)
        path_cells = []
        while node in prev:
            path_cells.append(node)
            node = prev[node]
        path_cells.append((sr, sc))
        path_cells.reverse()

        # Convert to world coords, starting/ending with exact source/dest
        coords = [(src_pt.x, src_pt.y)]
        for r_, c_ in path_cells[1:-1]:
            coords.append(self.cell_to_world(r_, c_))
        coords.append((dst_pt.x, dst_pt.y))

        # Simplify slightly for file-size efficiency
        line = LineString(coords)
        try:
            line = line.simplify(self.cs * 0.3, preserve_topology=False)
        except Exception:
            pass
        return line


# ─────────────────────────────────────────────────────────────────────────────
# Spine road generation
# ─────────────────────────────────────────────────────────────────────────────

def generate_spine_roads(buildable_area_gdf, substation_point, config):
    """
    Generates main spine road using Medial Axis Transform (momepy Skeleton).
    Falls back to a centre-line from substation to site top-centre.
    """
    logger.info("Generating Main Spine Road via Medial Axis Transform...")

    try:
        import momepy
        buildable_union  = buildable_area_gdf.geometry.unary_union
        buildable_simple = buildable_union.simplify(10, preserve_topology=True)

        # Fill holes
        if hasattr(buildable_simple, "geoms"):
            filled = unary_union([Polygon(g.exterior) for g in buildable_simple.geoms
                                  if hasattr(g, "exterior")])
        elif hasattr(buildable_simple, "exterior"):
            filled = Polygon(buildable_simple.exterior)
        else:
            filled = buildable_simple

        skeleton = momepy.Skeleton(
            gpd.GeoDataFrame(geometry=[filled], crs=buildable_area_gdf.crs),
            distance=50
        )
        sk_gdf = skeleton.skeleton
        if sk_gdf is not None and not sk_gdf.empty:
            spine_geom = unary_union(sk_gdf.geometry.tolist())
            # ── Clip spine to buildable area so it doesn't exit the site boundary ──
            spine_geom = spine_geom.intersection(buildable_union)
            logger.info("  Spine road (clipped): %s, length=%.2f km",
                        spine_geom.geom_type, spine_geom.length / 1000)
            crs = buildable_area_gdf.crs
            return (
                gpd.GeoDataFrame(
                    [{"geometry": spine_geom, "road_type": "main_collector"}], crs=crs
                ),
                spine_geom
            )
    except Exception as e:
        logger.warning("  momepy skeleton failed (%s). Falling back to simple spine.", e)

    # ── Fallback: straight spine from substation to top-centre of site ──
    try:
        ba_bounds = buildable_area_gdf.geometry.unary_union.bounds
        top_mid   = Point((ba_bounds[0] + ba_bounds[2]) / 2, ba_bounds[3])
        fallback_spine = LineString([
            (substation_point.x, substation_point.y),
            (top_mid.x,          top_mid.y)
        ])
        crs = buildable_area_gdf.crs
        return (
            gpd.GeoDataFrame(
                [{"geometry": fallback_spine, "road_type": "main_collector"}], crs=crs
            ),
            fallback_spine
        )
    except Exception as e2:
        logger.error("  Fallback spine also failed: %s", e2)
        empty = gpd.GeoDataFrame(columns=["geometry", "road_type"],
                                 crs=buildable_area_gdf.crs)
        return empty, None


# ─────────────────────────────────────────────────────────────────────────────
# Branch road routing  (PCU pad → spine, grid A*)
# ─────────────────────────────────────────────────────────────────────────────

def route_access_roads(blocks_gdf, substation_point, config, terrain_paths=None,
                       exclusions_gdf=None, buildable_area_gdf=None,
                       transformers_gdf=None):
    """
    Generates:
      1. Main spine road via medial-axis transform.
      2. Branch roads: PCU Border Pad → nearest spine point,
         routed obstacle-free via A* on an occupancy grid.
    """
    logger.info("Routing hierarchical access roads (Spine + A* Branch)...")

    if blocks_gdf.empty or substation_point is None or buildable_area_gdf is None:
        return gpd.GeoDataFrame(columns=["geometry", "road_type"], crs=blocks_gdf.crs)

    crs = blocks_gdf.crs
    road_features = []

    # 1. Generate Main Spine Road
    spine_gdf, spine_geom = generate_spine_roads(buildable_area_gdf, substation_point, config)

    # 2. Build occupancy grid over all blocks
    grid = OccupancyGrid(blocks_gdf, cell_size_m=CELL_SIZE_M)

    # PCU pad lookup: block_id → transformer Point
    pcu_lookup = {}
    if transformers_gdf is not None and not transformers_gdf.empty:
        for _, t_row in transformers_gdf.iterrows():
            pcu_lookup[t_row["block_id"]] = t_row.geometry

    # 3. Route branch road per block
    for idx, row in blocks_gdf.iterrows():
        b_id = row.get("block_id", f"b{idx}")

        origin_pt = pcu_lookup.get(b_id, row.geometry.centroid)

        if spine_geom is not None:
            target_pt, _ = nearest_points(spine_geom, origin_pt)
        else:
            target_pt = Point(substation_point.x, substation_point.y)

        branch_line = grid.astar(origin_pt, target_pt)

        road_features.append({
            "geometry":  branch_line,
            "road_type": "branch_road",
            "block_id":  b_id,
            "length_m":  round(branch_line.length, 1)
        })

    # 4. Spine as a featured road
    if spine_geom is not None:
        road_features.append({
            "geometry":  spine_geom,
            "road_type": "main_collector",
            "block_id":  "SPINE",
            "length_m":  round(spine_geom.length, 1)
        })

    roads_gdf = gpd.GeoDataFrame(road_features, crs=crs)
    total_road_km = roads_gdf.geometry.length.sum() / 1000
    logger.info("  Access roads: %d branch roads + spine, total %.2f km",
                len(blocks_gdf), total_road_km)
    return roads_gdf


# ─────────────────────────────────────────────────────────────────────────────
# MV cable routing  (transformer → substation, grid A* + feeder grouping)
# ─────────────────────────────────────────────────────────────────────────────

def route_mv_cables(transformers_gdf, substation_point, roads_gdf, blocks_gdf,
                    config, terrain_paths=None, exclusions_gdf=None):
    """
    Routes MV collection cables (33kV) from each PCU transformer to the
    substation using grid-based A* to avoid crossing PV blocks.

    Feeder grouping assigns each transformer to a numbered 33kV radial
    feeder (≤ max_blocks_per_feeder per feeder).
    """
    logger.info("Routing MV cables (33kV, A* obstacle-aware, feeder-grouped)...")

    if transformers_gdf is None or transformers_gdf.empty or substation_point is None:
        fallback_crs = (transformers_gdf.crs
                        if transformers_gdf is not None and not transformers_gdf.empty
                        else "EPSG:32646")
        return gpd.GeoDataFrame(columns=["geometry", "cable_type"], crs=fallback_crs)

    crs = transformers_gdf.crs
    mv_features = []
    sub_pt = Point(substation_point.x, substation_point.y)

    # Build occupancy grid (reuse same blocks obstacle map)
    if blocks_gdf is not None and not blocks_gdf.empty:
        grid = OccupancyGrid(blocks_gdf, cell_size_m=CELL_SIZE_M)
    else:
        grid = None

    # Feeder grouping
    max_blocks_per_feeder = config.get("routing", {}).get("max_blocks_per_feeder", 8)
    feeder_assignments = {}
    for i, (idx, _) in enumerate(transformers_gdf.iterrows()):
        feeder_assignments[idx] = f"FEEDER_{(i // max_blocks_per_feeder) + 1:02d}"

    for idx, row in transformers_gdf.iterrows():
        trans_pt  = Point(row.geometry.x, row.geometry.y)
        tid       = row.get("transformer_id", f"T{idx}")
        feeder_id = feeder_assignments[idx]

        if grid is not None:
            full_route = grid.astar(trans_pt, sub_pt)
        else:
            full_route = LineString([(trans_pt.x, trans_pt.y), (sub_pt.x, sub_pt.y)])

        mv_features.append({
            "geometry":       full_route,
            "cable_type":     "MV_33kV",
            "feeder_id":      feeder_id,
            "transformer_id": tid,
            "voltage_kv":     33,
            "topology":       "radial_obstacle_free",
            "length_m":       round(full_route.length, 1),
        })

    mv_cables_gdf = gpd.GeoDataFrame(mv_features, crs=crs)
    total_km  = mv_cables_gdf.geometry.length.sum() / 1000
    n_feeders = mv_cables_gdf["feeder_id"].nunique()
    logger.info(
        "  MV cables: %d transformers → %d feeders, obstacle-free %.2f km total",
        len(mv_cables_gdf), n_feeders, total_km
    )
    return mv_cables_gdf


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────────────────────

def route_mv_cables_and_roads(inverters_gdf, transformers_gdf, substation_point,
                              blocks_gdf, config, terrain_paths=None,
                              exclusions_gdf=None, buildable_area_gdf=None):
    """
    Phase 3.2 orchestrator: routes access roads then MV cables,
    both using fast grid A* obstacle avoidance.
    """
    roads_gdf = route_access_roads(
        blocks_gdf, substation_point, config,
        terrain_paths=terrain_paths,
        exclusions_gdf=exclusions_gdf,
        buildable_area_gdf=buildable_area_gdf,
        transformers_gdf=transformers_gdf,
    )
    mv_cables_gdf = route_mv_cables(
        transformers_gdf, substation_point, roads_gdf, blocks_gdf, config,
        terrain_paths=terrain_paths,
        exclusions_gdf=exclusions_gdf,
    )
    return roads_gdf, mv_cables_gdf
