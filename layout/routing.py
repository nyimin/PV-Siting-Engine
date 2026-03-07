"""
routing.py   —  Phase 5: Road-Following MV Routing & Gradient-Aware Roads
===========================================================================
Road hierarchy
--------------
• main_collector       — Corridor spine road from substation along site long axis.
• secondary_collector  — Perpendicular corridor branch roads (herringbone pattern).
• branch_road          — PCU pad → nearest corridor line (A* routed connectors).

MV cable topology (Phase 5)
---------------------------
• MV_33kV, radial feeders grouped by spatial K-means clustering.
• Cables route along the existing road network (NetworkX shortest path).
• Blocks on the same feeder share trunk cable segments along the collector.
• Cable sized per feeder load (IEC 60502-2 XLPE Al), voltage drop computed.
"""

import logging
import math
import heapq

import geopandas as gpd
import numpy as np
import networkx as nx
import rasterio
from shapely.geometry import LineString, Point, MultiLineString, Polygon
from shapely.ops import nearest_points, unary_union, linemerge, snap

from utils.raster_helpers import sample_raster_mean as _sample_raster_mean

logger = logging.getLogger("PVLayoutEngine.routing")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

# _sample_raster_mean imported from utils.raster_helpers


def _sample_slope_grid(slope_path, ox, oy, cs, nrows, ncols):
    """Sample slope raster values onto the occupancy grid cells.

    Returns a (nrows, ncols) array of slope in degrees at each cell centre.
    Cells outside the raster extent get 0 (flat, no penalty).
    """
    slope_deg = np.zeros((nrows, ncols), dtype=np.float32)
    try:
        with rasterio.open(slope_path) as src:
            for r in range(nrows):
                for c in range(ncols):
                    cx = ox + (c + 0.5) * cs
                    cy = oy + (r + 0.5) * cs
                    try:
                        row_px, col_px = src.index(cx, cy)
                        if 0 <= row_px < src.height and 0 <= col_px < src.width:
                            val = src.read(1, window=rasterio.windows.Window(col_px, row_px, 1, 1))
                            slope_deg[r, c] = float(val[0, 0])
                    except Exception:
                        pass
    except Exception as e:
        logger.debug("  Could not sample slope raster for gradient enforcement: %s", e)
    return slope_deg


# ─────────────────────────────────────────────────────────────────────────────
# Occupancy grid + A* router (with gradient enforcement)
# ─────────────────────────────────────────────────────────────────────────────

CELL_SIZE_M = 15          # grid resolution in metres (15 m for tighter block avoidance)
OBSTACLE_PENALTY = 1e9   # effectively infinite cost for blocked cells
GRADIENT_PENALTY = 10.0  # cost multiplier for cells exceeding max gradient


class OccupancyGrid:
    """
    Rasterised 2-D grid that marks PV block cells as obstacles.
    A* paths on this grid avoid cutting through blocks.
    Optional slope-aware gradient enforcement penalises steep cells.
    """

    def __init__(self, blocks_gdf, cell_size_m=CELL_SIZE_M, padding_cells=2,
                 slope_raster_path=None, max_gradient_pct=None):
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

        # ── Gradient enforcement (Task 4.4) ──
        # Sample slope raster and compute cost multiplier per cell
        self.slope_deg = None
        self.gradient_cost = np.ones((self.nrows, self.ncols), dtype=np.float32)
        if slope_raster_path and max_gradient_pct is not None:
            try:
                self.slope_deg = _sample_slope_grid(
                    slope_raster_path, self.ox, self.oy, self.cs,
                    self.nrows, self.ncols
                )
                # Convert max gradient % to degrees:  tan(angle) = pct/100
                max_gradient_deg = math.degrees(math.atan(max_gradient_pct / 100.0))
                steep_mask = self.slope_deg > max_gradient_deg
                self.gradient_cost[steep_mask] = GRADIENT_PENALTY
                n_steep = int(steep_mask.sum())
                logger.info("  Gradient enforcement: %d / %d cells exceed %.1f%% "
                            "(%.1f°) → 10× cost multiplier",
                            n_steep, self.nrows * self.ncols,
                            max_gradient_pct, max_gradient_deg)
            except Exception as e:
                logger.warning("  Gradient enforcement failed: %s", e)

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
        Steep cells get a gradient-based cost penalty.
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
                # Apply gradient penalty (Task 4.4)
                effective_cost = move_cost * self.gradient_cost[nr, nc]
                ng = cost + effective_cost
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
# Road width polygon modelling (Task 4.3)
# ─────────────────────────────────────────────────────────────────────────────

def _buffer_roads_to_polygons(roads_gdf, config):
    """Buffer road centrelines by their configured width to compute road surface area.

    Adds 'road_width_m' and 'road_surface_m2' numeric columns (JSON-safe).
    Does NOT store polygon objects in the GeoDataFrame to avoid serialisation issues.
    """
    roads_cfg = config.get("roads", {})
    main_width = roads_cfg.get("main_collector_width_m", 6)
    aisle_width = roads_cfg.get("maintenance_aisle_width_m", 4)

    width_lookup = {
        "main_collector":      main_width,
        "secondary_collector": aisle_width,
        "branch_road":         aisle_width,
    }

    widths = []
    areas = []
    for _, row in roads_gdf.iterrows():
        road_type = row.get("road_type", "branch_road")
        w = width_lookup.get(road_type, aisle_width)
        widths.append(w)
        try:
            poly = row.geometry.buffer(w / 2.0, cap_style="flat")
            areas.append(round(poly.area, 1))
        except Exception:
            areas.append(round(row.geometry.length * w, 1))

    roads_gdf["road_width_m"] = widths
    roads_gdf["road_surface_m2"] = areas

    total_area_ha = sum(areas) / 10000
    logger.info("  Road surface polygons: %.2f ha total paved area", total_area_ha)

    return roads_gdf


# ─────────────────────────────────────────────────────────────────────────────
# Branch road routing  (PCU pad → nearest corridor line, grid A*)
# ─────────────────────────────────────────────────────────────────────────────

def route_access_roads(blocks_gdf, substation_point, config, terrain_paths=None,
                       exclusions_gdf=None, buildable_area_gdf=None,
                       transformers_gdf=None, corridor_info=None):
    """
    Generates hierarchical access roads.
    When corridor_info is provided (Phase 3+), uses the pre-planned spine and
    branch lines as road centrelines. Otherwise falls back to a simple straight
    line from substation to site centroid.
    """
    logger.info("Routing hierarchical access roads...")

    if blocks_gdf.empty or substation_point is None or buildable_area_gdf is None:
        return gpd.GeoDataFrame(columns=["geometry", "road_type"], crs=blocks_gdf.crs)

    crs = blocks_gdf.crs
    road_features = []

    # 1. Get spine road — from corridor info or simple fallback
    if corridor_info and "spine_line" in corridor_info:
        spine_geom = corridor_info["spine_line"]
        logger.info("  Using pre-planned corridor spine road (%.0fm)", spine_geom.length)
        # Build composite road network from corridors for branch snapping
        branch_corridor_lines = corridor_info.get("branch_lines", [])
        all_road_lines = [spine_geom] + branch_corridor_lines
        combined_road_network = unary_union(all_road_lines)
    else:
        # Task 4.1: Simple fallback — straight line from substation to site centroid.
        # The old generate_spine_roads() medial-axis function has been removed.
        logger.info("  No corridor info — using straight-line spine from substation to site centroid")
        ba_union = buildable_area_gdf.geometry.union_all()
        centroid = ba_union.centroid
        spine_geom = LineString([
            (substation_point.x, substation_point.y),
            (centroid.x, centroid.y),
        ])
        combined_road_network = spine_geom
        branch_corridor_lines = []

    # 2. Build occupancy grid over all blocks (with gradient enforcement)
    slope_path = terrain_paths.get("slope") if terrain_paths else None
    max_gradient_pct = config.get("roads", {}).get("max_gradient_pct", 5)
    grid = OccupancyGrid(
        blocks_gdf, cell_size_m=CELL_SIZE_M,
        slope_raster_path=slope_path,
        max_gradient_pct=max_gradient_pct,
    )

    # PCU pad lookup: block_id → transformer Point
    pcu_lookup = {}
    if transformers_gdf is not None and not transformers_gdf.empty:
        for _, t_row in transformers_gdf.iterrows():
            pcu_lookup[t_row["block_id"]] = t_row.geometry

    # 3. Route branch road per block (Task 4.2: with corridor branch attribution)
    for idx, row in blocks_gdf.iterrows():
        b_id = row.get("block_id", f"b{idx}")

        origin_pt = pcu_lookup.get(b_id, row.geometry.centroid)

        # Snap to nearest point on combined road network (spine + branches)
        corridor_branch_id = "SPINE"
        if combined_road_network is not None:
            target_pt, _ = nearest_points(combined_road_network, origin_pt)

            # Task 4.2: Attribute which corridor line this branch road connects to
            if branch_corridor_lines:
                best_dist = spine_geom.distance(origin_pt)
                corridor_branch_id = "SPINE"
                for j, bline in enumerate(branch_corridor_lines):
                    d = bline.distance(origin_pt)
                    if d < best_dist:
                        best_dist = d
                        corridor_branch_id = f"BRANCH_{j+1:02d}"
        else:
            target_pt = Point(substation_point.x, substation_point.y)

        branch_line = grid.astar(origin_pt, target_pt)

        road_features.append({
            "geometry":  branch_line,
            "road_type": "branch_road",
            "block_id":  b_id,
            "corridor_branch_id": corridor_branch_id,
            "length_m":  round(branch_line.length, 1)
        })

    # 4. Spine as a featured road
    if spine_geom is not None:
        road_features.append({
            "geometry":  spine_geom,
            "road_type": "main_collector",
            "block_id":  "SPINE",
            "corridor_branch_id": "SPINE",
            "length_m":  round(spine_geom.length, 1)
        })

    # 4b. Add corridor branch lines as secondary collector roads
    for j, bline in enumerate(branch_corridor_lines):
        road_features.append({
            "geometry": bline,
            "road_type": "secondary_collector",
            "block_id": f"BRANCH_{j+1:02d}",
            "corridor_branch_id": f"BRANCH_{j+1:02d}",
            "length_m": round(bline.length, 1),
        })

    roads_gdf = gpd.GeoDataFrame(road_features, crs=crs)

    # Task 4.3: Add road surface polygons
    roads_cfg = config.get("roads", {})
    if roads_cfg.get("road_surface_buffer", True):
        roads_gdf = _buffer_roads_to_polygons(roads_gdf, config)

    total_road_km = roads_gdf.geometry.length.sum() / 1000
    logger.info("  Access roads: %d branch + %d secondary + spine, total %.2f km",
                len(blocks_gdf), len(branch_corridor_lines), total_road_km)
    return roads_gdf


# ─────────────────────────────────────────────────────────────────────────────
# Road network graph (Task 5.1)
# ─────────────────────────────────────────────────────────────────────────────

def _build_road_graph(roads_gdf, substation_point, tolerance=2.0):
    """Build a NetworkX graph from road centreline geometries.

    Nodes = distinct road-endpoint coordinates (snapped to tolerance).
    Edges = road segments weighted by length (m).
    Returns the graph and a dict of node_id → Point.
    """
    G = nx.Graph()

    def _snap_coord(x, y):
        """Round to tolerance grid to merge nearby endpoints."""
        return (round(x / tolerance) * tolerance,
                round(y / tolerance) * tolerance)

    edge_id = 0
    for _, row in roads_gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        # Handle both LineString and MultiLineString
        lines = [geom] if geom.geom_type == "LineString" else list(geom.geoms)
        for line in lines:
            coords = list(line.coords)
            if len(coords) < 2:
                continue
            # Discretise at ~50m intervals for better connectivity
            total_len = line.length
            n_segments = max(1, int(total_len / 50))
            prev_node = _snap_coord(coords[0][0], coords[0][1])
            G.add_node(prev_node, x=prev_node[0], y=prev_node[1])
            for seg_i in range(1, n_segments + 1):
                frac = seg_i / n_segments
                pt = line.interpolate(frac, normalized=True)
                cur_node = _snap_coord(pt.x, pt.y)
                G.add_node(cur_node, x=cur_node[0], y=cur_node[1])
                seg_len = math.hypot(cur_node[0] - prev_node[0],
                                     cur_node[1] - prev_node[1])
                if seg_len > 0 and prev_node != cur_node:
                    G.add_edge(prev_node, cur_node, weight=seg_len,
                               edge_id=edge_id)
                    edge_id += 1
                prev_node = cur_node

    # Add substation as a node, connect to nearest road node
    sub_snap = _snap_coord(substation_point.x, substation_point.y)
    G.add_node(sub_snap, x=sub_snap[0], y=sub_snap[1])
    if len(G.nodes) > 1:
        best_node = None
        best_dist = float("inf")
        for n in G.nodes:
            if n == sub_snap:
                continue
            d = math.hypot(n[0] - sub_snap[0], n[1] - sub_snap[1])
            if d < best_dist:
                best_dist = d
                best_node = n
        if best_node and best_dist < 5000:
            G.add_edge(sub_snap, best_node, weight=best_dist)

    logger.info("  Road graph: %d nodes, %d edges", G.number_of_nodes(), G.number_of_edges())
    return G, sub_snap


def _route_on_road_graph(G, src_node, dst_node, src_pt, dst_pt, tolerance=2.0):
    """Route from src to dst on road graph. Returns LineString.

    Falls back to straight line if no graph path exists.
    """
    def _snap_coord(x, y):
        return (round(x / tolerance) * tolerance,
                round(y / tolerance) * tolerance)

    # Snap source to nearest graph node
    src_snap = _snap_coord(src_pt.x, src_pt.y)
    if src_snap not in G:
        # Find nearest node
        best_node = None
        best_dist = float("inf")
        for n in G.nodes:
            d = math.hypot(n[0] - src_pt.x, n[1] - src_pt.y)
            if d < best_dist:
                best_dist = d
                best_node = n
        if best_node:
            src_snap = best_node

    try:
        path_nodes = nx.shortest_path(G, src_snap, dst_node, weight="weight")
        coords = [(src_pt.x, src_pt.y)]  # exact start
        for node in path_nodes:
            coords.append((node[0], node[1]))
        coords.append((dst_pt.x, dst_pt.y))  # exact end
        line = LineString(coords)
        return line.simplify(tolerance, preserve_topology=False)
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return LineString([(src_pt.x, src_pt.y), (dst_pt.x, dst_pt.y)])


# ─────────────────────────────────────────────────────────────────────────────
# Spatial feeder grouping (Task 5.3)
# ─────────────────────────────────────────────────────────────────────────────

def _spatial_feeder_grouping(transformers_gdf, substation_point, max_blocks_per_feeder):
    """Group transformers into feeders using K-means spatial clustering.

    Returns dict mapping transformer DataFrame index → feeder_id string.
    """
    n_transformers = len(transformers_gdf)
    if n_transformers == 0:
        return {}

    n_feeders = max(1, math.ceil(n_transformers / max_blocks_per_feeder))

    # Extract coordinates
    coords = np.array([(g.x, g.y) for g in transformers_gdf.geometry])

    if n_feeders >= n_transformers:
        # Each transformer is its own feeder
        assignments = {}
        for i, idx in enumerate(transformers_gdf.index):
            assignments[idx] = f"FEEDER_{i+1:02d}"
        return assignments

    try:
        from sklearn.cluster import KMeans
        km = KMeans(n_clusters=n_feeders, n_init=10, random_state=42)
        labels = km.fit_predict(coords)

        # Sort cluster labels by distance from substation (nearest cluster = FEEDER_01)
        sub_xy = np.array([substation_point.x, substation_point.y])
        cluster_dists = []
        for c_id in range(n_feeders):
            mask = labels == c_id
            centroid = coords[mask].mean(axis=0)
            d = np.linalg.norm(centroid - sub_xy)
            cluster_dists.append((d, c_id))
        cluster_dists.sort()
        label_map = {old_id: new_rank + 1 for new_rank, (_, old_id) in enumerate(cluster_dists)}

        assignments = {}
        for i, idx in enumerate(transformers_gdf.index):
            feeder_num = label_map[labels[i]]
            assignments[idx] = f"FEEDER_{feeder_num:02d}"

        logger.info("  Spatial feeder grouping: %d transformers → %d feeders (K-means)",
                    n_transformers, n_feeders)
        return assignments

    except ImportError:
        logger.warning("  sklearn not available; falling back to sequential feeder assignment")
        assignments = {}
        for i, idx in enumerate(transformers_gdf.index):
            assignments[idx] = f"FEEDER_{(i // max_blocks_per_feeder) + 1:02d}"
        return assignments


# ─────────────────────────────────────────────────────────────────────────────
# Cable sizing & voltage drop (Task 5.4)
# ─────────────────────────────────────────────────────────────────────────────

# IEC 60502-2 33kV XLPE Aluminium conductor catalogue
_CABLE_CATALOGUE = [
    # (size_mm2, max_amps, R_ohm_per_km, X_ohm_per_km)
    (95,   240,  0.320, 0.110),
    (185,  355,  0.164, 0.100),
    (300,  460,  0.100, 0.093),
    (500,  590,  0.061, 0.087),
]


def _select_cable_and_vdrop(feeder_load_mw, cable_length_km, voltage_kv=33, pf=0.95):
    """Select minimum cable size for feeder load and compute voltage drop %.

    Parameters
    ----------
    feeder_load_mw : float  — total feeder load in MW
    cable_length_km : float — trunk cable length in km
    voltage_kv : float      — rated voltage (line-to-line)
    pf : float              — power factor (cos φ)

    Returns
    -------
    cable_size_mm2 : int
    voltage_drop_pct : float
    rated_current_a : float
    """
    V_ll = voltage_kv * 1000  # volts
    I_load = (feeder_load_mw * 1e6) / (math.sqrt(3) * V_ll * pf)  # amps
    sin_phi = math.sqrt(1 - pf ** 2)

    # Select smallest cable that can carry the load
    selected = _CABLE_CATALOGUE[-1]  # default to largest
    for size, max_a, R, X in _CABLE_CATALOGUE:
        if max_a >= I_load:
            selected = (size, max_a, R, X)
            break

    size_mm2, _, R, X = selected

    # Voltage drop: VD% = (√3 × I × L × (R·cosφ + X·sinφ)) / V_ll × 100
    vd_pct = (math.sqrt(3) * I_load * cable_length_km * (R * pf + X * sin_phi)) / V_ll * 100

    return size_mm2, round(vd_pct, 2), round(I_load, 1)


# ─────────────────────────────────────────────────────────────────────────────
# MV cable routing  (Phase 5: road-following + shared trench + cable sizing)
# ─────────────────────────────────────────────────────────────────────────────

def route_mv_cables(transformers_gdf, substation_point, roads_gdf, blocks_gdf,
                    config, terrain_paths=None, exclusions_gdf=None):
    """Routes MV collection cables (33kV) along the road network.

    Phase 5 improvements:
    - Cables route along existing road centrelines (NetworkX shortest path)
    - Feeder grouping uses spatial K-means (not sequential)
    - Cable sized per feeder (IEC 60502-2) with voltage drop calculation
    """
    logger.info("Routing MV cables (33kV, road-following, spatial feeders)...")

    if transformers_gdf is None or transformers_gdf.empty or substation_point is None:
        fallback_crs = (transformers_gdf.crs
                        if transformers_gdf is not None and not transformers_gdf.empty
                        else "EPSG:32646")
        return gpd.GeoDataFrame(columns=["geometry", "cable_type"], crs=fallback_crs)

    crs = transformers_gdf.crs
    mv_features = []
    sub_pt = Point(substation_point.x, substation_point.y)

    mv_cfg = config.get("mv_cables", {})
    voltage_kv = mv_cfg.get("voltage_kv", 33)
    pf = mv_cfg.get("power_factor", 0.95)
    max_vd = mv_cfg.get("max_voltage_drop_pct", 3.0)
    max_blocks_per_feeder = mv_cfg.get("max_blocks_per_feeder",
                                        config.get("routing", {}).get("max_blocks_per_feeder", 8))

    # Task 5.1: Build road network graph
    if roads_gdf is not None and not roads_gdf.empty:
        G, sub_node = _build_road_graph(roads_gdf, substation_point)
    else:
        G, sub_node = nx.Graph(), None

    # Task 5.3: Spatial feeder grouping (K-means)
    feeder_assignments = _spatial_feeder_grouping(
        transformers_gdf, substation_point, max_blocks_per_feeder
    )

    # Build A* occupancy grid as fallback
    grid = None
    if blocks_gdf is not None and not blocks_gdf.empty and G.number_of_edges() == 0:
        slope_path = terrain_paths.get("slope") if terrain_paths else None
        max_gradient_pct = config.get("roads", {}).get("max_gradient_pct", 5)
        grid = OccupancyGrid(
            blocks_gdf, cell_size_m=CELL_SIZE_M,
            slope_raster_path=slope_path,
            max_gradient_pct=max_gradient_pct,
        )

    # Collect per-block capacity for feeder load calculation
    block_capacity = {}
    if blocks_gdf is not None and not blocks_gdf.empty and "capacity_ac_mw" in blocks_gdf.columns:
        for _, brow in blocks_gdf.iterrows():
            block_capacity[brow.get("block_id", "")] = brow.get("capacity_ac_mw", 0)

    # ── Route each transformer to substation ──
    for idx, row in transformers_gdf.iterrows():
        trans_pt = Point(row.geometry.x, row.geometry.y)
        tid = row.get("transformer_id", f"T{idx}")
        feeder_id = feeder_assignments.get(idx, "FEEDER_01")
        block_id = row.get("block_id", "")

        # Task 5.1: Route along road graph
        if G.number_of_edges() > 0 and sub_node is not None:
            full_route = _route_on_road_graph(G, None, sub_node, trans_pt, sub_pt)
        elif grid is not None:
            full_route = grid.astar(trans_pt, sub_pt)
        else:
            full_route = LineString([(trans_pt.x, trans_pt.y), (sub_pt.x, sub_pt.y)])

        mv_features.append({
            "geometry":       full_route,
            "cable_type":     "MV_33kV",
            "feeder_id":      feeder_id,
            "transformer_id": tid,
            "block_id":       block_id,
            "voltage_kv":     voltage_kv,
            "topology":       "radial_road_following",
            "length_m":       round(full_route.length, 1),
        })

    mv_cables_gdf = gpd.GeoDataFrame(mv_features, crs=crs)

    # ── Task 5.4: Cable sizing & voltage drop per feeder ──
    feeder_ids = mv_cables_gdf["feeder_id"].unique()
    feeder_details = []

    for fid in sorted(feeder_ids):
        feeder_cables = mv_cables_gdf[mv_cables_gdf["feeder_id"] == fid]
        # Feeder load = sum of block AC capacities
        feeder_block_ids = feeder_cables["block_id"].tolist()
        feeder_load_mw = sum(block_capacity.get(bid, 0) for bid in feeder_block_ids)
        if feeder_load_mw <= 0:
            # Fallback: estimate from transformer count
            feeder_load_mw = len(feeder_cables) * 0.55  # ~0.55 MWac typical per block

        # Trunk length = longest cable in feeder (conservative — represents farthest block)
        trunk_length_km = feeder_cables["length_m"].max() / 1000

        cable_size, vd_pct, rated_current = _select_cable_and_vdrop(
            feeder_load_mw, trunk_length_km, voltage_kv, pf
        )

        # Annotate cables in this feeder
        mask = mv_cables_gdf["feeder_id"] == fid
        mv_cables_gdf.loc[mask, "cable_size_mm2"] = cable_size
        mv_cables_gdf.loc[mask, "voltage_drop_pct"] = vd_pct
        mv_cables_gdf.loc[mask, "feeder_load_mw"] = round(feeder_load_mw, 2)
        mv_cables_gdf.loc[mask, "rated_current_a"] = rated_current

        vd_flag = " ⚠️ EXCEEDS LIMIT" if vd_pct > max_vd else ""
        feeder_details.append({
            "feeder_id": fid,
            "n_blocks": len(feeder_cables),
            "load_mw": round(feeder_load_mw, 2),
            "trunk_km": round(trunk_length_km, 2),
            "cable_mm2": cable_size,
            "vd_pct": vd_pct,
            "current_a": rated_current,
        })
        logger.info("  %s: %d blocks, %.1f MW, %.1f km, %dmm² Al, VD=%.1f%%%s",
                    fid, len(feeder_cables), feeder_load_mw,
                    trunk_length_km, cable_size, vd_pct, vd_flag)

    total_km = mv_cables_gdf.geometry.length.sum() / 1000
    n_feeders = mv_cables_gdf["feeder_id"].nunique()
    max_vd_actual = mv_cables_gdf["voltage_drop_pct"].max() if "voltage_drop_pct" in mv_cables_gdf.columns else 0
    logger.info(
        "  MV cables: %d transformers → %d feeders, %.2f km total, max VD=%.1f%%",
        len(mv_cables_gdf), n_feeders, total_km, max_vd_actual
    )

    # Stash feeder_details on the GeoDataFrame for metrics downstream
    mv_cables_gdf.attrs["feeder_details"] = feeder_details

    return mv_cables_gdf


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────────────────────

def route_mv_cables_and_roads(inverters_gdf, transformers_gdf, substation_point,
                              blocks_gdf, config, terrain_paths=None,
                              exclusions_gdf=None, buildable_area_gdf=None,
                              corridor_info=None):
    """
    Orchestrator: routes access roads then MV cables.
    When corridor_info is provided, roads use pre-planned corridors.
    """
    roads_gdf = route_access_roads(
        blocks_gdf, substation_point, config,
        terrain_paths=terrain_paths,
        exclusions_gdf=exclusions_gdf,
        buildable_area_gdf=buildable_area_gdf,
        transformers_gdf=transformers_gdf,
        corridor_info=corridor_info,
    )
    mv_cables_gdf = route_mv_cables(
        transformers_gdf, substation_point, roads_gdf, blocks_gdf, config,
        terrain_paths=terrain_paths,
        exclusions_gdf=exclusions_gdf,
    )
    return roads_gdf, mv_cables_gdf
