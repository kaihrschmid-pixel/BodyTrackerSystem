"""
adapters/registry.py
====================
Adapter registry — maps string names from config.yaml to adapter classes.

This is the plugin system. Adding a new adapter = adding one line here.
No changes needed anywhere else in the codebase.

Usage (from core/scheduler.py):
    from adapters.registry import get_scale_adapter, get_camera_adapter, get_wearable_adapters

    scale = get_scale_adapter(config["hardware"]["scale"])
    camera = get_camera_adapter(config["hardware"]["camera"])
    wearables = get_wearable_adapters(config.get("wearables", {}))
"""

from __future__ import annotations

import logging
from typing import Type

from adapters.base import ScaleAdapter, CameraAdapter, WearableAdapter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Scale adapters
# ---------------------------------------------------------------------------
# Import lazily to avoid pulling in BLE/HTTP deps when not configured

_SCALE_REGISTRY: dict[str, str] = {
    "manual":   "adapters.scales.manual:ManualAdapter",
    "withings": "adapters.scales.withings:WithingsScaleAdapter",
    "ble":      "adapters.scales.ble:BLEScaleAdapter",
}

# ---------------------------------------------------------------------------
# Camera adapters
# ---------------------------------------------------------------------------

_CAMERA_REGISTRY: dict[str, str] = {
    "v4l2":       "adapters.cameras.v4l2:V4L2CameraAdapter",
    "picamera2":  "adapters.cameras.picamera2:PiCamera2Adapter",
    "rtsp":       "adapters.cameras.rtsp:RTSPCameraAdapter",
    "realsense":  "adapters.cameras.realsense:RealSenseCameraAdapter",
    "mock":       "adapters.cameras.mock:MockCameraAdapter",
}

# ---------------------------------------------------------------------------
# Wearable adapters
# ---------------------------------------------------------------------------

_WEARABLE_REGISTRY: dict[str, str] = {
    "oura":          "adapters.wearables.oura:OuraAdapter",
    "garmin":        "adapters.wearables.garmin:GarminAdapter",
    "fitbit":        "adapters.wearables.fitbit:FitbitAdapter",
    "withings":      "adapters.wearables.withings_health:WithingsHealthAdapter",
    "apple_health":  "adapters.wearables.apple_health:AppleHealthAdapter",
    "whoop":         "adapters.wearables.whoop:WhoopAdapter",
    "open_wearables":"adapters.wearables.open_wearables:OpenWearablesAdapter",
    "csv":           "adapters.wearables.csv_import:CSVWearableAdapter",
}


# ---------------------------------------------------------------------------
# Generic loader
# ---------------------------------------------------------------------------

def _load_class(dotted_path: str) -> type:
    """Import 'some.module:ClassName' and return the class."""
    module_path, class_name = dotted_path.split(":")
    import importlib
    module = importlib.import_module(module_path)
    return getattr(module, class_name)


def get_scale_adapter(scale_config: dict) -> ScaleAdapter:
    """
    Instantiate the configured scale adapter.

    scale_config example:
        adapter: "manual"
        mode: "interactive"
    """
    name = scale_config.get("adapter", "manual").lower()
    if name not in _SCALE_REGISTRY:
        raise ValueError(
            f"Unknown scale adapter '{name}'. "
            f"Available: {list(_SCALE_REGISTRY.keys())}"
        )
    cls = _load_class(_SCALE_REGISTRY[name])
    logger.info("Using scale adapter: %s", name)
    return cls(scale_config)


def get_camera_adapter(camera_config: dict) -> CameraAdapter:
    """
    Instantiate the configured camera adapter.

    camera_config example:
        adapter: "v4l2"
        device: "/dev/video0"
        stabilisation_delay_s: 3
    """
    name = camera_config.get("adapter", "mock").lower()
    if name not in _CAMERA_REGISTRY:
        raise ValueError(
            f"Unknown camera adapter '{name}'. "
            f"Available: {list(_CAMERA_REGISTRY.keys())}"
        )
    cls = _load_class(_CAMERA_REGISTRY[name])
    logger.info("Using camera adapter: %s", name)
    return cls(camera_config)


def get_wearable_adapters(wearables_config: dict) -> list[WearableAdapter]:
    """
    Instantiate all enabled wearable adapters.

    wearables_config example:
        oura:
          enabled: true
          personal_access_token: "abc123"
        garmin:
          enabled: false
        fitbit:
          enabled: true
          client_id: "..."
          client_secret: "..."

    Returns only adapters that are enabled and registered.
    """
    adapters = []
    for name, cfg in wearables_config.items():
        if not isinstance(cfg, dict) or not cfg.get("enabled", False):
            continue
        if name not in _WEARABLE_REGISTRY:
            logger.warning("Unknown wearable adapter '%s' — skipping", name)
            continue
        try:
            cls = _load_class(_WEARABLE_REGISTRY[name])
            adapters.append(cls(cfg))
            logger.info("Wearable adapter enabled: %s", name)
        except Exception as exc:
            logger.error("Failed to load wearable adapter '%s': %s", name, exc)
    return adapters


def list_available_adapters() -> dict[str, list[str]]:
    """Utility: return all registered adapter names by category."""
    return {
        "scales":   list(_SCALE_REGISTRY.keys()),
        "cameras":  list(_CAMERA_REGISTRY.keys()),
        "wearables": list(_WEARABLE_REGISTRY.keys()),
    }
