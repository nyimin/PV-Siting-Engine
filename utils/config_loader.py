import os
import yaml
import logging

logger = logging.getLogger("PVLayoutEngine.config")


def load_config(config_path="config/config.yaml"):
    """Loads and validates the main configuration file."""
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Configuration file not found at {config_path}")

    with open(config_path, 'r') as f:
        config = yaml.safe_load(f)

    _validate_config(config)
    return config


def _validate_config(config):
    """Validates that required configuration sections and keys exist with sane values."""
    required_sections = ["solar", "block", "terrain", "buffers"]
    for section in required_sections:
        if section not in config:
            raise ValueError(f"Missing required config section: '{section}'")

    solar = config["solar"]
    _check_positive(solar, "module_power_w", 100, 1000)
    _check_positive(solar, "modules_per_string", 10, 60)
    _check_positive(solar, "strings_per_inverter", 5, 50)
    _check_positive(solar, "inverter_capacity_kw", 50, 5000)
    _check_positive(solar, "row_pitch_m", 3.0, 20.0)
    _check_positive(solar, "tilt_deg", 0, 60)
    _check_positive(solar, "dc_ac_ratio", 1.0, 2.0)

    block = config["block"]
    _check_positive(block, "inverters_per_block", 1, 50)
    _check_positive(block, "ac_capacity_mw", 0.5, 50)
    _check_positive(block, "footprint_ha", 0.5, 50)

    terrain = config["terrain"]
    _check_positive(terrain, "max_slope_percent", 1, 30)

    logger.info("Configuration validated successfully.")


def _check_positive(section, key, min_val=None, max_val=None):
    """Validates a numeric config value exists and is within the expected range."""
    if key not in section:
        raise ValueError(f"Missing required config key: '{key}'")
    val = section[key]
    if not isinstance(val, (int, float)):
        raise ValueError(f"Config key '{key}' must be numeric, got {type(val).__name__}")
    if min_val is not None and val < min_val:
        raise ValueError(f"Config key '{key}' = {val} is below minimum {min_val}")
    if max_val is not None and val > max_val:
        raise ValueError(f"Config key '{key}' = {val} is above maximum {max_val}")


def setup_logging(log_level=logging.INFO):
    """Sets up standard logging for the application."""
    logging.basicConfig(
        level=log_level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    return logging.getLogger("PVLayoutEngine")
