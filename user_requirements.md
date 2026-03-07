# User Requirements: Myinsai Solar Project

Based on the **Concept_Design_130MW_Magway_Solar.pdf**, the PV Layout Engine must be constrained to generate **only** the specified capacity for the Myinsai site, rather than filling the entire project boundary.

## 1. Project Capacity Targets (Myinsai Specifically)

- **Target AC Capacity:** 51.2 MWac
- **Target DC Capacity:** 62.6 MWp
- **Total PV Blocks:** Exactly 16 blocks
- **Total PV Modules:** 98,560
- **Total Strings:** 3,520

## 2. Standardized PV Block Design (3.2 MWac Unit)

The layout engine is currently aligned with these physical parameters, but they must be strictly enforced for exactly 16 blocks:

- **Inverters per Block:** 10 × SG320HX (320 kW)
- **Block AC Rating:** 3.2 MWac
- **Block DC Rating:** 3.912 MWp
- **DC/AC Ratio (ILR):** 1.22
- **Strings per Block:** 220
- **Modules per Block:** 6,160 (28 modules per string)
- **Block Transformer:** 1 × 3.15 MVA (800 V / 33 kV)
- **Approx. Footprint:** ~2.5 hectares per block

## 3. Module & Mounting Specifications

- **Module:** 635 Wp Bifacial Monocrystalline (2384 x 1303 x 35 mm)
- **Mounting:** Fixed-Tilt
- **Orientation:** Portrait (2-high or 1-high depending on structure design, typically 2P for portrait). The design implies single row of 28 modules per string.
- **Tilt Angle:** 26°
- **Azimuth:** 180° (South)
- **Row Spacing (Pitch):** 6.5 - 7.0 m
- **Ground Clearance:** 1.5 m minimum

## 4. BESS (Battery Energy Storage System)

- **Myinsai Allocation:** 10 MW Power / 20 MWh Energy
- **Duration:** 2 hours
- **Location:** Adjacent to the Myinsai substation

## 5. Substation & Routing

- **Collection Voltage:** 33 kV
- **Transmission Voltage:** 66 kV (Myinsai steps up 33 kV to 66 kV to transmit to Letpantaw hub).
- **Infrastructure needed:** Substation must accommodate the 16 incoming 33kV PV block feeders and the BESS feeder, plus the step-up transformer to 66 kV.
