# Project Handoff: PV Layout Engine Upgrade

## Context

We are systematically upgrading the **PV Layout Engine codebase** to align with professional utility-scale solar engineering practices (like PVcase/Helioscope). The goal is to transform the system into a reliable early-stage solar feasibility and conceptual layout generator.

We have successfully completed the latest **Terrain Analysis & Hydrology Upgrade**, fully implementing all phases of the associated implementation plan.

## Progress So Far

1. **Buildable Terrain Detection & Thresholds - COMPLETE**
   - Configured slope thresholds strictly in degrees.
   - Tightened commercial thresholds: Topographic Position Index (TPI) to `-2.0m` and Terrain Ruggedness Index (TRI) to `1.5m`.
   - Segregated OpenStreetMap power line buffers based on voltage: standard lines (30m) and HV lines >66kV (50m).

2. **Advanced Terrain Analytics (Slope Metrics) - COMPLETE**
   - Purged redundant TPI calculation logic.
   - Implemented algorithmic `classify_slope_direction` checks against panel row azimuths (e.g., 180° South).
   - Pipeline now reports precise **Across-Row Slope Area** and **Along-Row Slope Area** percentages to inform grading volume limits and tracker viability.

3. **Hydrology & Flood Risk (PySheds) - COMPLETE**
   - Integrated `pysheds` deeply for accurate hydrological analysis.
   - Implemented native D8 Flow Direction and Accumulation mapping natively against the raster.
   - Automatically generates a `terrain_streams_d8` exclusion constraint based on catchments (>500 upslope cells) and applies a 30m buffering corridor.
   - All generated exclusion geometries are now accurately clipped to the project boundary before reporting, preventing inflated exclusion metrics.

## Cut / Fill & Hydrology Engineering Context

- **Industry Standard Hydrology:** We evaluate natural drainage channels using PySheds to prevent placing arrays in concentrated stormwater paths.
- **Current Strategy (Conservative Layout):** To provide a realistic, lower-risk EPC design, the aggressive Topographic Position Index (TPI) threshold has been reduced from `-3.5m` to `-2.0m`.
- Coupled with the new **30m D8 stream buffer exclusions**, the layout generator rigorously avoids altering natural hydrologic systems, thereby minimizing civil earthworks CapEx, box culverts, and environmental permitting roadblocks.

## Immediate Next Steps

The Terrain Analysis and Hydrological upgrades are successfully verified via a 100 MW Myanmar test site run.
Please refer to the `outputs/engineering_report.md` for verifiable metrics.

Future work can proceed towards advanced grading computations, automated 3D earthworks modeling, or deeper CapEx/OpEx cost-estimation pipelines depending on project priorities.
