# PV Layout Engine — Codebase Audit

**Date:** 2026-03-06  
**Auditor:** Senior Solar Design Engineer / GIS Specialist / Python Systems Architect  
**Scope:** Full codebase audit against real-world solar engineering practice (PVcase, Helioscope, Aurora Solar)  
**Goal:** Transform the system into a reliable early-stage solar feasibility and conceptual layout generator

---

## Executive Summary

The codebase forms a functional end-to-end pipeline skeleton. Most individual algorithms (`calculate_slope`, `calculate_tri`, `calculate_curvature`, `calculate_aspect`) are mathematically correct. However, several **critical engineering flaws** prevent this system from being used as a reliable solar feasibility tool:

1. The buildable terrain filter is **incomplete** — aspect penalties, TRI and curvature thresholds are not applied as hard exclusion constraints in the constraint combiner.
2. The PV layout grid ignores **GCR** from config — it reverses the correct engineering workflow (GCR → pitch, not pitch → block size).
3. The access road routing uses an **A\* MST on block centroids**, producing a visually messy, non-hierarchical spiderweb. No main spine road, no inter-row maintenance aisles.
4. MV cable routing ignores road geometry entirely — all cables are drawn as **direct straight lines** to the substation regardless of comment text saying otherwise.
5. The terrain suitability score is internally computed but **never written to the required output file** `terrain_suitability.tif` (a file named `suitability.tif` is produced but not the specified output).
6. The Phase 1 required outputs (`buildable_area.geojson`, `terrain_slope_map.png`, `terrain_aspect_map.png`, `terrain_constraints_map.png`, `terrain_ruggedness_map.png`) are **partially missing** from the pipeline outputs.
7. The `config.yaml` has a `max_slope_percent: 10` threshold but also an `excluded_slope_percent: 15` that is **never used anywhere in the code** — only `max_slope_percent` feeds the constraint combiner, creating a silent inconsistency.
8. The `site_boundary_m` setback is unused unless `buffers.site_boundary_m` exists in config, but it is **not in config.yaml** — the setback silently defaults to 15 m.

---

## Issue Catalogue

Issues are **ranked by engineering impact and reliability risk** (P1=Critical, P2=High, P3=Medium, P4=Low).

---

## Category 1 — Terrain Analysis

### TA-01 | P2 | Slope calculation produces `percent` but treats result as `percent` in exclusion (correct), but `calculate_suitability` linearly ramps from `preferred_slope` to `max_slope` without a slope penalty for **east/west-facing steep slopes**

**File:** `terrain/terrain_analysis.py` — `calculate_suitability()` (line 204)  
**Problem:** The aspect penalty only penalises north-facing slopes. Steep east-facing and west-facing slopes (>7%) cause significant inter-row shading and tracker-motor issues on single-axis trackers but receive no penalty.  
**Engineering Impact:** Suitability scores for east/west-facing steep terrain are overestimated. Poor sites receive high scores.

### TA-02 | P2 | `calculate_suitability()` — aspect penalty angle math is incorrect for northern hemisphere

**File:** `terrain/terrain_analysis.py` — line 237  
**Code in question:**

```python
angle_diff = np.where(aspect <= 180, aspect, 360 - aspect)
```

**Problem:** This formula computes the absolute angular difference from 0° (North) for all aspects, not just the penalty region. The penalty mask is `(aspect <= 45) | (aspect >= 315)` (±45° from North), but the `angle_diff` calculation maps aspects of 1–180° to 1–180° (which is wrong — aspect 45° gives `angle_diff=45`, aspect 90° gives `angle_diff=90`, aspect 180° gives `angle_diff=180`). Then `penalty = (1 - angle_diff/45) * 60` would produce **negative penalties** for aspects >45°, which are then clipped to 0. The net result: only aspects exactly at 0° get the full penalty; 45° and beyond get zero penalty. A correct formula would use the angular shortest distance from due North.  
**Engineering Impact:** North-facing slope penalty is severely underestimated. Nearly north-facing terrain gets zero penalty instead of a proportional penalty.

**Correct formula:**

```python
# Angular difference to North: wrap to [-180, 180], take absolute value
angle_to_north = np.abs(((aspect + 180) % 360) - 180)
penalty = np.where(angle_to_north <= 45, (1 - angle_to_north / 45.0) * 60.0, 0)
```

### TA-03 | P3 | `calculate_curvature()` uses `np.roll` for border pixels, which wraps data from the opposite edge

**File:** `terrain/terrain_analysis.py` — lines 144–151  
**Problem:** Using `np.roll` without clipping introduces toroidal boundary artefacts: left-edge curvature values contaminate right-edge calculations. The TRI calculation correctly clips post-roll values, but the curvature function doesn't.  
**Engineering Impact:** Curvature artefacts at raster edges could falsely mark edge-row terrain as unbuildable or buildable. Risk is low for large DEMs but meaningful for small sites where PV rows are near raster edges.

### TA-04 | P3 | `process_terrain()` reads the original WGS84 DEM to extract latitude (line 315) but this could fail if the original DEM path is no longer accessible after caching

**File:** `terrain/terrain_analysis.py` — line 315  
**Problem:** The function opens `dem_path` (original WGS84 DEM) again at line 315 after already reprojecting to UTM. If the cache path changes between runs or the original DEM is cleaned up, this secondary open fails silently (no try/except). Furthermore, the latitude should be derived from the UTM DEM's centre reprojected back to WGS84, not from re-reading the raw file.  
**Engineering Impact:** Low in practice but creates a fragile dependency and a potential silent error if latitude extraction fails.

### TA-05 | P4 | Hillshade uses a fixed azimuth (315° = NW) that is not configurable

**File:** `terrain/terrain_analysis.py` — line 174  
**Problem:** Hillshade is purely for visualisation, but a fixed NW azimuth ignores site-specific sun angles. For sites in the southern hemisphere or at high latitudes, a more geographically appropriate sun position would improve visual interpretation.  
**Engineering Impact:** Negligible for engineering calculations, minor visualisation quality issue.

---

## Category 2 — Buildable Terrain Detection

### BD-01 | **P1 (Critical)** | Aspect exclusion is **not applied as a hard exclusion constraint** in `combine_constraints()`

**File:** `constraints/constraint_combiner.py`  
**Problem:** The `combine_constraints()` function applies slope and TRI exclusions as raster-to-polygon conversions, but **aspect is never used as an exclusion constraint**. The `calculate_suitability()` function applies an aspect penalty to the suitability score, but the buildable area computation is entirely based on `slope > max_slope` and `tri > max_tri`. North-facing steep slopes (which should be excluded) are marked as buildable.  
**Required fix:** Add aspect exclusion: for sites in the northern hemisphere, pixels where `aspect ∈ [315°, 45°]` AND `slope > 5%` should be **excluded** from the buildable area (north-facing slopes steeper than 5% are not viable for fixed-tilt solar in most standards).

### BD-02 | **P1 (Critical)** | `excluded_slope_percent: 15` in config is **ignored** — only `max_slope_percent: 10` is used

**File:** `constraints/constraint_combiner.py` — line 111; `config/config.yaml` — line 56  
**Problem:** The config defines two slope thresholds: `max_slope_percent: 10` and `excluded_slope_percent: 15`. The constraint combiner only reads `max_slope_percent`. The config comment implies `excluded_slope_percent` is a hard cutoff while `max_slope_percent` is the preferred maximum. This creates a silent data inconsistency: engineers might expect that terrain between 10–15% slope is "acceptable with grading" but the system currently treats 10% as the hard cutoff. The unused `excluded_slope_percent` is a dead config key.  
**Required fix:** Either (a) remove `excluded_slope_percent` from config and document `max_slope_percent` as the hard limit, or (b) implement two-tier slope filtering: `preferred_slope_percent` (< 5%) = full score, `max_slope_percent` (5–10%) = buildable with grading, `excluded_slope_percent` (> 15%) = hard exclusion.

### BD-03 | **P1 (Critical)** | `site_boundary_m` setback is **not in config.yaml** — defaults silently to 15 m

**File:** `constraints/constraint_combiner.py` — line 128; `config/config.yaml`  
**Problem:** `config.get("buffers", {}).get("site_boundary_m", 15)` reads a key `site_boundary_m` that does not exist in `config.yaml`. The 15 m default is applied silently. Industry standard for utility-scale solar is 5–10 m property setback (not 15 m) plus additional cell setbacks per state/national regulation. The missing config key means engineers cannot adjust this without finding the default in code.  
**Required fix:** Add `site_boundary_m: 10` to `config.yaml` under the `buffers` section and document its purpose.

### BD-04 | P2 | No **flood plain** or **wetland topographic** exclusion (independent of WorldCover)

**File:** `constraints/constraint_combiner.py`, `constraints/osm_downloader.py`  
**Problem:** WorldCover class 90 (Herbaceous Wetland) is excluded, but:

1. WorldCover wetland detection is based on 2021 imagery and misses seasonal flood plains at 10 m resolution.
2. No DEM-based flood risk analysis is performed (e.g., using a topographic wetness index or HAND — Height Above Nearest Drainage).
3. OSM `natural=floodplain` and `natural=wetland` features are not queried.
   **Engineering Impact:** The buildable area may include flood-prone areas that would be excluded in a real IFC (International Finance Corporation) environmental screening.

### BD-05 | P2 | No **curvature-based exclusion** in `combine_constraints()`, only in per-row terrain check

**File:** `constraints/constraint_combiner.py`; `layout/block_generator.py` — `_check_row_terrain()` line 84  
**Problem:** Curvature is computed and saved in `process_terrain()` but is **not used in buildable area generation**. The per-row check in `block_generator.py` catches some high-curvature terrain, but this happens after buildable area is already defined. This means curvature constraints are applied inconsistently (only during layout generation, not during buildable area delineation).  
**Required fix:** Add curvature exclusion to `combine_constraints()` using `raster_to_polygons(curvature_path, 0.4, ...)`.

### BD-06 | P2 | No **forest buffer** beyond WorldCover class 10 exclusion — tree height shading ignored

**File:** All constraint modules  
**Problem:** WorldCover excludes Tree Cover pixels, but the buffer around forest edges where trees cast shading on adjacent rows is not computed. In practice, 50–100 m of solar panels adjacent to a forest edge (on the equatorial side) can experience significant morning/afternoon shading.  
**Engineering Impact:** Panels near forest edges will underperform. No setback buffer is applied around the forest exclusion.

### BD-07 | P3 | TRI threshold `max_tri_m: 3.0` is too generous for fixed-tilt solar

**File:** `constraints/constraint_combiner.py` — line 120; `config/config.yaml`  
**Problem:** A TRI of 3.0 m means the average elevation difference between a pixel and its 8 neighbours is 3 m. For a 30 m DEM pixel, this corresponds to a ~10% slope — the same as `max_slope_percent`. However, TRI captures local roughness including rocky outcrops and gullies that slope alone misses. For fixed-tilt solar with standard pile foundations (driven into soil), terrain roughness averaging more than 1.5 m at the pile spacing scale is typically a red flag requiring expensive custom foundations.  
**Recommended fix:** Lower `max_tri_m` to `1.5` and validate against physical layout.

### BD-08 | P3 | Water body buffer (OSM `rivers_m: 50`) does not account for **waterway order/size**

**File:** `constraints/constraint_combiner.py` — `buffer_map`  
**Problem:** All OSM water features (streams, rivers, canals, lakes) receive the same 50 m buffer regardless of size. A small seasonal stream may require only 20 m while a major navigable river requires 100 m+ under most environmental regulations (IFC Performance Standard 6, ADB SPS).  
**Engineering Impact:** Overly conservative for small streams (wastes buildable area) and potentially under-conservative for large rivers.

---

## Category 3 — PV Layout Generation

### LG-01 | **P1 (Critical)** | GCR parameter from config is **never used** — pitch is derived from winter shadow angle only

**File:** `layout/block_generator.py` — `_compute_block_dimensions()`, line 44–46  
**Problem:** `config.yaml` defines `gcr: 0.38` and `row_pitch_m: 6.7` but these are ignored in the block dimension calculation. Instead, pitch is derived from the winter shadow constraint formula:

```python
gap = vertical_height / math.tan(math.radians(winter_solar_elevation))
pitch = table_height_m * math.cos(math.radians(tilt_deg)) + gap
```

This formula calculates the **no-shade pitch** at winter solstice solar noon — a reasonable starting point, but it ignores GCR, which is the primary engineering design parameter for utility-scale solar (PVcase, SAM all use GCR as the primary input). The resulting pitch may be much larger than GCR=0.38 would imply, reducing site yield.  
**Correct workflow:** `pitch = module_length_along_slope / GCR`, then verify against winter shadow constraint.

### LG-02 | **P1 (Critical)** | Block grid is axis-aligned (East-West columns) but **rows are placed East-West** — orientation is backwards for fixed-tilt in northern hemisphere

**File:** `layout/block_generator.py` — line 158  
**Code:**

```python
angle_deg = 180 if tracker_type == "fixed" and site_latitude > 0 else 0
```

This sets the row rotation angle to 180° for fixed-tilt in the northern hemisphere. `angle_deg=180` means the row rectangle is rotated 180° — which for an axis-aligned rectangle is identical to 0°. The block grid columns step East-West (`x += block_col_width`) and rows step North-South (`y += row_pitch`). But for fixed-tilt in the northern hemisphere, **rows should be oriented East-West (running along latitude lines)** with Python row **columns running North-South** (inter-row spacing goes North-South). The current code appears to produce E-W columns and N-S rows within them — the actual PV row footprint width (`phys_row_length`) runs East-West (correct), but the grid logic conflates "row" geometry with "column" geometry.  
**Engineering Impact:** Hard to detect visually unless GIS outputs are inspected, but the layout will be incorrect on non-square sites and will not correctly respect N-S pitch spacing versus E-W module length.

### LG-03 | P2 | Block capacity calculation scales `ac_capacity_mw` and `dc_capacity_mw` by `current_strings / target_strings_per_block` (line 273)

**File:** `layout/block_generator.py` — line 273  
**Problem:** `capacity_factor = current_strings / target_strings_per_block`. Block capacity is scaled by string ratio, which is correct in principle. However, `ac_capacity_mw` and `dc_capacity_mw` are read from config as fixed block capacities (3.2 MWac, 3.904 MWdc), not computed from `strings × modules × Wp`. These numbers are hardcoded design targets not computed from the module/inverter parameters in config. As a result, the actual electrical sizes derived from `module_power_w=635 W`, `modules_per_string=28`, `strings_per_inverter=22`, `inverters_per_block=10` are:

- DC per block = 10 inv × 22 strings × 28 modules × 635W = **3.902 MWdc** ← close but not exact
- AC per block = 10 × 320 kW = **3.2 MWac** ← matches  
  The discrepancy is small here, but if module parameters change in config without updating block capacities, the two values will diverge. Capacity must be computed from first principles.

### LG-04 | P2 | `_check_row_terrain()` hardcodes slope threshold at `10.0` (line 82) instead of reading `max_slope_percent` from config

**File:** `layout/block_generator.py` — line 82  
**Problem:** The hardcoded `10.0` doesn't respect the config value. If a user changes `max_slope_percent`, the row-level terrain check will silently still use 10%.

### LG-05 | P2 | `strings_per_row = 4` is hardcoded (line 50), not derived from block dimensions

**File:** `layout/block_generator.py` — line 50  
**Problem:** `strings_per_row = 4` determines how many string inverter inputs fit across one physical row. This should be derived from `row_width_m / module_width_m` and verified against `strings_per_inverter`. A hardcoded value of 4 is arbitrary and may not match the actual module count per row for different module sizes.

### LG-06 | P2 | Block 40% utilisation threshold (`current_strings >= target_strings_per_block * 0.4`) accepts very low-density blocks

**File:** `layout/block_generator.py` — line 270  
**Problem:** A block with only 40% utilisation means only 40% of the space is used by PV rows. This is a very low threshold. In standard solar design, blocks below ~70% utilisation are typically redesigned or merged. Accepting 40% blocks overstates the site's installed capacity relative to land used.

### LG-07 | P3 | No terrain-following row placement — rows are placed on a flat 2D grid without adapting to slope orientation

**File:** `layout/block_generator.py`  
**Problem:** The grid iterates on `(x, y)` positions in projected coordinates and places rows as flat rectangles. On sloped terrain (e.g. north-south running hillside), the actual module-to-module distance should follow the **slope-projected** surface, not the flat plan distance. The `_compute_slope_adjusted_pitch()` function exists but samples a single point slope for the entire row column (line 236-238), not the actual local slope at each row position.

### LG-08 | P3 | `_enforce_azimuth_limits()` contains incorrect logic — the `norm_angle` variable is computed but never used

**File:** `layout/block_generator.py` — lines 99–109  
**Problem:** `norm_angle = angle_deg % 180` is computed and assigned to `norm_angle` but never referenced again. All subsequent logic uses `angle_deg % 180` directly. Dead variable.

---

## Category 4 — BOP Siting

### BOP-01 | P2 | Substation siting candidates are sampled from the **site boundary** line, not from the interior buildable area near the boundary

**File:** `layout/substation_placement.py` — `_select_substation_point()` line 68–74  
**Problem:** Candidates are sampled by interpolating along `site_geom.boundary`. These points are literally on the site boundary perimeter. The BOP compounds are then built extending **inward from** those points. This approach places the substation at the edge of the site, which is correct for a grid connection point but:

1. The scoring criterion "proximity to site centroid" contradicts the centroid-facing placement — a substation in the site centre would be unreachable for the HV line without crossing the entire PV array.
2. Real substations are sited near the HV grid connection point (usually near a road or existing line), **not at the geometric centroid**.  
   **Missing:** No consideration of HV grid infrastructure proximity (which OpenTopography/OSM data already downloads via the `power` layer).

### BOP-02 | P3 | `_build_compound_polygons()` places the **guard house inside the O&M compound** (guard_geom = \_rect(om_cx, om_cy, gh, gw/2)), not at the gate

**File:** `layout/substation_placement.py` — line 215  
**Problem:** The guard house rectangle is centred at `(om_cx, om_cy)` — the centre of the O&M compound — extending `gh` metres in the inward direction. The guard house should be at the **entrance** of the O&M compound (near the boundary side), not at the compound centre.

### BOP-03 | P3 | BOP zone shift loop (lines 316–333) modifies GeoDataFrame geometries using `.geometry = ...translate(...)` — this is GeoPandas deprecated assignment; may produce silent wrong results

**File:** `layout/substation_placement.py` — lines 329–332  
**Problem:** `gdf.geometry = gdf.geometry.translate(...)` is equivalent to setting a column value on a copy, not modifying in place. In recent GeoPandas versions this raises a `SettingWithCopyWarning` or silently fails. The correct approach is `gdf = gdf.set_geometry(gdf.geometry.translate(...))` or `gdf["geometry"] = gdf.geometry.translate(...)`.  
**Engineering Impact:** If the translate is silently not applied, BOP compounds may remain outside the site boundary indefinitely, producing illegal layouts.

### BOP-04 | P4 | BESS compound sizing is fixed (60×30 m) regardless of `capacity_mw`/`capacity_mwh` — not scaled to BESS size

**File:** `layout/substation_placement.py`; `config/config.yaml`  
**Problem:** Real BESS compound footprints depend directly on the number of BESS containers, which scales with energy capacity (MWh). Typical utility BESS containers are ~18 m × 2.5 m each. A 20 MWh BESS at ~280 kWh/container requires ~72 containers needing a ~1,800 m² compound — but the current fixed size is 60×30 = 1,800 m² which coincidentally matches. However, this is not computed from the capacity config values. If BESS capacity is changed in config, the compound footprint does not update.

---

## Category 5 — Road Network

### RN-01 | **P1 (Critical)** | Road routing produces a **visually incorrect network** — no spine/branch hierarchy, routes over PV rows

**File:** `layout/routing.py` — `route_access_roads()`  
**Problem:** The routing graph is built over `blocks_gdf.copy()` geometry (line 128-135), then a `convex_hull.buffer(20)` corridor is created but then discarded (lines 132-133 — the `corridors` variable is never used). The actual routing grid uses `blocks_gdf` as the spatial domain, meaning grid nodes exist **inside** PV block geometries. Roads therefore route directly over PV panels rather than in the inter-row/inter-block aisles.  
**Required fix:** Build the routing grid over the **gaps between blocks** (block buffer minus block geometry), not over block interiors.

### RN-02 | **P1 (Critical)** | After routing, all road segments are re-labelled `"main_collector"` regardless of position — **hierarchy is destroyed**

**File:** `layout/routing.py` — lines 203–205  
**Code:**

```python
roads_gdf = roads_gdf.dissolve().explode(index_parts=False).reset_index(drop=True)
roads_gdf["road_type"] = "main_collector"
```

The entire road network is dissolved into one geometry and relabelled as `main_collector`. This removes the distinction between spine roads and branch service roads. Industry standard requires at minimum:

- **Main spine road** (6 m wide sealed): connects substation to main blocks, one per site
- **Branch service roads** (4 m wide gravel): serve groups of rows within each block
- **Inter-row maintenance aisles** (3 m wide): between every 2–3 rows

### RN-03 | P2 | Road routing grid resolution of 15 m is fixed — no config parameter

**File:** `layout/routing.py` — line 135  
**Problem:** `G = _create_routing_grid(blocks_gdf, resolution=15.0, ...)`. 15 m is reasonable but should be configurable (e.g., `roads.grid_resolution_m` in config). For large sites the 15 m grid creates enormous graphs (O(n²) node count) that are slow to solve.

### RN-04 | P2 | `_snap_point_to_grid()` iterates all nodes in O(n) — **quadratic time** for large grids

**File:** `layout/routing.py` — `_snap_point_to_grid()` lines 102–111  
**Problem:** For every block centroid, this function iterates all N grid nodes to find the nearest. For a site with thousands of grid nodes and tens of blocks, this is O(blocks × nodes). Should use `scipy.spatial.cKDTree` for O(blocks × log(nodes)) lookup.

---

## Category 6 — Electrical Routing (MV Cables)

### ER-01 | **P1 (Critical)** | MV cables are routed as **direct straight lines** from transformer to substation — the code comment says "road trench" but implementation ignores roads

**File:** `layout/routing.py` — `route_mv_cables()` lines 234–255  
**Problem:** Despite the comment at line 243 ("in reality, the EPC simply lays cables IN THE ROAD TRENCH"), the implementation uses:

```python
full_route = LineString([(trans_pt.x, trans_pt.y), (sub_pt.x, sub_pt.y)])
```

This generates straight-line cables that pass through PV panels, obstacles, and the substation. No road-following logic is implemented. The cable lengths in the engineering report will therefore be underestimated compared to real trench lengths.  
**Engineering Impact:** MV cable cost estimates in the report are incorrect (typically 15–40% shorter than actual trench lengths). The generated GIS output `mv_cables.geojson` is misleading.

### ER-02 | P2 | MV cable topology is labelled `"homerun_radial"` with one cable per transformer — but a single transformer serves an entire block (10 inverters × 320 kW = 3.2 MWac), and a 33 kV homerun per 3.2 MW block is correct

**File:** `layout/routing.py`; `config/config.yaml` — `mv_cables.topology: homerun`  
**Note:** The topology choice is architecturally correct, but the cable sizing (number, current rating, cross-section) is never computed. No conductor size, no thermal calculation, no cable de-rating is performed.

### ER-03 | P3 | LV cables (inverter → transformer) are drawn as **straight lines** regardless of panel row positions — cables would pass through panels

**File:** `layout/bop_placement.py` — line 127  
**Problem:** `line = LineString([inv_pt, xfmr_pt])`. Inverters are placed at the eastern edge of a row (line 67: `Point(maxx + 1.0, (miny + maxy)/2)`), and transformers at block centroids. Straight lines between them will cut through adjacent rows. LV cables should run along row ends (north or south face of the panel table) to the row access aisle.

---

## Category 7 — Engineering Calculations

### EC-01 | P2 | `_estimate_earthworks()` computes cut volume only (not fill) — a balanced cut/fill earthworks estimate requires both

**File:** `analysis/metrics.py` — `_estimate_earthworks()` lines 54–102  
**Problem:** Only the cut volume is computed (`valid[valid > mean_elev] - mean_elev`). Fill volume (`mean_elev - valid[valid < mean_elev]`) is not computed. A real earthworks estimate requires both, plus a shrinkage/swell factor (typically 1.15–1.30 for soil). The report shows "Cut/Fill Extrapolated Volume" but only reports cut.

### EC-02 | P2 | GCR calculation in metrics is computed as `pv_area_ha / block_area_ha` — this is not GCR

**File:** `analysis/metrics.py` — line 163  
**Problem:** `gcr_achieved = pv_area_ha / block_area_ha`. This computes the ratio of panel footprint to block footprint, which is approximately GCR only if blocks contain only panels. GCR is defined as `module_area / land_area_per_row` = `(module_length × num_modules_in_cross_section) / pitch`. The metrics GCR is a rough approximation but will be systematically lower than true GCR because block footprints include access aisles.

### EC-03 | P2 | `_get_pvwatts_yield()` uses `azimuth: 180` (south-facing default) regardless of site hemisphere or actual panel orientation

**File:** `analysis/metrics.py` — line 37  
**Problem:** For southern hemisphere sites, the optimal azimuth is 0° (north-facing). A south-facing array at the equator or in the southern hemisphere will give artificially inflated losses. The PVWatts API already corrects for hemisphere if using NRELs resource data, but passing azimuth=180 for a southern hemisphere site is a systematic error.

### EC-04 | P3 | `capacity_estimator.py` not reviewed — referenced but not audited

**File:** `analysis/capacity_estimator.py`  
**Status:** File not reviewed in this audit pass. Should be audited in Phase 4 implementation.

### EC-05 | P3 | Specific yield fallback `dc_mw * 1600` MWh/MWdc assumes 1600 kWh/kWp which is only valid for equatorial climates

**File:** `analysis/metrics.py` — line 27  
**Problem:** The default yield of 1600 kWh/kWp is appropriate for sites with ~5.0 kWh/m²/day GHI (typical of Myanmar/SE Asia). For temperate or arid sites, this could be 30–50% wrong. The fallback should at minimum warn in the report when it is being used.

### EC-06 | P4 | Duplicate `import` and duplicate `logger` definition in metrics.py

**File:** `analysis/metrics.py` — lines 1–13  
**Problem:** `import logging`, `import os`, and `logger = logging.getLogger(...)` are defined twice (lines 1-6 and 7-13). This is a dead code fragment from a copy-paste merge. No functional impact but indicates messy refactoring.

---

## Category 8 — Reporting & Output Artefacts

### RP-01 | **P1 (Critical)** | Phase 1 required outputs are **partially missing or named differently** from specification

**Required outputs (from audit brief) vs actual outputs:**

| Required                      | Actual                                   | Match?                                 |
| ----------------------------- | ---------------------------------------- | -------------------------------------- |
| `buildable_area.geojson`      | `outputs/geojson/buildable_area.geojson` | ✓ (named correctly in save_gis_layers) |
| `terrain_slope_map.png`       | `outputs/slope_map.png`                  | ✗ (wrong name)                         |
| `terrain_aspect_map.png`      | ❌ Not generated                         | ✗ (aspect map missing entirely)        |
| `terrain_constraints_map.png` | `outputs/constraint_map.png`             | ✗ (wrong name)                         |
| `terrain_ruggedness_map.png`  | ❌ Not generated                         | ✗ (TRI map missing entirely)           |

**File:** `visualization/map_generator.py` — `create_terrain_maps()` lines 248–275  
**Fix needed:** Add aspect and TRI raster plots; rename outputs to match Phase 1 specification.

### RP-02 | P2 | `create_terrain_maps()` passes `exclusions_gdf` as an optional parameter but `main_pipeline.py` (line 232) calls it without `exclusions_gdf`

**File:** `visualization/map_generator.py` — line 248; `main_pipeline.py` — line 232  
**Problem:** The signature is `create_terrain_maps(terrain_paths, site_gdf, output_dir, exclusions_gdf=None)`. The call is `create_terrain_maps(terrain_paths, site_gdf, output_dir)`. The constraint map therefore always renders without the exclusion overlay in terrain maps — the exclusion polygons are only shown in the constraint map from the main `create_layout_map()` call. The terrain constraint map should show exclusion overlays for slope, TRI, and aspect.

### RP-03 | P2 | The engineering report references images (`layout_map.png`, `constraints_map.png`, `slope_map.png`) using incorrect relative paths — they are in the **same directory** as the report, but `constraints_map.png` is generated while the report references `constraint_map.png` (no 's')

**File:** `analysis/metrics.py` — lines 288, 299  
**Problem:** The report markdown uses `![Constraints Map](constraints_map.png)` but the file is saved as `constraint_map.png` (no 's'). Visualisers will show a broken image.

### RP-04 | P3 | GeoJSON output uses UTM (projected) coordinates, not WGS84 longitude/latitude — many GIS tools expect WGS84 for GeoJSON per RFC 7946

**File:** `visualization/map_generator.py` — `save_gis_layers()` lines 36–40  
**Problem:** GeoJSON files are saved in the UTM projection CRS. RFC 7946 (GeoJSON standard) mandates WGS84 geographic coordinates. Most web GIS tools (Mapbox, Leaflet, QGIS quick import) will fail or misplace geometries if given projected GeoJSON.  
**Fix:** Add `.to_crs(epsg=4326)` before saving GeoJSON files. Shapefiles and GeoPackage may retain UTM.

### RP-05 | P3 | Earthworks report claims "Cut/Fill" but computes only cut — misleading engineering report

**File:** `analysis/metrics.py` — line 343  
**(Also documented under EC-01)** — the report header says "Cut/Fill Extrapolated Volume" but only cut volume is computed.

---

## Priority Matrix

| ID     | Category           | Priority | Effort | Phase |
| ------ | ------------------ | -------- | ------ | ----- |
| BD-01  | Buildable Terrain  | **P1**   | Medium | 1     |
| BD-02  | Buildable Terrain  | **P1**   | Low    | 1     |
| BD-03  | Buildable Terrain  | **P1**   | Low    | 1     |
| RP-01  | Reporting/Output   | **P1**   | Low    | 1     |
| LG-01  | PV Layout          | **P1**   | Medium | 3     |
| LG-02  | PV Layout          | **P1**   | High   | 3     |
| RN-01  | Road Network       | **P1**   | High   | 5     |
| RN-02  | Road Network       | **P1**   | Low    | 5     |
| ER-01  | Electrical Routing | **P1**   | High   | 6     |
| TA-01  | Terrain Analysis   | P2       | Low    | 2     |
| TA-02  | Terrain Analysis   | P2       | Low    | 1     |
| BD-04  | Buildable Terrain  | P2       | Medium | 1     |
| BD-05  | Buildable Terrain  | P2       | Low    | 1     |
| BD-06  | Buildable Terrain  | P2       | Low    | 1     |
| LG-03  | PV Layout          | P2       | Low    | 3     |
| LG-04  | PV Layout          | P2       | Low    | 3     |
| LG-05  | PV Layout          | P2       | Low    | 3     |
| LG-06  | PV Layout          | P2       | Low    | 3     |
| BOP-01 | BOP Siting         | P2       | Medium | 4     |
| BOP-03 | BOP Siting         | P2       | Low    | 4     |
| RN-03  | Road Network       | P2       | Low    | 5     |
| RN-04  | Road Network       | P2       | Low    | 5     |
| ER-02  | Electrical         | P2       | Medium | 6     |
| ER-03  | Electrical         | P2       | Medium | 6     |
| EC-01  | Engineering Calc   | P2       | Low    | 7     |
| EC-02  | Engineering Calc   | P2       | Low    | 7     |
| EC-03  | Engineering Calc   | P2       | Low    | 7     |
| RP-02  | Reporting          | P2       | Low    | 1     |
| RP-03  | Reporting          | P2       | Low    | 1     |
| TA-03  | Terrain Analysis   | P3       | Medium | 2     |
| BD-07  | Buildable Terrain  | P3       | Low    | 1     |
| BD-08  | Buildable Terrain  | P3       | Low    | 1     |
| LG-07  | PV Layout          | P3       | High   | 3     |
| LG-08  | PV Layout          | P3       | Low    | 3     |
| BOP-02 | BOP Siting         | P3       | Low    | 4     |
| BOP-04 | BOP Siting         | P3       | Low    | 4     |
| EC-04  | Engineering Calc   | P3       | Low    | 4     |
| EC-05  | Engineering Calc   | P3       | Low    | 7     |
| RP-04  | Reporting          | P3       | Low    | 7     |
| RP-05  | Reporting          | P3       | Low    | 7     |
| TA-04  | Terrain Analysis   | P3       | Low    | 2     |
| TA-05  | Terrain Analysis   | P4       | Low    | 2     |
| EC-06  | Engineering Calc   | P4       | Low    | 7     |

---

## Phase 1 Implementation Plan — Buildable Terrain Detection

**Scope:** Fix all P1 and selected P2 issues in terrain filtering before any PV layout work begins.

### Changes to `config/config.yaml`

- Add `site_boundary_m: 10` to `buffers`
- Clarify `excluded_slope_percent: 15` meaning or remove
- Add `max_aspect_penalty_slope_pct: 5` (north-facing slope exclusion threshold)
- Add `forest_buffer_m: 50` to `buffers`

### Changes to `terrain/terrain_analysis.py`

- Fix `calculate_suitability()` aspect penalty formula (TA-02)
- Add east/west slope penalty (slopes >5% facing E/W get 30% score reduction) (TA-01)

### Changes to `constraints/constraint_combiner.py`

- Add aspect exclusion: north-facing terrain (`aspect ∈ [315°, 45°]`) with `slope > 5%` excluded for northern hemisphere (BD-01)
- Remove dead `excluded_slope_percent` key or implement two-tier slope logic (BD-02)
- Add `site_boundary_m` to config with correct default (BD-03)
- Add curvature exclusion raster-to-polygon (BD-05)
- Add forest buffer (BD-06): buffer existing `lulc_Tree cover` exclusion polygons by `forest_buffer_m`
- Lower TRI threshold recommendation to 1.5 m (BD-07)

### Changes to `visualization/map_generator.py`

- Add `terrain_aspect_map.png` and `terrain_ruggedness_map.png` outputs (RP-01)
- Rename `slope_map.png` → `terrain_slope_map.png` (RP-01)
- Rename `constraint_map.png` → `terrain_constraints_map.png` (RP-01)
- Pass `exclusions_gdf` in `create_terrain_maps()` call in `main_pipeline.py` (RP-02)
- Fix broken image reference in report: `constraints_map.png` → `terrain_constraints_map.png` (RP-03)
- Save GeoJSON files in WGS84 (RP-04)

### Required Phase 1 Outputs After Fix

- `outputs/geojson/buildable_area.geojson` — buildable area excluding all constraints
- `outputs/terrain_slope_map.png` — slope map with site boundary overlay
- `outputs/terrain_aspect_map.png` — aspect map with N-facing penalty zones shown
- `outputs/terrain_constraints_map.png` — all exclusion zones by type
- `outputs/terrain_ruggedness_map.png` — TRI map with threshold indicated

### Manual Validation Criteria (Phase 1)

1. Open `buildable_area.geojson` in QGIS: verify north-facing slopes are excluded on sites with known N-facing hillsides
2. Open `terrain_slope_map.png`: verify steep terrain (>10%) is highlighted in red; flat areas (<5%) in green
3. Open `terrain_aspect_map.png`: verify north-facing areas (NW–NE sectors) are visually distinguished
4. Open `terrain_constraints_map.png`: verify all exclusion types are present with legend
5. Check `engineering_report.md`: verify buildable area percentage is realistic (typically 40–70% for undisturbed rural sites)

---

_End of Audit — Proceed to Phase 1 implementation only after this document is reviewed._
