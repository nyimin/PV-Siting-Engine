# PV Layout Engine — Project Handoff

> **Date:** 2026-03-06  
> **Test Site:** 100 MW Myanmar Greenfield  
> **Pipeline entry:** `python main_pipeline.py outputs/geojson/site_boundary.geojson 100`  
> **Last successful run:** exit code 0 ✓

---

## Overall Architecture

```
main_pipeline.py
 ├─ data/         OSM + DEM fetch
 ├─ terrain/      Slope, TPI, TRI, D8 hydrology (PySheds)
 ├─ layout/
 │    ├─ block_generator.py     PV row/block tessellation
 │    ├─ bop_placement.py       Inverter + PCU Border Pad placement
 │    └─ routing.py             ← Active development
 └─ visualization/map_generator.py
```

---

## Completed Phases ✅

### Phase 1 — Terrain Analysis & Buildable Area

- Slope thresholds in degrees (not %, not ratio) — configurable in `config.yaml`.
- TPI `< -2.0 m` and TRI `> 1.5 m` excl. constraints excluding valleys/rough terrain.
- **PySheds D8** flow direction + accumulation for stream channel detection; 30 m stream buffer exclusion.
- Across-row slope (3.1%) and along-row slope (13.2%) percentages reported.
- DEM risk flagged when resolution > 20 m.

### Phase 2 — Substation & Spine Road Placement

- Substation sited inside buildable area using a multi-criteria gravity model (slope + centroid proximity + site access).
- Medial axis spine road generated via `momepy.Skeleton` over the filled buildable area polygon.
- Fallback spine: straight line from substation to site top-centre if momepy fails.

### Phase 2.2 — Block Generation & PCU Border Pad Placement

- **Block spec:** 3.2 MWac per block, 10 × SG320HX (320 kW) string inverters + 1 × 3.15 MVA block transformer (standard A configuration).
- **Total deployed:** 46 blocks, 891 PV rows, 105 string inverters, 46 block transformers.
- **Module:** 635 W bifacial (2P portrait fixed-tilt, 12.55 m pitch).
- PCU pad snapped to **nearest block boundary facing the substation** (Border Pad) — not the block centroid.
- Exactly 10 inverters clustered around each transformer at 1.5 m spacing.

### Phase 3 — MV Cables & Access Roads (routing.py)

- Branch roads connect from PCU Border Pad → nearest spine point.
- MV cables (33 kV) routed from each transformer → substation.
- 46 transformers grouped into radial feeder circuits (≤ 8 blocks/feeder).

---

## Phase 3.2 — Obstacle-Aware Routing ✅

### Problem identified by user (session ending)

Roads, LV, and MV cables were still being drawn as **straight lines that pass directly through PV blocks**. This is geometrically and engineering-wise unacceptable.

### Fix implemented

`layout/routing.py` was **fully rewritten** with a **coarse occupancy-grid + A\* pathfinder**:

| Component         | Approach                                                                                       |
| ----------------- | ---------------------------------------------------------------------------------------------- |
| Obstacle map      | 15 m resolution raster grid; block polygons painted as occupied cells (with 3 m inward buffer) |
| Pathfinding       | 8-connected A\* (cardinal + diagonal moves); obstacle cells are completely avoided             |
| Branch roads      | PCU pad → nearest spine point, A\*-routed around other blocks                                  |
| MV cables (33 kV) | Transformer → substation, A\*-routed around all blocks                                         |
| Fallback          | Straight line if A\* finds no path (logged as WARNING)                                         |

- The spine road was correctly clipped to the `buildable_area_gdf`.

**Pipeline ran successfully** with the grid A\* routing active, correctly avoiding PV blocks.

---

## Phase 4 — Earthworks Estimation & Yield Reporting ✅

| Task                 | File                    | Description                                                                                                 |
| -------------------- | ----------------------- | ----------------------------------------------------------------------------------------------------------- |
| Real cut/fill volume | `terrain/earthworks.py` | Integrated DEM elevation under each block polygon; fitted a best-fit 3D plane; calculated cut/fill volumes. |
| Grading cost CapEx   | `terrain/earthworks.py` | Applied USD/m³ rates from `config.yaml`.                                                                    |
| Actual yield model   | `layout/yield_model.py` | Integrated NREL PVWatts V8 API to compute P50 and P90 annual MWh yields.                                    |
| Report section       | `analysis/metrics.py`   | Added earthworks table, grading CapEx, and P50/P90 MWh to the final Markdown `engineering_report.md`.       |

_Note on Earthworks: Due to the large block size (3.2 MW) and hilly terrain, strict grading tolerances (>1.5m cut) caused many blocks to be flagged as "High Topo Rejected Area". Adjust thresholds in `config.yaml` or use smaller block configurations to improve grading acceptance on rugged sites._

---

## Current Output Files

| File                            | Description                                  |
| ------------------------------- | -------------------------------------------- |
| `outputs/layout_map.png`        | Static layout map (roads, cables, blocks)    |
| `outputs/layout_map.html`       | Interactive Folium map (toggle layers)       |
| `outputs/engineering_report.md` | Capacity, terrain, yield, earthworks summary |
| `outputs/geojson/*.geojson`     | All spatial features as GeoJSON              |

---

## Immediate Next Session Checklist

- [ ] **Evaluate / implement LV grouping and routing:** LV DC cables (strings to inverters) and LV AC cables (inverters to transformers) currently use simple placeholders.
- [ ] **Refine constraint logic:** Explore tighter integration with PVcase-like terrain-following tracking rules if desired.
- [ ] **Cost modeling (Phase 5):** Expand the financial model to estimate overall CapEx beyond just grading costs (e.g., PV modules, civil works, structures).
