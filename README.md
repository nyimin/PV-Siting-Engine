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
- **Topographic Derivatives:** Generates explicit Slope, Aspect, Curvature, Terrain Ruggedness Index (TRI), and Hillshade models.
- **Hydrology & Flood Risk (PySheds):** Computes Topographic Wetness Index (TWI) and D8 Flow Accumulation catchments to automatically generate exclusion buffers against ravines and likely stream networks.
- **TPI Exclusions:** Utilizes Topographic Position Index (TPI) to detect deep channels independent of OSM vector data.
- **Solar Suitability Scoring:** Produces a 0–100 spatial score evaluating slope thresholds, TRI constraints, and northern-hemisphere-aware aspect penalties.

### 🏗️ Conceptual Layout Generation & BOP Siting

- **Substation & BOP Siting (Feedback Loop):**
  - Substation sites are selected before panel layout using multi-criteria suitability (Terrain Flatness: 30%, Proximity to POI: 20%, Buildable Coverage: 20%, Road Access / Water Avoidance: 15% each).
  - Reserves footprints for Substation, BESS, and O&M compounds.
  - **R3 Feedback Loop**: Evaluates the electrical center of gravity post-generation and re-sites the BOP zone dynamically if cable run distances exceed configurable thresholds.
- **Contiguous Block Generation:** Generates conceptual 3.2 MWac utility blocks. Replaced legacy clustering with terrain-respecting region-growing algorithms to aggressively maximize target capacity contiguousness.
- **Exact Target Capacity Optimization:** Dynamically prioritizes string and table alignment to hit target requested AC MW exactly without unnecessarily consuming usable land.
- **Optional Terrain Aisles:** Supports terrain-aligned tertiary access aisles configurable to split structural array generation.

### ⚡ Infrastructure Routing

- **Terrain-Aware A\* Road Construction:** Generates A\* navigational corridors for primary "spine" collectors utilizing a highly configurable cost grid respecting terrain gradients (>5% penalty).
- **Geometric Corridors:** Reserves straight, buffered line corridors extracted directly from the buildable area prior to layout generation, preventing overlapped modules.
- **Daisy-Chain MV Feeder Routing:** Routes 33 kV medium-voltage lines topologically referencing the created physical road network (daisy-chain) utilizing NetworkX. Applies IEC standard capacity limits for sizing and computing voltage drop (≤3%).

### 📊 Reporting & Economics

- **Bankable Yield Modelling (PySheds):** Employs a robust 3-tier energy simulation pipeline ranging from a high-fidelity PySAM simulator (shade/slope aware), falling back to NREL PVWatts SDK, and a local latitude proxy for offline usage.
- **CAPEX Economics:** Generates complete Blended CAPEX and Specific CAPEX ($/Wdc) using detailed unit pricing models for Modules, Inverters, Earthworks, and Cabling.
- **Civil Earthworks Estimates:** Resolves rough cut/fill calculations against the underlying 10m topological surface.
- **Full Report and Visual Assets:** Emits HTML folium maps, GeoJSON layers, QGIS-ready GeoPackages, and comprehensive tabular markdown reports breaking down exclusions, blocks, strings, feeders, and financials.

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
