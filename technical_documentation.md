# Technical Documentation: PV Layout Pipeline Architecture

This document describes the active, functional pipeline methodology and algorithms driving the PV Layout Engine as implemented in the codebase.

## 1. System Overview and Intent

The PV Layout Engine is an automated spatial processing pipeline designed for the **early-stage conceptual design and feasibility analysis** of utility-scale solar PV fields. The tool takes a rough, user-provided bounding box polygon along with a requested MW capacity and executes a deterministic multi-stage spatial optimization workflow.

> **Disclaimer**: The methodologies and algorithms implemented herein provide conceptual boundaries, lengths, and estimates. The outputs must not replace human-supervised civil, mechanical, or electrical engineering.

## 2. Global Pipeline Architecture (8-Phase)

The pipeline (`main_pipeline.py`) systematically implements the layout constraint creation following standard utility workflows (PVcase/Helioscope methodology):

### Phase 1: Data Acquisition

Dynamically fetches critical data sets covering the bounding box dynamically derived from bounding centroid metadata:

- **DEM:** OpenTopography COP30 (30m) downscaled to 10m via raster resampling.
- **LULC:** ESA WorldCover (10m)
- **Vectors:** OpenStreetMap queries (roads, wetlands, existing infra).

### Phase 2 & 3: Terrain Analysis & Constraints

Exclusion polygons are derived before any panel placement is initiated.

1. Computes Horn's Method Slope, Aspect, and Zevenbergen-Thorne Curvature.
2. Combines buffered OSM infrastructure, ESA Land Cover exclusions, configurable steep slope restrictions, and Hydrological avoidance networks (via PySheds D8 flow models) into a unified **Buildable Area Geometry**.

### Phase 4: Capacity Feasibility

The engine computes a rapid sanity check against the available remaining buildable acreage.

- Based on `calculate_feasible_capacity`, it evaluates if the user-requested MW capacity can physically fit within the buildable polygon limits.
- If it cannot, the script will warn the user that the site is fully constrained or too small and emit reduced Target Installation caps.

### Phase 5: BOP Zone Reservation

Places primary compounds (Substation, BESS, O&M) utilizing an intelligent scoring grid inside the buildable boundaries:

- Scores 80m candidate interior and boundary placement points using: **Terrain Flatness (0.30), POI Proximity (0.20), Buildable Surface Coverage (0.20), Proximity to Roads (0.15), Distance from Water (0.15)**.
- Extrudes exact dimensional footprint geometries perpendicular to local topological descent gradients.

### Phase 5.5 & 6: Corridor Planning & Block Generation

1. **Corridors**: An A\* pathfinding crawler searches a 15m occupancy grid and generates the primary backbone and secondary branch roadways. Spaces required for these geometries are carved out (boolean difference) from the buildable area.
2. **Block Generation**: The core generator (`generate_solar_blocks`) drops contiguous standard inverter units (~3.2 MWac default):
   - Tessellates the area uniformly into blocks using an optimized grid displacement mechanic.
   - Evaluates strings inside structural block segments against slope tolerances.
   - Truncates block production precisely when the requested Target MW capacity is satisfied.

### Phase 7: Balance of Plant Equipment & Routing Integration

1. **Routing**: `route_mv_cables_and_roads` converts previously generated corridors into NetworkX pathing graphs.
2. **Daisy-chaining**: Transformer nodes within blocks are topologically snapped to the road infrastructure and "homerun" radial daisy-chained back to the substation using shortest-path geometric distance computations.
3. **ECG Feedback R3**: If the capacity-weighted geometric center of all transformers diverges >300m away from the initially placed BOP footprint, the pipeline proactively overrides the BOP placement configuration prioritizing POI constraints heavier and re-fires generation optimizations from phase 5.

### Phase 8: Financials, Yields, and Exports

- **Yield**: A Bankable PySAM generation module uses high-fidelity physical coordinates, panel tilt profiles, and dynamically retrieved NSRDB PSM3 weather files to compute P50 array outputs.
- Tabulates exact quantities of required string inverters, cable tonnages, row mounting constraints, and area geometries and runs simple scalar CAPEX multipliers to evaluate a Blended and Specific installation cost vector ($/Wdc).

## 3. Notable Algorithm Clarifications

- **Block Aggregation doesn't use K-Means**: During iteration phases, `generate_solar_blocks` now utilizes a unified global grouping strategy. Adjacent valid strings and blocks are clustered utilizing localized region expansion logic to optimize contiguity instead of strictly mathematical clustering variants.
- **Tertiary Aisles vs "Access Canyons"**: Configuration parameters currently default `tertiary_aisles_enabled: false`. If standard gaps between block modules are needed, explicit tertiary aisle geometries can optionally be instantiated, carving physical space through block bounds.

## 4. Known Technical Limitations vs. Future Improvements

**Currently Unimplemented Functions ("Planned Status"):**

- **Multi-Objective Global Optimization**: The current pipeline runs deterministically, seeking out the first geometrically valid path resulting in success. Extending layout placements via Genetic Algorithms or Simulated Annealing to weigh Earthworks vs Yield tradeoff scenarios is currently on the roadmap.
- **Export Formats (DWG/DXF)**: Exclusively outputs Geopackages, GeoJSON, and ESRI Shapefiles. An integration layer for direct civil CAD system parsing is unreleased.
- **Interactive UI Generation**: Modifying the configuration parameters mid-run or generating real-time error evaluations requires manually killing the run pipeline or launching `patch_block_gen.py`.

## 5. User Input and Configuration (`config.yaml`)

The conceptual pipeline is completely deterministic based on two primary inputs:

1. **The Execution Command Interface**: Defines the spatial bounds and total capacity.
2. **The `config.yaml` Ruleset**: Defines the physical equipment and spatial heuristics.

### The Execution Command

```bash
python main_pipeline.py <site_boundary_vector> <target_capacity_mw> --config <path_to_config>
```

- **`site_boundary_vector`**: Must be a valid, enclosed geospatial polygon (GeoPackage, GeoJSON, Shapefile). The engine will automatically detect its location and project it to the optimal localized UTM coordinate system.
- **`target_capacity_mw`**: A float dictating the target scale of the layout. The pipeline will intelligently truncate block tessellation once this thermal limit is crossed to avoid wasting buildable acreage.

### Core Configuration Modules (`config.yaml`)

The YAML parameters control hundreds of topological thresholds. The key blocks include:

- `project.poi`: An optional `[longitude, latitude]` vector indicating where the Point of Interconnection (POI) exists. The substation siting logic will spatially gravitate towards this coordinate.
- `bop_siting.weights`: Controls the multi-criteria siting variables. Modify these to favor terrain flatness (`terrain_slope`) vs existing access roads (`road_access`).
- `roads`: Defines civil engineering widths (`main_collector_width_m`) and critical cut/fill limits (`max_gradient_pct`). Toggling `tertiary_aisles_enabled` allows carving explicit maintenance aisles directly through blocks.
- `terrain`: Governs the threshold variables classifying terrain from "Flat" (<3°) down to "Unsuitable" (>15°). Adjusting `max_tpi_valley_m` dictates how aggressively the engine bridges natural drainage swales vs forcing exclusions.
- `solar`: Explicitly sets the physical geometries of the tracking system or fixed tilt tables (module width, tilt degree, spacing pitches) used to calculate table capacity arrays.
- `economics`: A lookup dictionary assigning USD scaling costs per Watt or Meter for civil/electrical installations to establish the benchmark Specific CAPEX value.
- `yield.engine`: Swaps the annual P50 calculation module between NREL's `pysam` (advanced localized shading), `pvwatts` (standard lookup API), or a manual offline `proxy`.

## 6. Output Structure and Interpretation

Upon successful execution, the pipeline dumps heavily structured artifacts to the `outputs/` directory.

### `engineering_report.md`

A Markdown-formatted summary containing the core metrics driving the conceptual site:

1. **Site Summary**: The raw geometric exclusion limits separated into Component Type categories (LULC vs OpenStreetMap vs Terrain Steepness).
2. **Terrain Summary**: Computed topological values representing the generated Mean Terrain Ruggedness (TRI) and Suitability percentages driving the block layout.
3. **Capacity Analysis**: Tabular breakdown of the target MW requested by the user versus the layout logic's achieved AC/DC Installation capacities.
4. **Energy Yield & Storage**: Evaluated P50/P90 generation values explicitly stating which yield simulation engine triggered (PySAM vs PVWatts vs Latitude Proxy).
5. **Civil Earthworks & Economics (CAPEX)**: Automatically calculates earthwork geometric cut/fill (m³) and applies scalar unit costs ($/Wdc) defined in the configuration files to generate a "Blended CAPEX" investment cost.
6. **Electrical Collection**: A breakdown table defining every single 33kV MV feeder line generated, identifying its exact cable thickness, voltage drop penalty (%), and length in kilometers.
7. **Risk Assessment**: A traffic-light style categorization (🔴 High, 🟡 Medium, 🟢 Low) automatically triggering across common feasibility parameters (e.g. buildable acreage < 50%, high baseline slope, impossible capacity targets).

### Visual Layout Exports (`layout_map.html` and `.png` models)

The pipeline leverages Matplotlib and Folium to generate two distinct layout views:

- **`layout_map.png`**: High-resolution static rendering visualizing the exact polygons, roadways (black spines), and MV cable routing traces connecting individual block footprints.
- **`interactive_map.html`**: A Leaflet-styled interactive browser map enabling toggling of terrain characteristics vs infrastructure placement (ex: clicking PV blocks queries MW output).

> _Note: GIS Layers (Geopackage/GeoJSON) also contain these explicit topological definitions for deeper import into QGIS/ArcGIS._
