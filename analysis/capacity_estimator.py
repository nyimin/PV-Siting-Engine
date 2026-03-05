import logging
import math

logger = logging.getLogger("PVLayoutEngine.analysis")


def calculate_feasible_capacity(buildable_area_ha, required_mw_dc, config):
    """
    Estimates feasible capacity using block-based architecture.

    Instead of a simple area/factor calculation, this computes:
    - How many 3.2 MWac blocks fit in the buildable area
    - Total AC and DC capacity from block count
    """
    logger.info("Calculating feasible solar capacity (block-based)...")

    block_cfg = config.get("block", {})
    solar_cfg = config.get("solar", {})

    ac_per_block = block_cfg.get("ac_capacity_mw", 3.2)
    dc_per_block = block_cfg.get("dc_capacity_mw", 3.904)
    footprint_ha = block_cfg.get("footprint_ha", 2.5)
    dc_ac_ratio = solar_cfg.get("dc_ac_ratio", 1.22)

    # Maximum number of blocks that fit (with 10% allowance for roads/gaps)
    usable_area_ha = buildable_area_ha * 0.85  # 15% for roads, gaps, BoP
    max_blocks = int(usable_area_ha / footprint_ha)

    max_feasible_ac_mw = max_blocks * ac_per_block
    max_feasible_dc_mw = max_blocks * dc_per_block

    # Convert requested DC to AC for comparison
    required_ac_mw = required_mw_dc / dc_ac_ratio

    logger.info(f"  Buildable area: {buildable_area_ha:.2f} ha")
    logger.info(f"  Usable area (85%): {usable_area_ha:.2f} ha")
    logger.info(f"  Max blocks: {max_blocks} × {footprint_ha} ha")
    logger.info(f"  Max feasible: {max_feasible_ac_mw:.1f} MWac / {max_feasible_dc_mw:.1f} MWdc")
    logger.info(f"  Requested: {required_mw_dc:.1f} MWdc ({required_ac_mw:.1f} MWac)")

    is_feasible = max_feasible_dc_mw >= required_mw_dc

    if is_feasible:
        logger.info("  ✓ Site is FEASIBLE for the requested capacity.")
    else:
        logger.warning(f"  ✗ Site CANNOT support {required_mw_dc} MWdc.")
        logger.warning(f"  Shortfall: {required_mw_dc - max_feasible_dc_mw:.2f} MWdc")

    return {
        "required_mw_dc": required_mw_dc,
        "required_mw_ac": round(required_ac_mw, 2),
        "max_feasible_ac_mw": round(max_feasible_ac_mw, 2),
        "max_feasible_dc_mw": round(max_feasible_dc_mw, 2),
        "max_blocks": max_blocks,
        "is_feasible": is_feasible,
        "buildable_area_ha": round(buildable_area_ha, 2),
        "usable_area_ha": round(usable_area_ha, 2),
        "footprint_ha_per_block": footprint_ha,
        "dc_ac_ratio": dc_ac_ratio,
    }
