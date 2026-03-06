# Project Handoff: PV Layout Engine Upgrade

## Context

We are systematically upgrading the **PV Layout Engine codebase** to align with professional utility-scale solar engineering practices (like PVcase/Helioscope). The goal is to transform the system into a reliable early-stage solar feasibility and conceptual layout generator.

We have completed **Phase 1** and **Phase 2** of the implementation plan (now saved locally as `implementation_phases.md`).

## Progress So Far

1. **Buildable Terrain Detection (Phase 1) - COMPLETE**
   - Configured slope thresholds strictly in degrees.
   - Added `north_facing_exclusion` for slopes > 5° facing 315°–45° (Northern Hemisphere).
   - Added forest edge buffers (50m inward).
   - Addressed false-positive roughness exclusions by implementing:
     - **Sub-grid Cubic Resampling** (30m -> 10m DEM)
     - **Gaussian Filtering** (`sigma=1.0`) on the DEM to remove high-frequency radar canopy noise.

2. **Hydrology & Waterways - COMPLETE**
   - Added a **Topographic Position Index (TPI)** algorithm to the `terrain_analysis.py` module.
   - Using a ~150m radius, the engine computes local elevation means. Regions deeper than `max_tpi_valley_m` (default -2.0m) are classified as channels/valleys.
   - This automatically excludes 256ha of ravines from the Myanmar DEM without relying on spotty OSM vector data.
   - OSM and LULC waterways are _also_ buffered by 50m as a secondary fail-safe.

3. **Terrain Suitability Scoring (Phase 2) - COMPLETE**
   - Successfully generated 0-3 suitability indices based on weighted Slope (60%) and Aspect (40%).

## Cut / Fill & Hydrology Engineering Context

_Note for the user regarding waterway crossings and cut/fill thresholds:_
In utility-scale solar, shallow rills (e.g. < 0.5m deep) are routinely graded flat ("cut and fill"). However, deeper valleys carry concentrated stormwater during monsoon events.

- **Industry Standard:** We typically do _not_ cut/fill established drainage channels, because altering natural hydrologic flow requires massive civil earthworks (culverts, concrete lining) and triggers severe environmental/permitting delays.
- **Current Strategy (Aggressive / High CapEx):** To maximize buildable area and MW capacity on this site, we are using an aggressive Topographic Position Index (TPI) threshold of **`max_tpi_valley_m: -3.5`**.
- This means the engine will only avoid unmistakably deep ravines and will route solar blocks directly across shallower valleys (up to -3.5m deep).
- **Warning:** Grading across these -3.5m valleys violates natural hydrology, requires massive civil infrastructure mapping (box culverts/trenching), and will create severe environmental and permitting roadblocks. The CapEx for civil earthworks will be significantly higher than a balanced design, but it achieves the "optimum area" requested.

## Immediate Next Steps for the New Context

You must pick up work at **Phase 3 — PV Layout Generation**.

Read `CODEBASE_AUDIT.md` and `implementation_phases.md` in the root directory to see the exact files and fixes required for Phase 3.

The primary goals for Phase 3 (fixing `layout/block_generator.py`) are:

1. Derive row pitch directly from GCR (`module_depth / gcr`).
2. Fix row orientation so rows always run E-W for northern hemisphere fixed-tilt sites.
3. Compute strings-per-row dynamically from module dimensions.

Please read `implementation_phases.md` before proceeding.
