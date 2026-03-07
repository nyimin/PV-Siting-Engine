# PV Layout Engine

An industry-grade geospatial pipeline for utility-scale solar plant early-stage development, terrain analysis, and conceptual engineering layout generation.

This engine transforms unrefined site boundaries into full conceptual engineering layouts using public global datasets, automatically generated PV row geometries, and terrain-aware routing algorithms.

---

## 🚀 Key Engineering Features

### 📡 Data Acquisition & Validation

- **OpenTopography DEMs:** Automatic fetching of COP30/COP90 elevation models with multi-attempt retry logic and resolution validation.
- **ESA WorldCover (10m):** Direct S3 downloads of Land Use/Land Cover tiles for automated exclusion zones (forests, water, urban, etc.).
- **OpenStreetMap Constraints:** Extraction of infrastructure (roads, buildings, waterways, powerlines) with configurable setbacks.
- **Data Caching:** MD5-hashed caching to accelerate multi-run iterations on identical sites.

### ⛰️ Advanced Terrain Analytics

- **Projected Metric Analysis:** Automatically detects and projects data to the appropriate local UTM zone for accurate metric calculations.
- **Topographic Derivatives:** Generates Slope, Aspect, Curvature, Terrain Ruggedness Index (TRI), and Hillshade.
- **Hydrology & Flood Risk (PySheds):** Natively calculates Topographic Wetness Index (TWI) and D8 Flow Accumulation catchments to automatically generate exclusion buffers around likely stream channels and ravines.
- **Structural Slope Geometry:** Classifies terrain gradients directly against panel row azimuths to generate **Across-row vs Along-row slope gradients** to inform earthworks and tracker viability.
- **TPI Exclusions:** Utilizes a Topographic Position Index (TPI) to detect deep valleys/channels without relying on OSM vector data. _Configured for commercial utility-scale tolerances (e.g., -2.0m valley threshold, 1.5m TRI)._
- **Solar Suitability Scoring:** Produces a 0–100 spatial score raster weighting slope thresholds, TRI, and hemisphere-aware north-facing penalties.

### 🏗️ Solar Layout & BOP Siting

- **Multi-Criteria Substation Placement:** Siting logic evaluates 5 criteria (20% weight each): flat terrain, centroid proximity, road access, flood risk avoidance, and buildability.
- **BOP Zone Reservation:** Automatically reserves and carves out space for Substation, BESS (Battery Energy Storage System), and O&M compounds _before_ panel placement, mirroring industry-standard tools like PVcase.
- **Strip-Based Block Generation:** Dynamically chunks PV rows across N-S columns to maximize variable boundary space, recovering otherwise discarded blocks to increase total installed capacity. Maintains strict True South (E-W) alignment for fixed-tilt systems.
- **Geometric Block Generation:** Clusters and generates PV blocks (e.g., 3.2 MWac) with true module-level geometry, strings, and inverters.
- **Central Skids & Internal Access Roads:** Automatically carves internal access roads (e.g. 6m gaps) through the heart of utility blocks, centering the Virtual Central Skids (Block Transformers & tightly clustered String Inverters).

### ⚡ Infrastructure Routing

- **Terrain-Aware A\* Spine Road:** Generates a dominant "main collector" spine road from the substation using a 10m-resolution A\* pathfinder restricted entirely within the true buildable area, guaranteeing it never crosses exclusion zones.
- **Herringbone Comb Branches:** Branch roads are generated perpendicular to the local tangent of the curving spine road, creating a natural comb network that flows with the terrain geometry.
- **Strict Paddock Geometric Containment:** PV Blocks are explicitly clipped to the precise boundaries of "PV Paddocks" (the buildable area minus road corridors), mathematically guaranteeing no overlaps between panels and roads/cable corridors.
- **Daisy-Chain MV Cables:** 33 kV medium-voltage cables string block transformers together in a daisy-chain along the physical road graph, eliminating cross-country routing and accurately representing real-world commercial topology.
- **Separated Networks:** Maintains distinct GIS layers for internal access roads, MV electrical cables, and LV DC/AC cabling.

### 📊 Reporting & Outputs

- **Engineering Report:** Generates a comprehensive Markdown report including DC/AC ratio, GCR, component counts, infrastructure lengths, and a detailed constraints breakdown.
- **CAPEX Economic Scoring:** Fully calculates Blended CAPEX and Specific CAPEX ($/Wdc) leveraging configurable unit costs for Modules, Inverters, Roads, MV Cables, and Earthworks.
- **Civil Earthworks Estimation:** Rough Cut/Fill volume (m³) calculation using planar fit analysis over block areas.
- **Yield Integration:** Estimated annual energy yield integration via NREL PVWatts API.
- **Visual Assets:** Exports 10+ GIS layers (GeoJSON, GPKG), high-resolution static maps, and interactive HTML dashboards.

---

## 🛠️ Requirements

The project uses a standard Python virtual environment and relies on the geospatial Python stack:
`geopandas`, `rasterio`, `pysheds`, `osmnx`, `networkx`, `shapely`, `pyyaml`, `scipy`, `requests`, `python-dotenv`.

## ⚙️ Installation

1. **Create and activate a virtual environment:**

   ```bash
   python -m venv .venv
   .venv\Scripts\activate  # Windows
   source .venv/bin/activate  # Linux/macOS
   ```

2. **Install dependencies:**

   ```bash
   pip install -r requirements.txt
   ```

3. **Configure Credentials:**
   Create a `.env` file in the project root:
   ```env
   OPENTOPOGRAPHY_API_KEY=your_key_here
   NASA_EARTHDATA_USER=your_user
   NASA_EARTHDATA_PASSWORD=your_password
   PVWATTS_API_KEY=your_nrel_key (optional)
   ```

## ⌨️ Usage

Run the pipeline by pointing to a site boundary vector file (GeoPackage, GeoJSON, or Shapefile) and providing the target capacity in MW DC.

```bash
python main_pipeline.py inputs/project_boundary.gpkg 60.0
```

## 📂 Output Structure

Results are stored in `outputs/`:

- `outputs/layout.gpkg`: Unified spatial database of all site infrastructure.
- `outputs/engineering_report.md`: Tabulated feasibility metrics, exclusions, and earthworks.
- `outputs/interactive_map.html`: Web-based view of the complete layout.
- `outputs/*_map.png`: Visual renderings of layout, slope, and suitability.

---

## ⚖️ Standards Compliance

The engine follows international engineering best practices including:

- **IEC 60364-7-712**: Solar PV power supply systems.
- **IEC 61936-1**: Power installations exceeding 1 kV a.c. (Substation clearances).
- **IRENA/IEA-PVPS**: Utility-scale PV design concepts and O&M sizing.
- **ADB Environmental Safeguards**: Watercourse and social infrastructure setbacks.
