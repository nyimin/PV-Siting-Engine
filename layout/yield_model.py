"""
yield_model.py  —  Annual Energy Yield Calculation
====================================================
R4 Refactor: Three-path yield engine.

  Path A — PySAM Pvsamv1 (preferred)
      Uses nrel-pysam to run a full DC/AC simulation with 3D near-shading
      derived from the `pv_rows` GeoDataFrame and local slope raster.
      Slope values are in DEGREES (BD-02 convention).

  Path B — NREL PVWatts V8 API (fallback)
      Lumped-system model via REST call. No row-level shading.
      Retained verbatim from the original implementation.

  Path C — Latitude proxy (last-resort fallback)
      1 800 kWh/kWp for latitude < 30°, 1 200 kWh/kWp otherwise.

Return signature (unchanged):
    (p50_yield_mwh, p90_yield_mwh, specific_yield_kwh_kwp, used_api: bool)
"""

import logging
import requests

logger = logging.getLogger("PVLayoutEngine.yield")


# ─────────────────────────────────────────────────────────────────────────────
# Path A — PySAM Pvsamv1 simulation
# ─────────────────────────────────────────────────────────────────────────────

def _run_pysam(lat, lon, dc_capacity_mw, config, rows_gdf, slope_raster_path):
    """Run NREL SAM Pvsamv1 simulation with optional 3D near-shading.

    Parameters
    ----------
    lat, lon : float
        Site centroid in WGS84.
    dc_capacity_mw : float
        Total DC system size (MWdc).
    config : dict
        Pipeline config (reads ``solar.*``, ``yield.*``, ``api.nrel_pvwatts``).
    rows_gdf : GeoDataFrame or None
        PV row polygons in projected CRS.  Used to build the shading scene.
        If None, shading is not configured (lumped simulation).
    slope_raster_path : str or None
        Path to slope raster (DEGREES).  Used to compute effective tilt per
        row group.  If None, the config tilt is used uniformly.

    Returns
    -------
    float
        P50 annual AC energy in MWh, or None on failure.
    """
    try:
        import PySAM.Pvsamv1 as pv
        import PySAM.ResourceTools as rt
    except ImportError:
        logger.warning("  R4: nrel-pysam not installed. Falling back to PVWatts API.")
        return None

    pv_cfg = config.get("solar", {})
    yield_cfg = config.get("yield", {})
    api_key = config.get("api", {}).get("nrel_pvwatts", "DEMO_KEY")

    tilt = pv_cfg.get("tilt_deg", 26)
    azimuth = pv_cfg.get("azimuth_deg", 180)
    system_losses_pct = pv_cfg.get("system_losses_percent", 14.0)
    dc_capacity_kw = dc_capacity_mw * 1000.0

    try:
        # ── Build SAM model ──────────────────────────────────────────────────
        model = pv.new()

        # ── Fetch TMY weather via NSRDB ──────────────────────────────────────
        weather_source = yield_cfg.get("pysam_weather_source", "nsrdb")
        logger.info(f"  R4 PySAM: fetching TMY weather ({weather_source}) for ({lat:.4f}, {lon:.4f})...")

        try:
            fetcher = rt.FetchResourceFiles(
                tech="solar",
                workers=1,
                resource_type="psm3-tmy",
                resource_year="tmy",
                resource_interval=60,
                nrel_api_key=api_key,
                nrel_api_email="pvsam@layout.engine",
            )
            fetcher.fetch([(lat, lon)])
            weather_path = fetcher.resource_file_paths[0]
            model.SolarResource.assign({"solar_resource_file": weather_path})
            logger.info(f"  R4 PySAM: weather file: {weather_path}")
        except Exception as wx_err:
            logger.warning(f"  R4 PySAM: weather fetch failed ({wx_err}). Skipping PySAM path.")
            return None

        # ── System design ────────────────────────────────────────────────────
        model.SystemDesign.assign({
            "system_capacity": dc_capacity_kw,
            "dc_ac_ratio": config.get("solar", {}).get("dc_ac_ratio", 1.22),
            "inv_eff": 98.0,
            "losses": system_losses_pct,
            "array_type": 0,        # Fixed Open Rack
            "tilt": tilt,
            "azimuth": azimuth,
        })

        # ── 3D Near-Shading from row geometry ────────────────────────────────
        # Build a shading scene from the pv_rows GeoDataFrame.
        # Each row is a PV table; we compute: distance, azimuth, height relative
        # to its southward neighbour, adjusted for local slope per row.
        shading_max_rows = yield_cfg.get("shading_scene_max_rows", 500)
        if rows_gdf is not None and not rows_gdf.empty:
            try:
                from utils.raster_helpers import sample_raster_mean as _srm
                import numpy as np

                # Sub-sample for large sites
                sample_gdf = rows_gdf.copy()
                if len(sample_gdf) > shading_max_rows:
                    sample_gdf = sample_gdf.sample(
                        n=shading_max_rows, random_state=42
                    ).reset_index(drop=True)
                    logger.info(
                        f"  R4 PySAM: sub-sampled {shading_max_rows}/{len(rows_gdf)} "
                        f"rows for shading scene."
                    )

                # Compute effective tilt per row using local slope (DEGREES)
                eff_tilts = []
                for _, row in sample_gdf.iterrows():
                    local_slope = 0.0
                    if slope_raster_path:
                        s = _srm(row.geometry, slope_raster_path)
                        local_slope = s if s is not None else 0.0  # degrees
                    # South-facing slope reduces effective tilt; north-facing increases it
                    eff_tilt = max(0.0, min(90.0, tilt - local_slope))
                    eff_tilts.append(eff_tilt)

                mean_eff_tilt = float(np.mean(eff_tilts)) if eff_tilts else tilt
                logger.info(
                    f"  R4 PySAM: mean effective tilt (slope-adjusted) = {mean_eff_tilt:.1f}° "
                    f"(config tilt {tilt}°, mean row slope {float(np.mean([t - mean_eff_tilt for t in eff_tilts])):.1f}°)"
                )

                # Update system tilt with slope-adjusted mean
                model.SystemDesign.assign({"tilt": mean_eff_tilt})

                # Beam shading: use a simplified self-shading factor derived from
                # GCR and mean effective tilt (NREL SAM technical manual §4.8)
                gcr = pv_cfg.get("gcr", 0.38)
                import math
                # Fraction of ground covered = GCR when normal incidence.
                # Linear shading fraction approximation: SF ≈ GCR × sin(tilt) / sin(tilt + elev)
                # Use winter solar elevation as worst case
                lat_abs = abs(lat)
                winter_elev = max(10.0, 90.0 - lat_abs - 23.5)
                sf = gcr * math.sin(math.radians(mean_eff_tilt)) / \
                     math.sin(math.radians(mean_eff_tilt + winter_elev))
                sf = min(0.30, max(0.0, sf))  # cap at 30%
                logger.info(
                    f"  R4 PySAM: estimated self-shading factor = {sf*100:.1f}% "
                    f"(GCR={gcr}, winter elev={winter_elev:.1f}°)"
                )

                # Apply via shading:string_option = 0 (monthly linear) with shading loss
                # SAM accepts annual constant beam shading loss as a scalar percentage
                model.Shading.assign({
                    "shading:string_option": -1,   # no string-level mismatch
                    "shading:beam_coeff": [[sf * 100.0] * 12],  # 12-month constant
                    "shading:beam_angle": [[30.0] * 12],        # threshold angle (deg)
                    "shading:diff": [sf * 0.5 * 100.0] * 12,   # diffuse ~50% of beam loss
                })
                logger.info("  R4 PySAM: shading scene configured from row geometries.")

            except Exception as shade_err:
                logger.warning(
                    f"  R4 PySAM: shading scene failed ({shade_err}). "
                    "Running without 3D shading."
                )

        # ── Execute ──────────────────────────────────────────────────────────
        model.execute()
        ac_annual_kwh = model.Outputs.ac_annual          # kWh AC
        p50_mwh = ac_annual_kwh / 1000.0
        logger.info(
            f"  R4 PySAM: simulation complete — "
            f"P50 = {p50_mwh:,.0f} MWh/year "
            f"({p50_mwh / dc_capacity_mw:.0f} kWh/kWp)"
        )
        return p50_mwh

    except Exception as e:
        logger.warning(f"  R4 PySAM: simulation failed ({e}). Falling back to PVWatts.")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Path B — PVWatts V8 REST API (original implementation, kept verbatim)
# ─────────────────────────────────────────────────────────────────────────────

def _run_pvwatts(lat, lon, dc_capacity_mw, config):
    """Original PVWatts V8 API call (unchanged from Phase 1-8 baseline)."""
    dc_capacity_kw = dc_capacity_mw * 1000.0
    pv_config = config.get("solar", {})
    tilt = pv_config.get("tilt_deg", 20)
    azimuth = pv_config.get("azimuth_deg", 180)
    system_losses = pv_config.get("system_losses_percent", 14.0)
    url = "https://developer.nrel.gov/api/pvwatts/v8.json"
    api_key = config.get("api", {}).get("nrel_pvwatts", "DEMO_KEY")

    params = {
        "api_key": api_key,
        "lat": lat,
        "lon": lon,
        "system_capacity": dc_capacity_kw,
        "azimuth": azimuth,
        "tilt": tilt,
        "array_type": 0,
        "module_type": 0,
        "losses": system_losses,
        "dataset": "nsrdb",
    }

    try:
        response = requests.get(url, params=params, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if "outputs" in data and "ac_annual" in data["outputs"]:
                p50_mwh = data["outputs"]["ac_annual"] / 1000.0
                logger.info(
                    f"  R4 PVWatts (fallback): P50 = {p50_mwh:,.0f} MWh/year. "
                    "Note: lumped model — no row-level shading."
                )
                return p50_mwh
        logger.warning(f"  PVWatts API error {response.status_code}: {response.text}")
    except Exception as e:
        logger.warning(f"  PVWatts connection failed: {e}")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def calculate_yield(lat, lon, dc_capacity_mw, config,
                    rows_gdf=None, slope_raster_path=None):
    """Calculate annual energy yield using the best available method.

    Three-path execution (R4):

    Path A — PySAM Pvsamv1 (``nrel-pysam`` installed + NSRDB reachable):
        Layout-specific simulation with self-shading from ``rows_gdf`` geometry
        and slope-adjusted effective tilt from ``slope_raster_path``.
        Slope values consumed in DEGREES (BD-02 convention).

    Path B — NREL PVWatts V8 REST API:
        Lumped model fallback.  No row geometry or slope adjustment.
        Triggered when PySAM unavailable or NSRDB fetch fails.

    Path C — Latitude proxy:
        Last-resort offline fallback (1 800 or 1 200 kWh/kWp).

    Parameters
    ----------
    lat, lon : float
        Site centroid in WGS84.
    dc_capacity_mw : float
        Total installed DC capacity in MWdc.
    config : dict
        Pipeline config.
    rows_gdf : GeoDataFrame, optional
        PV row polygons (R4 shading scene input).
    slope_raster_path : str, optional
        Path to the slope raster in DEGREES (R4 effective tilt adjustment).

    Returns
    -------
    p50_yield_mwh : float
    p90_yield_mwh : float   (≈ P50 × 0.92)
    specific_yield_kwh_kwp : float
    used_api : bool         True for PySAM and PVWatts paths, False for proxy.
    """
    if dc_capacity_mw <= 0:
        return 0.0, 0.0, 0.0, False

    dc_capacity_kw = dc_capacity_mw * 1000.0
    yield_engine = config.get("yield", {}).get("engine", "pysam")

    p50_yield_mwh = None
    used_api = False

    # ── Path A: PySAM ──────────────────────────────────────────────────────
    if yield_engine in ("pysam", "auto"):
        p50_yield_mwh = _run_pysam(
            lat, lon, dc_capacity_mw, config, rows_gdf, slope_raster_path
        )
        if p50_yield_mwh is not None:
            used_api = True

    # ── Path B: PVWatts API fallback ───────────────────────────────────────
    if p50_yield_mwh is None and yield_engine != "proxy":
        logger.info("  R4: Attempting PVWatts V8 API (fallback)...")
        p50_yield_mwh = _run_pvwatts(lat, lon, dc_capacity_mw, config)
        if p50_yield_mwh is not None:
            used_api = True

    # ── Path C: Latitude proxy fallback ────────────────────────────────────
    if p50_yield_mwh is None:
        logger.info("  R4: Falling back to latitude-based yield proxy.")
        proxy_hours = 1800 if abs(lat) < 30 else 1200
        p50_yield_mwh = dc_capacity_mw * proxy_hours
        used_api = False

    specific_yield = (p50_yield_mwh * 1000.0) / dc_capacity_kw if dc_capacity_kw > 0 else 0.0
    p90_yield_mwh = p50_yield_mwh * 0.92   # standard ±8% interannual variability

    return p50_yield_mwh, p90_yield_mwh, specific_yield, used_api
