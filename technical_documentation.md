# Technical Documentation: PV Layout Pipeline Architecture

This document describes the functional pipeline methodology and algorithms driving the PV Layout Engine as implemented in the codebase.

## 1. System Overview and Intent

The PV Layout Engine is an automated spatial processing pipeline designed for the **early-stage conceptual design and feasibility analysis** of utility-scale solar PV fields. The tool transforms a site boundary and target capacity into a deterministic, terrain-aware engineering layout.

> **Disclaimer**: The methodologies and algorithms implemented herein provide conceptual boundaries, lengths, and estimates. The outputs must not replace human-supervised civil, mechanical, or electrical engineering.

## 2. Global Pipeline Architecture (8-Phase)

The pipeline (`main_pipeline.py`) orchestrates the following sequential phases:

### Phase 1: Data Acquisition

- **DEM:** OpenTopography COP30 (30m) reprojected to UTM and cubic-resampled to 10m.
- **LULC:** ESA WorldCover (10m) for automated habitat and urban exclusions.
- **Vectors:** OSM infrastructure extraction (roads, powerlines, waterways).

### Phase 2: Terrain Analysis

- **Slope & Aspect:** Computed using **Horn's Method** for robust gradient estimation.
- **Roughness:** Terrain Ruggedness Index (TRI) for identifying non-buildable rocky/sharp features.
- **Hydrology:** **D8 Flow Accumulation** and **Topographic Wetness Index (TWI)** via PySheds to detect seasonal drainage channels.
- **Suitability:** Weighted sum of slope and aspect scores (Configurable thresholds: Flat, Gentle, Moderate, Steep).

### Phase 3: Constraints & Buildable Area

- Aggregates OSM buffers, LULC exclusions, and High-Slope (>15°) masks.
- Produces the **Buildable Area Geometry** using GeoPandas spatial joins and boolean operations.

### Phase 4: Capacity Feasibility

- Performs a first-pass area/MW calculation. If the requested target exceeds site potential, the engine triggers a "Capacity Warning" and scales the target to the site's geometric limit (~2.5 hectares/MWdc benchmark).

### Phase 5: BOP Zone Reservation

- **Multi-Criteria Siting:** Evaluates a scoring grid for the Substation/BESS/O&M compound. Weights: **Flatness (0.3), POI Proximity (0.2), Buildable Coverage (0.2), Road Access (0.15), Water Avoidance (0.15)**.
- **Compound Orientation:** Uses local aspect to orient the compound footprint along contour lines, minimizing grading requirements.
- **Buildable Subtraction:** The reserved BOP zone is boolean-subtracted from the buildable area prior to layout.

### Phase 5.5: Infrastructure Corridor Planning

- **Road-First Strategy:** Reserves primary (spine) and secondary (branch) road corridors _before_ placing panels.
- **A\* Pathfinding:** Uses an A\* search on a cost-grid that penalizes steep gradients (>5%) and avoids exclusion zones to find the optimal spine route from the POI/Substation.

### Phase 6: Layout Generation (Tessellation & Clustering)

- **Table Generation:** Populates buildable area with PV tables (strings) based on pitch/GCR/Azimuth rules.
- **BFS Region-Growing:** Rows are clustered into ~3.2 MWac Power Blocks using a Breadth-First Search adjacency algorithm to maximize block contiguity.
- **Exact Truncation:** Terminates row/block production the moment the target MW capacity is satisfied, prioritizing high-suitability terrain.

### Phase 7: Equipment Placement & Routing

- **Transformer Siting:** Places PCUs at the geometric centroid of each Power Block.
- **Road Network Graph:** Converts corridors into a **NetworkX** graph for routing.
- **MV Cable Routing:** Routes 33kV cables along the physical road network using a **Daisy-Chain** topology.
- **Feeder Grouping:** Uses **K-means clustering** to group block transformers into balanced electrical feeders.
- **ECG Feedback (R3):** If the Electrical Centre of Gravity (ECG) shifts significantly from the initial BOP, the pipeline triggers a re-siting logic to optimize cable run lengths.

### Phase 8: Exports & Reporting

- **Yield Engine:** Uses **NREL PySAM** (System Advisor Model) for high-fidelity simulation including bifacial and tracking losses.
- **GIS Exports:** Unified GeoPackage containing layered geometries (Blocks, Roads, Cables, Compounds).
- **Technical Report:** Markdown synthesis of financials (CAPEX), quantities, and risk metrics.

## 3. Key Algorithms

| Feature                 | Algorithm                   | Purpose                                       |
| :---------------------- | :-------------------------- | :-------------------------------------------- |
| **Terrain Derivatives** | Horn's Method               | Gradient (Slope/Aspect) calculation.          |
| **Drainage Detection**  | D8 Flow Accumulation        | Identifying ravines/streams via raster flow.  |
| **Site Selection**      | Weighted Linear Combination | Multi-criteria scoring for Substation.        |
| **Road Pathfinding**    | A\* Search                  | Finding lowest-cost paths on terrain grids.   |
| **Block Clustering**    | BFS Region-Growing          | Grouping tables into contiguous power blocks. |
| **Electrical Feeders**  | K-means + NetworkX          | Feeder grouping and daisy-chain routing.      |
| **Yield Modeling**      | PySAM (SDK)                 | Bankable hourly energy simulation.            |

## 4. Configuration (`config.yaml`)

- `project`: Defines site metadata and POI coordinates.
- `terrain`: Slope suitability thresholds (e.g., `max_slope: 15`).
- `bop_siting`: Weights for the multi-criteria selection grid.
- `roads`: Physical dimensions and A\* gradient penalties.
- `solar`: Equipment specs (Module Wp, String length, Inverter MW).
- `economics`: Unit costs for Blended CAPEX calculation ($/Wdc).

## 5. Known Limitations

- **Fixed Topology:** Currently only supports radial daisy-chain MV routing (no ring main support).
- **Static Pricing:** CAPEX is based on scalar multipliers; doesn't account for real-time supply chain volatility.
- **Geometric Simplification:** Assumes all PV tables are rectangular; does not handle irregular corner truncation (uses whole table exclusion).
