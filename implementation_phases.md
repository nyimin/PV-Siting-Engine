# PV Layout Engine — Phased Fix Implementation Plan

Based on findings in [CODEBASE_AUDIT.md](file:///d:/Triune/Stack%20Space%20-%20Documents/Code/PVLayoutEngine/CODEBASE_AUDIT.md).

---

## Phase 1 — Buildable Terrain Detection ⚠️ Highest Priority

**Stop after this phase for manual GIS validation before proceeding.**

### Files Changed

#### [MODIFY] [config.yaml](file:///d:/Triune/Stack%20Space%20-%20Documents/Code/PVLayoutEngine/config/config.yaml)

- Add `site_boundary_m: 10` under `buffers` (fixes BD-03 — silent 15 m default)
- Add `forest_buffer_m: 50` under `buffers`
- Add `max_north_facing_slope_pct: 5` under `terrain` (controls aspect exclusion)
- Remove dead key `excluded_slope_percent` (BD-02)

#### [MODIFY] [terrain_analysis.py](file:///d:/Triune/Stack%20Space%20-%20Documents/Code/PVLayoutEngine/terrain/terrain_analysis.py)

- Fix aspect penalty formula in `calculate_suitability()` — use correct angular distance to North (fixes TA-02):
  ```python
  angle_to_north = np.abs(((aspect + 180) % 360) - 180)
  penalty = np.where(angle_to_north <= 45, (1 - angle_to_north / 45.0) * 60.0, 0.0)
  ```
- Add east/west slope penalty: slope > 5% facing 60°–120° or 240°–300° → score × 0.7 (TA-01)

#### [MODIFY] [constraint_combiner.py](file:///d:/Triune/Stack%20Space%20-%20Documents/Code/PVLayoutEngine/constraints/constraint_combiner.py)

- Add **aspect hard exclusion** for northern hemisphere: `aspect ∈ [315°, 45°]` AND `slope > max_north_facing_slope_pct` → excluded polygon (fixes BD-01 — the most critical P1 issue)
- Add **curvature hard exclusion** via `raster_to_polygons(curvature_path, 0.4, ...)` (BD-05)
- Add **forest edge buffer**: buffer `lulc_Tree cover` exclusion polygons by `forest_buffer_m` (BD-06)
- Lower default TRI threshold comment to 1.5 m recommendation (BD-07)
- Read `site_boundary_m` from config (BD-03)

#### [MODIFY] [map_generator.py](file:///d:/Triune/Stack%20Space%20-%20Documents/Code/PVLayoutEngine/visualization/map_generator.py)

- Add `terrain_aspect_map.png` plot in `create_terrain_maps()` (RP-01)
- Add `terrain_ruggedness_map.png` (TRI) plot (RP-01)
- Rename outputs: `slope_map.png` → `terrain_slope_map.png`, `constraint_map.png` → `terrain_constraints_map.png` (RP-01)
- Save all GeoJSON in WGS84 (`to_crs(epsg=4326)` before saving) (RP-04)

#### [MODIFY] [main_pipeline.py](file:///d:/Triune/Stack%20Space%20-%20Documents/Code/PVLayoutEngine/main_pipeline.py)

- Pass `exclusions_gdf` into `create_terrain_maps()` call (RP-02)
- Fix image filename in report: `constraint_map.png` → `terrain_constraints_map.png` (RP-03)

### Phase 1 Required Outputs

| File                                     | Description                        |
| ---------------------------------------- | ---------------------------------- |
| `outputs/geojson/buildable_area.geojson` | Buildable polygon (WGS84)          |
| `outputs/terrain_slope_map.png`          | Slope % with site boundary         |
| `outputs/terrain_aspect_map.png`         | Aspect with N-facing penalty zones |
| `outputs/terrain_constraints_map.png`    | All exclusion zones by type        |
| `outputs/terrain_ruggedness_map.png`     | TRI map                            |

### Manual Validation Gate

- [ ] North-facing slopes are visually absent from buildable area
- [ ] Steep terrain (>10%) excluded — check in QGIS against slope map
- [ ] All 5 output files exist and render correctly
- [ ] Buildable percentage is 40–75% of site area (sanity check)

---

## Phase 2 — Terrain Suitability Scoring

**Fixes TA-01, TA-02, TA-03, TA-04.**

### Files Changed

#### [MODIFY] [terrain_analysis.py](file:///d:/Triune/Stack%20Space%20-%20Documents/Code/PVLayoutEngine/terrain/terrain_analysis.py)

- Fix `calculate_curvature()` border artefacts: replace `np.roll` with `scipy.ndimage.convolve` using `mode='nearest'` (TA-03) — already used for slope/aspect, just align curvature to same pattern
- Extract latitude from UTM DEM centroid (reproject to WGS84) rather than re-reading the raw DEM file (TA-04)

#### [MODIFY] [map_generator.py](file:///d:/Triune/Stack%20Space%20-%20Documents/Code/PVLayoutEngine/visualization/map_generator.py)

- Rename output from `suitability_map.png` → `terrain_suitability_map.png`
- Ensure suitability raster is also saved as `terrain_suitability.tif` (currently saved as `suitability.tif`)

### Phase 2 Required Outputs

| File                                     | Description                         |
| ---------------------------------------- | ----------------------------------- |
| `data/processed/terrain/suitability.tif` | Renamed → `terrain_suitability.tif` |
| `outputs/terrain_suitability_map.png`    | Suitability score map (0–100)       |

### Manual Validation Gate

- [ ] South/flat/open terrain scores >70; north-facing steep terrain scores <20
- [ ] Suitability map is visually coherent — high scores align with buildable area from Phase 1

---

## Phase 1 & 2 Addendum — DEM Accuracy Improvements

**"Creative" accuracy enhancements for coarse 30m DEMs**

### Files Changed

#### [MODIFY] [config.yaml](file:///d:/Triune/Stack%20Space%20-%20Documents/Code/PVLayoutEngine/config/config.yaml)

- Added `resample_resolution_m: 10.0` to enforce sub-grid interpolation
- Added `gaussian_smooth_sigma: 1.0` to remove high-frequency radar canopy noise

#### [MODIFY] [terrain_analysis.py](file:///d:/Triune/Stack%20Space%20-%20Documents/Code/PVLayoutEngine/terrain/terrain_analysis.py)

- Modified `reproject_dem_to_utm()` to optionally accept a target resolution and apply `Resampling.cubic`, interpolating the 30m grid into a smooth 10m surface
- Added `scipy.ndimage.gaussian_filter()` to `process_terrain()` applied immediately after projection.
- **Result:** Drastically smoothed derivatives (Slope, Aspect, TRI), reducing false-positive TRI hard-exclusions from **~40% to just 5.0%** of the raster at the same 2.5m threshold.

---

## Phase 3 — PV Layout Generation

**Fixes LG-01, LG-02, LG-03, LG-04, LG-05, LG-06.**

### Files Changed

#### [MODIFY] [block_generator.py](file:///d:/Triune/Stack%20Space%20-%20Documents/Code/PVLayoutEngine/layout/block_generator.py)

- **Rewrite `_compute_block_dimensions()`** to derive pitch from GCR first (LG-01):
  ```python
  # Primary: GCR drives pitch
  gcr = config["solar"]["gcr"]  # e.g. 0.38
  module_depth = table_height_m  # tilted module depth along slope
  pitch = module_depth / gcr
  # Secondary: verify against no-shade constraint for warning only
  ```
- Fix row orientation (LG-02): ensure E-W rows (running along X-axis) step in N-S direction for fixed-tilt northern hemisphere. Grid columns = N-S (step in Y), rows within each column = E-W (step in X). Clarify variable naming.
- Compute `strings_per_row` from module geometry instead of hardcoding 4 (LG-05)
- Read slope threshold from config in `_check_row_terrain()` not hardcoded (LG-04)
- Compute block DC capacity from `strings × modules × module_power_w` (LG-03)
- Raise block acceptance threshold from 40% to 60% utilisation (LG-06)

### Manual Validation Gate

- [ ] Row footprints in QGIS are oriented E-W for northern hemisphere sites
- [ ] Row-to-row spacing matches `pitch = module_depth / GCR` (check with ruler tool)
- [ ] No rows overlap BOP zone or exclusion areas
- [ ] Total DC capacity is within ±5% of `total_strings × modules_per_string × module_power_w`

---

## Phase 4 — BOP Infrastructure

**Fixes BOP-01, BOP-02, BOP-03, BOP-04.**

### Files Changed

#### [MODIFY] [substation_placement.py](file:///d:/Triune/Stack%20Space%20-%20Documents/Code/PVLayoutEngine/layout/substation_placement.py)

- Fix GeoPandas geometry assignment in the inward-shift loop: use `gdf = gdf.set_geometry(gdf.geometry.translate(...))` (BOP-03)
- Move guard house to the boundary-facing edge of the O&M compound, not the centre (BOP-02)
- Add HV power line proximity to substation scoring criteria using existing `osm_power` exclusion layer (BOP-01)
- Scale BESS compound footprint from `capacity_mwh` and container dimensions (BOP-04)

### Manual Validation Gate

- [ ] All BOP compounds lie entirely inside site boundary
- [ ] Substation compound is closest BOP element to the site perimeter access point
- [ ] Guard house is at the gate/entrance of the O&M compound

---

## Phase 5 — Road Network

**Fixes RN-01, RN-02, RN-03, RN-04.**

### Files Changed

#### [MODIFY] [routing.py](file:///d:/Triune/Stack%20Space%20-%20Documents/Code/PVLayoutEngine/layout/routing.py)

- **Rewrite `route_access_roads()`** with a spine + branch hierarchy:
  1. **Main spine road**: single road connecting substation to the centroid of the block cluster, running approximately N-S through the centre of the site
  2. **Branch roads**: perpendicular roads connecting the spine to each block centroid
  3. Road type attribute: `"main_spine"` or `"branch_service"`
- Build routing corridor from **gaps between blocks** (buffer blocks by 3 m, subtract from convex hull, this is the road space) rather than over block interiors (RN-01)
- Remove the `dissolve()` that destroys road hierarchy (RN-02)
- Replace `_snap_point_to_grid()` O(n) loop with `scipy.spatial.cKDTree` (RN-04)
- Add `grid_resolution_m` to `config.yaml` under `roads` (RN-03)

### Manual Validation Gate

- [ ] Road network shows one clear spine road through site
- [ ] Branch roads connect perpendicularly to spine, not diagonal tangles
- [ ] No roads pass through PV row footprints
- [ ] Road hierarchy (`road_type` attribute) is preserved in GeoJSON output

---

## Phase 6 — Electrical Collection System

**Fixes ER-01, ER-02, ER-03.**

### Files Changed

#### [MODIFY] [routing.py](file:///d:/Triune/Stack%20Space%20-%20Documents/Code/PVLayoutEngine/layout/routing.py)

- **Rewrite `route_mv_cables()`**: route cables along existing road geometry (snap transformer to nearest road point, follow road network to substation) rather than straight lines (ER-01)
- Compute cable length as road-following path length, not Euclidean distance

#### [MODIFY] [bop_placement.py](file:///d:/Triune/Stack%20Space%20-%20Documents/Code/PVLayoutEngine/layout/bop_placement.py)

- Route LV cables along row access aisles (N or S end of each row footprint) to the block transformer, not straight through panels (ER-03)

### Manual Validation Gate

- [ ] MV cable GeoJSON follows road corridors visually in QGIS — no cables cross panel rows
- [ ] MV cable total length in report is plausibly longer than direct-line distances
- [ ] LV cables run along row edges, not through panel interiors

---

## Phase 7 — Engineering Metrics & Reporting

**Fixes EC-01, EC-02, EC-03, EC-05, EC-06, RP-03, RP-04, RP-05.**

### Files Changed

#### [MODIFY] [metrics.py](file:///d:/Triune/Stack%20Space%20-%20Documents/Code/PVLayoutEngine/analysis/metrics.py)

- Add fill volume to earthworks estimate; report cut and fill separately (EC-01)
- Compute true GCR from module geometry: `(module_length × n_mods_per_cross_section) / row_pitch` (EC-02)
- Fix PVWatts azimuth for southern hemisphere sites (EC-03)
- Add fallback yield warning to report when PVWatts API key is absent (EC-05)
- Remove duplicate import/logger block at top of file (EC-06)

### Manual Validation Gate

- [ ] Engineering report shows separate Cut Volume and Fill Volume
- [ ] GCR in report matches the configured GCR (e.g., 0.38 ± 0.02)
- [ ] No broken image links in engineering report markdown
- [ ] Report renders correctly in a markdown viewer

---

## Post-Audit Phase — Advanced Layout Optimisation (Execution Phase 6)

**Fixes fragmentation, fills gaps, and adds economic scoring.**

### Files Changed

#### [MODIFY] [block_generator.py](file:///d:/Triune/Stack%20Space%20-%20Documents/Code/PVLayoutEngine/layout/block_generator.py)

- **Variable block sizing via Strip Generation**: Instead of placing rigid M×N grid cells and rejecting them if they overlay boundaries, the generator now chunks full-column strips, aggregating rows N-S until `target_strings_per_block` is met.
- **Oblique Tessellation Logic**: Framework added for rotating grid to site's principal axis, but explicitly disabled for fixed-tilt systems to enforce strict E-W orientation.

#### [MODIFY] [metrics.py](file:///d:/Triune/Stack%20Space%20-%20Documents/Code/PVLayoutEngine/analysis/metrics.py)

- **CAPEX Economic Scoring**: Combines unit rates from `config.yaml` (`pv_module_usd_per_watt`, `inverter_usd_per_watt`, `mv_cable_usd_per_m`, `road_usd_per_m2`, `earthworks_usd_per_m3`) to calculate a blended total CAPEX and Specific CAPEX ($/Wdc).

### Manual Validation Gate

- [ ] Blocks fill boundary gaps dynamically, creating irregularly sized but densely packed clusters.
- [ ] Row footprints remain strictly East-West for fixed-tilt arrays.
- [ ] Engineering report contains a fully populated "Economic Analysis (CAPEX)" section.

---

## Sequencing Summary

```
Phase 1 → [Manual Review] → Phase 2 → [Manual Review]
       → Phase 3 → [Manual Review] → Phase 4 → Phase 5
       → [Manual Review] → Phase 6 → [Manual Review]
       → Phase 7 → [Final Review]
```

> **Rule:** Never modify code from multiple phases simultaneously. Each phase must produce working outputs validated before the next begins.
