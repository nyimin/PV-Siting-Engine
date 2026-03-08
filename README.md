# PV Layout Engine

**Disclaimer**: This engine is a **conceptual layout and feasibility analysis tool for early-stage utility-scale solar project development**. It explores bounding boxes, yields, and layout optimization using public constraint datasets to provide conceptual engineering layouts. It generates real geometries, terrain analysis, and estimates capacity, but is not a substitute for detailed, certified engineering design.

An industry-grade geospatial pipeline that transforms unrefined site boundaries into conceptual engineering layouts using public global datasets, dynamic PV row generation, and terrain-aware algorithms.

---

## 🚀 Key Engineering Features

### 📡 Data Acquisition & Validation

- **OpenTopography DEMs:** Automatic fetching of COP30/COP90 elevation models with multi-attempt retry logic and cubic sub-grid resampling validation.
- **ESA WorldCover (10m):** Direct S3 downloads of Land Use/Land Cover tiles for automated exclusion zones (forests, water, urban, etc.).
- **OpenStreetMap Constraints:** Extraction of infrastructure (roads, buildings, waterways, powerlines) with configurable polygon setbacks.
- **Data Caching:** MD5-hashed caching to accelerate iterative pipeline runs on identical boundaries.

### ⛰️ Advanced Terrain Analytics

- **Projected Metric Analysis:** Automatically detects and projects data to the appropriate local UTM zone for accurate metric geometry calculations.
- **Topographic Derivatives:** Generates explicit Slope (degrees), Aspect, Curvature, Terrain Ruggedness Index (TRI), and Hillshade models using Horn's method and Zevenbergen-Thorne algorithms.
- **Hydrology & Flood Risk (PySheds):** Computes Topographic Wetness Index (TWI) and D8 Flow Accumulation catchments to automatically generate exclusion buffers against ravines and likely stream networks.
- **TPI Exclusions:** Utilizes Topographic Position Index (TPI) to detect deep channels and ridges independent of OSM vector data.
- **Solar Suitability Scoring:** Produces a 0–3 spatial score evaluating slope classes and northern/southern-hemisphere-aware aspect suitability.

### 🏗️ Conceptual Layout Generation & BOP Siting

- **Multi-Criteria BOP Siting:**
  - Substation, BESS, and O&M compounds are sited before panel layout using a weighted scoring grid (Terrain Flatness, POI Proximity, Buildable Coverage, Road Access, Water Avoidance).
  - **Terrain-Aware Orientation:** Compounds are automatically oriented along contour lines to minimize civil grading.
  - **R3 ECG Feedback Loop:** Evaluates the capacity-weighted Electrical Centre of Gravity (ECG) post-generation and re-sites the BOP zone if cable run distances exceed configurable thresholds.
- **Road-First Tessellation:** Generates primary and secondary infrastructure corridors _before_ block generation to ensure dedicated, reserved space and eliminate overlaps.
- **BFS Region-Growing Clustering:** Generates contiguous ~3.2 MWac utility blocks using adjacency-based grouping instead of arbitrary K-means.
- **Exact Target Capacity Truncation:** Dynamically truncates block and row production to meet the target AC MW requested exactly, prioritizing flat terrain and proximity to BOP.

### ⚡ Infrastructure Routing

- **Terrain-Aware A\* Road Routing:** Generates A\* navigational paths for branch roads and spine collectors utilizing a cost grid that penalizes gradients exceeding configurable thresholds (e.g., >5%).
- **Road-Following MV Collection:** Routes 33 kV medium-voltage lines topologically referencing the physical road network.
- **Daisy-Chain Topology:** Implements radial daisy-chaining of block transformers back to the substation with K-means spatial feeder grouping.
- **IEC Cable Sizing:** Automatically selects XLPE Aluminium conductor sizes per feeder load and computes voltage drop (IEC 60502-2 / 60287).

### 📊 Reporting & Analytics

- **Advanced Yield Simulation (PySAM):** Employs a robust simulation pipeline using the NREL PySAM engine (System Advisor Model) for P50 energy estimates, with fallback to PVWatts or local latitude proxies.
- **CAPEX Economics:** Generates complete Blended CAPEX and Specific CAPEX ($/Wdc) using detailed unit pricing models.
- **Civil Earthworks Estimates:** Resolves rough cut/fill volumes against the underlying 10m topological surface.
- **Full GIS Portfolio:** Emits Folium interactive maps, GeoJSON layers, QGIS-ready GeoPackages, and comprehensive tabular reports.

---

## 🛠️ Requirements

The project uses a standard Python virtual environment and heavily relies on the scientific geospatial ecosystem:
`geopandas`, `rasterio`, `pysheds`, `osmnx`, `networkx`, `shapely`, `pyyaml`, `scipy`, `requests`, `python-dotenv`, `nrel-pysam`.

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
   NREL_API_KEY=your_key_here (optional, for PVWatts fallback)
   ```

## ⌨️ Usage

Run the pipeline by pointing to a site boundary vector file (GeoPackage, GeoJSON, or Shapefile) and providing the conceptual target layout capacity in MW DC or AC MW threshold.

```bash
python main_pipeline.py inputs/project_boundary.gpkg 60.0 --config config/config.yaml
```

## 📂 Output Structure

Execution writes highly specific spatial metrics into `outputs/`:

- `outputs/layout.gpkg`: Unified spatial database hosting block, road, compound, constraint, and cable geometries.
- `outputs/engineering_report.md`: Extensive textual synthesis containing economic metrics, equipment counts, exclusions, and yields.
- `outputs/interactive_map.html`: Interactive leaflet-style map summarizing the generated geometries.
- `outputs/*_map.png`: Graphical output maps visualizing localized slope profiles relative to placement points.

---

## ⚖️ Standards Compliance

Configurable parameters are currently defaulted to adhere to foundational engineering limits (South East Asian considerations):

- **IEC 60364-7-712**: Solar PV power supply systems
- **IEC 60502-2 / 60287**: MV cable conductor sizing and trench limits
- **IEC 61936-1**: Substation high-voltage placement tolerances
- **ADB Environmental Safeguards**: Mandatory public infrastructure & river boundary exclusions

---

## 📄 License

**Proprietary and Confidential**

This software, including all its methodologies, algorithms, and models, is the property of its respective owners. Unauthorized copying, distribution, or use of this software, via any medium, is strictly prohibited without express written permission.
