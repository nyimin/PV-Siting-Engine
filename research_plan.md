## Resolving Geometric Overlaps in Commercial PV Design Software

The user is experiencing persistent issues with geometric overlaps in a custom Python-based PV Layout Engine. Specifically, Balance of Plant (BOP) components (substations, BESS) and internal access roads are overlapping with the generated PV rows (tables/strings). Attempts to fix this via simple clipping, buffering, and boolean intersections have not fully resolved the visual anomalies.

### Goal

Investigate how industry-standard commercial solar design software (e.g., PVcase, Helioscope, PVsyst, AutoCAD Civil 3D, RatedPower) and academic literature handle geometric exclusion, collision avoidance, and topology rules during utility-scale PV layout generation.

### Key Findings

#### 1. Algorithmic Approach: Generative Masking vs. Pruning

Commercial software and academic models generally approach utility-scale PV collision avoidance using two primary paradigms:

- **Constraint-Based Generative Placement (Masking First):** High-end tools like ratedpower and PVcase typically subtract all exclusion zones (roads, topography constraints, environmental buffers, shadow polygons) from the global site boundary _before_ any panels are placed. This creates a highly complex "Buildable Area MultiPolygon". The generation algorithm then tessellates strictly inside this polygon. This is the approach our `PVLayoutEngine` is attempting to use.
- **Global Grid Pruning (Heuristics First):** Faster, preliminary tools overlay a massive global grid of rows across the entire site disregarding exclusions. In a secondary pass, they use rapid spatial indexing (like R-trees) to perform intersection checks against exclusion zones, aggressively pruning any row that collides.

#### 2. Topological Healing and Boolean Robustness

A significant challenge in both commercial GIS tools and custom python scripts (`shapely`/`geopandas`) is computational geometry failures (TopologyExceptions) during boolean subtractions (e.g., `buildable_area.difference(corridor)`).

- **Snapping and Precision:** Commercial CAD/GIS tools rely on extremely strict coordinate precision models to prevent "sliver polygons" (polygons with near-zero area but complex boundaries).
- **Topological Healing:** Functions equivalent to shapely's `.make_valid()` or `.buffer(0)` are universally applied before and after every boolean operation to resolve self-intersections that cause difference engines (like GEOS) to fail silently or crash. Our engine now implements this via `make_valid()`.

#### 3. String Clipping ("Partial Rows")

When a PV row intersects an exclusion zone (like a road cutting diagonally across a block):

- Academic papers (e.g., utilizing Mixed-Integer Linear Programming - MILP) treat the row length as a variable and optimize it.
- Commercial tools use discrete **Clipping Algorithms**. If an exclusion zone cuts a 100-module row into a 40-module segment and a 20-module segment (dropping 40 modules):
  - The tool checks the remaining segments against the _Minimum String Length_ constraint (e.g., 28 modules).
  - The 40-module segment is kept (but rounded down to 28, dropping the extra 12 to maintain electrical string sizes).
  - The 20-module segment is entirely deleted because it cannot form a complete electrical string.
- Our current implementation in `block_generator.py` utilizes a similar logic by strictly calculating `int(round(clipped_width / string_ew_width))` and rejecting rows that don't satisfy the threshold.

#### 4. Handling of Visual Anomalies (Our Recent Bug)

The bug we recently fixed (BOP buffer shifting but buildings remaining static, and roads extending past buildable areas) is a classic "Separation of Concerns" issue known in computational geometry as the **Data Model vs. View Model Desynchronization**. Commercial software mitigates this by enforcing strict topological relationships—if a geometry is a child of an exclusion zone (like a Substation is a child of the Substation Buffer), any transformation applied to the parent topological node is automatically matrix-multiplied to all child geometries before rendering.

### Summary for the User

The approaches we have recently implemented in the `PVLayoutEngine` (masking buildable areas first, wrapping difference operations in `.make_valid()`, and dynamically clipping row linestrings to the remaining polygon bounds) are exactly aligned with standard computational geometry pipelines used in commercial layout tools and academic MILP formulations.

The previous overlaps were not due to a flawed geometric algorithm, but rather a simple coordinate translation bug during rendering (Data vs. View desynchronization), which has now been fixed.

### Expected Output

A comprehensive but concise markdown report detailing the theoretical and practical solutions to geometric overlaps in PV plant generation, providing actionable insights that can be mapped back to our Python (`geopandas`/`shapely`) pipeline.
