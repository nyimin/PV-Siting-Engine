# Solar PV Plant Engineering Standards — Southeast Asia Reference

_Applicable to utility-scale fixed-tilt systems in Myanmar, Thailand, Vietnam, and the broader ASEAN region._
_Sources: IEC 60364-7-712, IEC 61936-1, IEC 62271-200, IEC 62446-1, IEC 62305, IEA-PVPS Task 13, IRENA Utility-Scale PV Technical Concepts (2016/2020), ADB Procurement Specs, Myanmar Electric Power Law 2014._

---

## 1. Site Boundary & Setbacks

| Parameter                         | Value                       | Standard / Note                 |
| --------------------------------- | --------------------------- | ------------------------------- |
| Site boundary setback (PV panels) | **15 m** min                | Standard industry / Myanmar EIA |
| Setback from public road          | **20 m** min                | Myanmar road reserve standard   |
| Setback from watercourse          | **50 m** min                | ADB Environmental Safeguards    |
| Setback from HV power line        | **30 m** min (per kV class) | IEC 61936-1                     |
| Setback from residential area     | **100 m** recommended       | Glint/noise buffer, ADB         |

---

## 2. Internal Access Roads

| Parameter                             | Value                                                   | Standard / Note                              |
| ------------------------------------- | ------------------------------------------------------- | -------------------------------------------- |
| **Main collector road width**         | 6–8 m (sealed or compacted gravel)                      | IRENA Utility-Scale PV; fire vehicle access  |
| **Inter-block maintenance road**      | 4 m min (between block rows)                            | IEA-PVPS T13                                 |
| **Row-end turnaround**                | 10 m turning radius                                     | Crane/truck access                           |
| Maximum gradient                      | 5% (10% absolute max)                                   | For monsoon drainage and vehicle safety      |
| **Road camber**                       | 2–3% cross-fall for drainage                            | Myanmar monsoon runoff design                |
| Road surface (main)                   | Compacted laterite gravel, 150mm depth                  | Myanmar standard (laterite widely available) |
| Road surface (access near substation) | Bitumen-sealed, min 100mm                               | HV equipment access                          |
| Cable-road crossings                  | HDPE conduit, 1.2m depth, concrete encased at crossings | IEC 60364-5-52                               |
| Road-to-public road connection        | Mandatory, at site boundary                             | ADB project specs                            |
| Substation access road                | Sealed, connects to public road, 6m min width           | IEC 61936-1 compound access                  |
| Vegetation clearance from road edge   | 1 m each side                                           | Fire prevention, O&M visibility              |

---

## 3. MV Collection Cable System (33 kV)

| Parameter                       | Value                                                                                           | Standard / Note                                       |
| ------------------------------- | ----------------------------------------------------------------------------------------------- | ----------------------------------------------------- |
| **Collection voltage**          | 33 kV AC                                                                                        | Myinsai concept design                                |
| **Topology**                    | **Homerun radial** (one dedicated 33kV circuit per block transformer direct to main substation) | Best practice for ≤20 blocks; avoids cascading outage |
| Cable type                      | 3-core XLPE 33kV (IEC 60502-2)                                                                  | UV/heat/moisture resistant                            |
| Conductor                       | Stranded aluminium or copper, sized for <2% volt drop at full load                              | IEC 60287                                             |
| Trench depth                    | 1.2 m under open ground, 1.5m under roads                                                       | IEC 60364-5-52                                        |
| **Routing rule**                | **Cables trenched alongside roads** (not cross-country) to minimize earthwork duplication       | Industry standard                                     |
| Water/drainage channel crossing | Min 1 m separation or encased crossing                                                          | IEC 60364-5-52                                        |
| Marker tape                     | 300 mm above cable, "Danger — HV Cable Below"                                                   | IEC 60364-5-52                                        |
| Road crossing conduit           | Reinforced concrete encased HDPE, ≥2× cable OD                                                  | ADB project specs                                     |
| Earthing                        | Continuous earth conductor in trench, connected at each transformer and substation              | IEC 60364-5-54                                        |
| Cable entry at substation       | Sealing gland, cable duct with fireproof sealant                                                | IEC 61936-1                                           |

---

## 4. Substation Compound

| Parameter                     | Value                                                                                    | Standard / Note              |
| ----------------------------- | ---------------------------------------------------------------------------------------- | ---------------------------- |
| **Function**                  | Receives 16 × 33kV PV block feeders + BESS feeder; steps up 33kV → 66kV for transmission | Myinsai concept              |
| **Compound area**             | Min **80 m × 60 m = 4,800 m²**                                                           | IEC 61936-1 layout clearance |
| **Security fence**            | 2.4 m chain-link + 0.5 m barbed wire top, 10 m setback from HV equipment                 | IEC 61936-1                  |
| Compound access gate          | Double-leaf, 6 m wide (for transformer transport)                                        | IEC 61936-1                  |
| Floor surface inside compound | Washed crushed stone 50–75mm (drainage)                                                  | IEC 61936-1                  |
| **Control room / relay room** | Inside compound, min **30 m²**, A/C, UPS 4h backup                                       | IEA-PVPS T13                 |
| LV battery room / LVDC panel  | Separate fire-compartment within control room                                            | IEC 62271-200                |
| Drainage                      | Perimeter drain inside compound fence, oil interceptor for transformer area              | ADB Environmental            |
| Lightning protection          | Class I per IEC 62305-3, mast/rod at each corner                                         | IEC 62305                    |
| Lighting                      | Emergency lighting, min 50 lux at grade inside compound                                  | IEC 61936-1                  |
| Earthing                      | Buried earth grid, copper conductors, ≤1 Ω resistance                                    | IEC 61936-1                  |
| Signage                       | Danger signs (in local language), arc-flash labels                                       | Myanmar MOEE requirement     |

---

## 5. O&M Facility (Office & Workshop Compound)

_Sized for a 51.2 MWac plant with ~8–12 on-site staff._

| Building / Zone             | Floor Area         | Key Features                                                            |
| --------------------------- | ------------------ | ----------------------------------------------------------------------- |
| **Main O&M Office**         | 150–200 m²         | Offices, SCADA/monitoring workstations, meeting room, first aid station |
| **Maintenance Workshop**    | 200–300 m²         | Bench testing, tool storage, module cleaning equipment bay              |
| **Spare Parts Warehouse**   | 100–150 m²         | Shelved, climate-controlled, lockable. For modules, inverters, cables   |
| **Guard/Security Post**     | 20–30 m² (at gate) | 24/7 staffed, intercom, CCTV monitor                                    |
| **Toilet / Ablution Block** | 30–50 m²           | Separate structure, male/female (per local building code)               |
| **Vehicle Parking + Yard**  | 500–800 m²         | Hardstanding for pickup trucks, scissor lift, cleaning vehicle          |
| **TOTAL COMPOUND**          | **~0.8–1.0 ha**    | Includes hardstanding, landscaping, security fence                      |

### Location Rules

| Rule                  | Specification                                                                                                              |
| --------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| **Position**          | Adjacent to substation (shared security fence wall preferred) — minimizes cable runs from SCADA to substation control room |
| **Gate access**       | Directly connected to main site collector road and public road                                                             |
| **Orientation**       | Place building on north side of compound to reduce solar heat gain on office                                               |
| Flood level           | **+0.6 m above 1-in-100yr local flood level** — mandatory for Myanmar lowland sites                                        |
| Setback from PV array | Min 15 m (fire safety, glint avoidance)                                                                                    |

### Myanmar Climate-Specific Design Rules

| Adaptation           | Specification                                             |
| -------------------- | --------------------------------------------------------- |
| Roof pitch           | ≥20° for monsoon runoff (no flat roofs)                   |
| Roof material        | Colour-bond zinc-aluminium coated steel sheet             |
| Wall construction    | Reinforced concrete block or structural steel frame       |
| Lightning protection | IEC 62305 Class II on all buildings                       |
| Raised floor         | +0.6 m above natural ground                               |
| Air conditioning     | Split-type inverter AC, min COP 4.0 (tropical efficiency) |
| Solar panel shading  | Optional solar carport over parking to reduce heat island |

---

## 6. BESS Compound

| Parameter              | Value                                                                | Note                                         |
| ---------------------- | -------------------------------------------------------------------- | -------------------------------------------- |
| **Rating**             | 10 MW / 20 MWh (2-hour duration)                                     | Myinsai allocation                           |
| **Compound footprint** | ~60 m × 30 m = 1,800 m²                                              | Includes container spacing and access aisles |
| Container layout       | 3 m gap between containers (fire safety)                             | NFPA 855 / IEC 62619                         |
| Fire suppression aisle | 6 m wide at ends (fire truck access)                                 | NFPA 855                                     |
| **Position**           | Adjacent to substation (shared 33kV connection), not near O&M office | Heat and fire risk separation                |
| BESS fence             | Separate from substation compound fence, min 2 m clearance           | Fire isolation                               |
| BESS access gate       | Min 4 m wide, vehicle access                                         | O&M requirement                              |

---

## 7. Engine Implementation Rules (Code Parameters)

These are the values to hard-code or configure in `config.yaml`:

```yaml
substation:
  compound_width_m: 80
  compound_height_m: 60

om_compound:
  width_m: 100
  height_m: 50
  office_area_m2: 175
  workshop_area_m2: 250
  warehouse_area_m2: 125

bess:
  capacity_mw: 10
  capacity_mwh: 20
  compound_width_m: 60
  compound_height_m: 30
  container_gap_m: 3

roads:
  main_collector_width_m: 6
  maintenance_aisle_width_m: 4
  max_gradient_pct: 5

mv_cables:
  topology: homerun # one cable per transformer direct to substation
  voltage_kv: 33
  routing: alongside_roads # prefer road corridors over cross-country
  trench_depth_m: 1.2
```
