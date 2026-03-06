import logging
import requests

logger = logging.getLogger("PVLayoutEngine.yield")

def calculate_yield(lat, lon, dc_capacity_mw, config):
    """
    Calls the NREL PVWatts V8 API to calculate annual energy yield (MWh).
    Computes P50 and estimates P90 sensitivity.
    
    Returns:
        p50_yield_mwh (float): P50 annual energy yield
        p90_yield_mwh (float): P90 estimate (approx 90-95% of P50 depending on variability)
        specific_yield_kwh_kwp (float): Specific yield
        used_api (bool): True if API was successful, False if fallback was used
    """
    if dc_capacity_mw <= 0:
        return 0.0, 0.0, 0.0, False

    dc_capacity_kw = dc_capacity_mw * 1000.0

    # Read base parameters
    pv_config = config.get("solar", {})
    tilt = pv_config.get("tilt_deg", 20)
    azimuth = pv_config.get("azimuth_deg", 180)
    system_losses = pv_config.get("system_losses_percent", 14.0)

    # PVWatts V8 API endpoint
    # https://developer.nrel.gov/docs/solar/pvwatts/v8/
    url = "https://developer.nrel.gov/api/pvwatts/v8.json"
    
    api_key = config.get("api", {}).get("nrel_pvwatts", "DEMO_KEY")

    params = {
        "api_key": api_key,
        "lat": lat,
        "lon": lon,
        "system_capacity": dc_capacity_kw,
        "azimuth": azimuth,
        "tilt": tilt,
        "array_type": 1,         # 0=Fixed Open Rack, 1=Fixed Roof, 2=1-Axis, 3=1-Axis back, 4=2-Axis. Let's use 0 for utility scale
        "module_type": 0,        # 0=Standard, 1=Premium, 2=Thin film
        "losses": system_losses,
        "dataset": "nsrdb",
    }
    # Fix array_type for utility scale (Fixed Open Rack)
    params["array_type"] = 0

    p50_yield_mwh = 0.0
    used_api = False

    try:
        response = requests.get(url, params=params, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if "outputs" in data and "ac_annual" in data["outputs"]:
                # PVWatts returns AC annual energy in kWh
                ac_annual_kwh = data["outputs"]["ac_annual"]
                p50_yield_mwh = ac_annual_kwh / 1000.0
                used_api = True
                logger.info("  PVWatts yield calculated successfully.")
        else:
            logger.warning(f"PVWatts API error {response.status_code}: {response.text}")
    except Exception as e:
        logger.warning(f"PVWatts connection failed: {e}")

    if not used_api:
        logger.info("  Falling back to latitude-based yield proxy.")
        proxy_hours = 1800 if abs(lat) < 30 else 1200
        p50_yield_mwh = dc_capacity_mw * proxy_hours

    specific_yield = (p50_yield_mwh * 1000.0) / dc_capacity_kw if dc_capacity_kw > 0 else 0.0
    
    # Rough P90 estimate (-8% from P50 is typical for solar resource interannual variability)
    p90_yield_mwh = p50_yield_mwh * 0.92

    return p50_yield_mwh, p90_yield_mwh, specific_yield, used_api
