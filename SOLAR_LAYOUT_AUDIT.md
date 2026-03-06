# PV Layout Engine — Solar Layout & Engineering Audit

**Date:** 2026-03-06
**Scope:** Audit of Solar Layout Generation, BOP Siting, and Infrastructure Routing
**Objective:** Compare current codebase against industry practices (e.g., PVcase, Helioscope, Aurora Solar) and propose a structured implementation roadmap.

---

## 1. Current Implementation Overview

Based strictly on the current codebase (`layout` and `constraints` modules), the system generates a conceptual solar layout as follows:

### Solar Layout Generation (`block_generator.py`)

- **Tessellation Grid:** The buildable area is divided into a rigid mathematical grid (defaulting to ~2.5 ha cells).
- **Row Placement:** Within each grid cell, East-West aligned 2P portrait fixed-tilt rows are generated. The distance between rows (pitch) is directly driven by the GCR parameter (`pitch = table_height / GCR`), with a secondary winter no-shade warning check.
- **Terrain Adaptation:** A local slope check optionally adjusting the row pitch dynamically for undulating terrain has been implemented via a local raster sample.
- **Constraints:** Rows are rejected if their centroid falls outside the cell, or if local slope and curvature exceed thresholds (e.g., >15° slope or >0.4 curvature).
- **Block Acceptance:** Grid cells are accepted as valid PV blocks if they meet a minimum physical utilisation (e.g., 60% `min_fill_fraction`).

### Balance of Plant (BOP) Siting (`substation_placement.py` & `bop_placement.py`)

- **Multi-Criteria Selection:** The substation location is selected by probing points along the site boundary and scoring them (flatness, proximity to centroid, roads, water body distance, and buildable area).
- **Compound Generation:** Geometric rectangles representing the Substation (80x60m), BESS (60x30m), O&M facility (100x50m), and Guard House are instantiated perpendicular to the inward array direction.
- **BOP Carve-Out:** These compounds form a unified "BOP Zone" that is iteratively shifted inwards to ensure it sits completely inside the site boundary and is then securely subtracted from the buildable area prior to PV layout.
- **Inverters & Transformers:** String inverters are placed at the geometrical ends of individual PV rows (one per approx. 22 strings), and block transformers are placed at the exact spatial centroid of each generated block.

### Infrastructure Routing (`routing.py`)

- **Internal Access Roads:** The engine creates an A* pathfinding grid over the *PV block geometry\*. Edges are weighted by distance and steep terrain slope. Shortest paths are then derived linking each block centroid to the substation node. All outputs are merged and generically labeled as `main_collector`.
- **MV Cables:** 33kV medium voltage (MV) cables are routed using straight line segments (`LineString`) connecting block transformers directly to the main substation.
- **LV Cables:** Low voltage interconnects are created as direct `LineString` links between the string inverters and the block transformer.

---

## 2. Comparison with Industry Practice

### Solar Layout

- **Industry Practice:** Commercial tools (e.g., PVcase) use advanced terrain-following placement where arrays are dropped in respecting setbacks dynamically. Instead of a rigid grid, they place continuous topological row domains across the site, which are then electrically clustered (via k-means or spatial slicing) into functional inverter blocks.
- **Codebase Deviation:** The system’s tessellation grid approach forces artificial rectilinear boundaries onto organic site shapes. This causes frequent discarding of viable corner spaces (which fail the 60% minimum fill test) leading to sub-optimal capacity utilisation.

### BOP Siting

- **Industry Practice:** Substation siting heavily prioritises existing HV infrastructure (transmission lines) and paved access above all else. BESS and O&M compounds are placed adjacent but are carefully zoned with strict fire-break setbacks (e.g., NFPA 855).
- **Codebase Deviation:** The code successfully implements compound carving, which mimics professional tools perfectly. However, the vector translation process that iteratively shifts the BOP zone inward toward the site centroid can push the compounds into conflicting, non-buildable complex terrain.

### Infrastructure Routing

- **Industry Practice:**
  1. _Roads:_ A strict hierarchy is deployed: a main spine road (e.g. 6m sealed) branches into secondary aisles (e.g. 4m gravel) that navigate _between_ blocks, avoiding arrays.
  2. _MV Cabling:_ Follows a "comb" or radial topology trenched entirely within the road corridor buffer, eliminating unnecessary secondary earthworks.
  3. _LV Cabling:_ Runs neatly within cable trays attached to the PV structural tables before dropping into local routing towards the transformer.
- **Codebase Deviation:**
  1. Internal road routing builds its grid nodes inside PV blocks. Roads are routed directly over PV panels.
  2. The network hierarchy is destroyed instantly via a global `dissolve()`.
  3. MV & LV cables are hard-coded as straight geographical lines, intersecting rows diagonally—a severe violation of physical constructability.

---

## 3. Identified Issues

### Critical (Layout Reliability Risk)

1. **Destructive Road Routing:** The `route_access_roads` generates grid graphs across block polygons instead of the spaces _between_ them. Roads intersect and wipe out PV rows entirely in reality.
2. **Straight-Line Cable Routing:** MV (`route_mv_cables`) and LV cables (`place_inverters_and_transformers`) use direct `LineString` trajectories over panels, ignoring the established road networks entirely.
3. **Rigid Tessellation Waste:** The unyielding block grid logic abandons significant viable terrain along irregular site boundaries due to the `min_fill_fraction` rule not supporting block-merging.

### Major (Engineering Realism Gaps)

4. **No Route Hierarchy:** All roads are clumped and classified as `main_collector`, stripping topological differentiation necessary for accurate EPC cost estimation.
5. **In-row Converter Placement:** String inverters are placed computationally near the row edge, but LV strings connect diagonally directly to centroids without row-edge routing.
6. **BOP Shifting:** Iterative shifting of the BOP zone (`bop_placement.py` / `substation_placement.py`) lacks topological guardrails, potentially stranding compounds offline.

### Minor (Optimization or Robustness)

7. **Vectorised Intersection Overhead:** Road routing node intersections and A\* snaps (`_snap_point_to_grid`) use suboptimal O(n²) or iterative operations without spatial indices (`R-tree`/`cKDTree`), creating significant scaling bottlenecks for >50MW plants.
8. **Inverter Fractional Spread:** Block capacity down-scaling applies mathematically when total strings don't divide cleanly, but physically, strings dictate precise DC capability. Mismatches emerge between stated block boundaries and true electrical limits.

---

## 4. Improvement Opportunities: The 5-Step Spatial Hierarchy Algorithm

Moving from just dropping module rows to generating a full conceptual Balance of Plant (BOP) layout requires a spatial paradigm shift. A robust conceptual layout generator processes geometries in a strict hierarchy: **from largest constraint to smallest**, subtracting geometries step-by-step to prevent overlaps.

Here is the recommended 5-Step Architectural Blueprint using `GeoPandas`, `Shapely`, `NetworkX`, and `momepy`:

**Step 1. Locate the Substation (The Anchor)**

- **Logic:** Identify the vertex of the buildable polygon nearest the grid POI or main access point.
- **Action:** Generate a fixed-size polygon for the substation.
- **Clip:** Subtract this substation polygon from the total buildable area.

**Step 2. Generate the Main Road Network (The Spine)**

- **Logic:** Use the **Medial Axis Transform** (via `momepy`, `scipy.spatial`, or `skimage`) to find the topological center-line (skeleton) of the irregular buildable polygon.
- **Action:** Connect the medial axis to the substation pad and buffer these lines by the road width (e.g., 6m).
- **Clip:** Subtract the buffered road polygons from the remaining buildable area. This acts as the physical routing trench for MV cables.

**Step 3. Subdivide into Inverter Blocks (The Grid)**

- **Logic:** Overlay a large grid (e.g., sized to 3–6 MW, roughly 4–8 hectares) across the remaining buildable area.
- **Action:** Intersect the grid with the site. Merge any undersized fragments (e.g., <1 ha) into adjacent cells. These distinct polygons become the physical "Blocks".

**Step 4. Place the Inverter Pads (PCUs) & Driveways**

- **Logic:** Find the `centroid` of each Block polygon.
- **Action:** Generate a PCU pad polygon at the centroid. Use `NetworkX` to route a shortest-path driveway from the PCU pad to the nearest internal spine road. Buffer this line for the driveway.
- **Clip:** Subtract the PCU pad and driveway from the Block polygon. (The PCU nodes and road network now form a clean graph for exact MV routing).

**Step 5. Populate the Modules**

- **Logic:** Iterate through every remaining Block polygon footprint.
- **Action:** Generate the solar rows (respecting azimuth, pitch, and string sizing) and intersect them _only_ with that specific block geometry.
- **Result:** Rows naturally terminate at internal roads, stop short of inverter pads, and cleanly respect site boundaries without risking intersection.

---

## 5. Implementation Plan

The following phased roadmap implements the 5-step spatial hierarchy sequentially, allowing incremental enhancements without crippling the existing geospatial pipeline:

### Phase 1: Substation & Medial Axis Spine Roads (Steps 1 & 2)

**Goal:** Establish the main topological anchor and skeleton of the site.

- **Module:** `layout/substation_placement.py` & `layout/routing.py`
- **Changes:**
  - Introduce `momepy` to calculate the medial axis of the global buildable area.
  - Generate the Substation compound near the POI vertex, then route the medial axis to it.
  - Buffer the network to form spine roads and subtract them from `buildable_area_gdf`.

### Phase 2: Block Subdivision & PCU Placement (Steps 3 & 4)

**Goal:** Create clean, non-overlapping electrical blocks and route their driveways.

- **Module:** `layout/block_generator.py` & `layout/routing.py`
- **Changes:**
  - Intersect a block grid over the remaining `buildable_area_gdf` and clean up small slivers.
  - Place PCU pads at block centroids.
  - Use `NetworkX` to trace shortest-path connections from each PCU to the medial axis spine road, forming branch roads and MV cable trenches. Subtract these from the blocks.

### Phase 3: Module Population (Step 5)

**Goal:** Fill the remaining clean block spaces with PV rows.

- **Module:** `layout/block_generator.py`
- **Changes:**
  - Transition the row generation script to iterate over the constrained block polygons, placing rows that naturally conform to the roads and PCU constraints.
  - Ensure row generation respects true GCR-driven N-S pitching.

### Phase 4: Siting Algorithm & Code Housekeeping

**Goal:** Finalize performance bottlenecks and placement quirks.

- **Module:** `layout/substation_placement.py` & `layout/routing.py`
- **Changes:**
  - Anchor the BOP zone securely on the initial legal boundary point rather than sliding it indiscriminately.
  - Integrate `scipy.spatial.cKDTree` in `routing.py` to eradicate any remaining O(n²) bottlenecks during node snapping.
  - Verify all MV cables strictly follow the `NetworkX` traces mapped in Phase 2 for exact homerun lengths.
