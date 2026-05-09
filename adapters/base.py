"""
adapters/base.py
================
Abstract base classes for all hardware adapters.

Every concrete adapter (scale, camera, wearable) implements one of these
interfaces. The core system only ever talks to these abstractions — it has
zero knowledge of specific hardware or APIs.

Design principles:
- All methods are async (I/O bound: BLE, HTTP, file reads)
- Dataclasses for all return types (typed, serialisable, diffable)
- Optional fields everywhere except the absolute minimum
- Timestamps are always UTC, always timezone-aware
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ===========================================================================
# SCALE ADAPTER
# ===========================================================================

@dataclass
class ScaleReading:
    """
    Normalised output of any scale adapter.

    Only `recorded_at` and `weight_kg` are guaranteed.
    Every other field is Optional — the adapter sets what it can,
    leaves the rest as None. The core and UI adapt accordingly.
    """
    recorded_at: datetime
    weight_kg: float

    # Basic BIA (most consumer scales)
    body_fat_pct: Optional[float] = None
    muscle_mass_kg: Optional[float] = None
    bone_mass_kg: Optional[float] = None
    water_pct: Optional[float] = None
    visceral_fat_index: Optional[float] = None
    bmr_kcal: Optional[float] = None
    bmi: Optional[float] = None
    metabolic_age: Optional[int] = None

    # Advanced BIA (8-electrode / segmental — e.g. Withings Body Scan)
    phase_angle: Optional[float] = None
    ecm_bcm_ratio: Optional[float] = None
    muscle_mass_left_arm_kg: Optional[float] = None
    muscle_mass_right_arm_kg: Optional[float] = None
    muscle_mass_left_leg_kg: Optional[float] = None
    muscle_mass_right_leg_kg: Optional[float] = None
    muscle_mass_torso_kg: Optional[float] = None
    fat_mass_left_arm_kg: Optional[float] = None
    fat_mass_right_arm_kg: Optional[float] = None
    fat_mass_left_leg_kg: Optional[float] = None
    fat_mass_right_leg_kg: Optional[float] = None
    fat_mass_torso_kg: Optional[float] = None

    # Cardiovascular (Withings Body Scan / Body Comp)
    heart_rate_bpm: Optional[int] = None
    pulse_wave_velocity: Optional[float] = None
    vascular_age: Optional[int] = None
    nerve_health_score: Optional[float] = None

    # Overflow bucket: any metric not listed above
    # e.g. {"cardiac_output_l_min": 5.2, "icg_score": 78}
    extras: dict[str, float] = field(default_factory=dict)

    # Adapter metadata
    adapter_name: str = "unknown"
    raw_payload: Optional[dict] = None  # original API/BLE response for debugging

    def available_metrics(self) -> list[str]:
        """Return names of all non-None fields (excluding metadata)."""
        skip = {"recorded_at", "weight_kg", "extras", "adapter_name", "raw_payload"}
        fields = [
            f for f, v in self.__dict__.items()
            if f not in skip and v is not None
        ]
        if self.extras:
            fields += list(self.extras.keys())
        return fields


class ScaleAdapter(abc.ABC):
    """
    Interface every scale adapter must implement.

    Lifecycle:
        adapter = MyScaleAdapter(config)
        await adapter.connect()
        reading = await adapter.read()
        await adapter.disconnect()

    Or use as async context manager:
        async with MyScaleAdapter(config) as adapter:
            reading = await adapter.read()
    """

    def __init__(self, config: dict):
        self.config = config

    @abc.abstractmethod
    async def connect(self) -> None:
        """Establish connection (BLE scan, HTTP auth, file open, …)."""

    @abc.abstractmethod
    async def read(self) -> ScaleReading:
        """
        Block until a stable reading is available, then return it.
        Fires the camera trigger event before returning.
        """

    @abc.abstractmethod
    async def disconnect(self) -> None:
        """Clean up connection."""

    @abc.abstractmethod
    def is_available(self) -> bool:
        """Quick sync check — is the hardware reachable right now?"""

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *_):
        await self.disconnect()


# ===========================================================================
# CAMERA ADAPTER
# ===========================================================================

@dataclass
class CapturedFrame:
    """Single captured image with metadata."""
    captured_at: datetime
    image_path: Path          # saved to data/photos/
    angle: str = "front"      # "front" | "side_left" | "side_right" | "back"
    width_px: Optional[int] = None
    height_px: Optional[int] = None
    depth_map_path: Optional[Path] = None   # RealSense / depth cameras only
    adapter_name: str = "unknown"


class CameraAdapter(abc.ABC):
    """
    Interface every camera adapter must implement.

    The trigger mechanism: ScaleAdapter calls `trigger_event` on the
    CameraAdapter when a stable weight reading is detected. The camera
    adapter then captures immediately (after optional stabilisation delay).
    """

    def __init__(self, config: dict):
        self.config = config
        self._output_dir = Path(config.get("photos_path", "data/photos"))
        self._stabilisation_delay_s: float = config.get("stabilisation_delay_s", 3.0)

    @abc.abstractmethod
    async def open(self) -> None:
        """Initialise camera device."""

    @abc.abstractmethod
    async def capture(self, angle: str = "front") -> CapturedFrame:
        """Capture a single frame and save it. Return metadata."""

    @abc.abstractmethod
    async def close(self) -> None:
        """Release camera device."""

    @abc.abstractmethod
    def is_available(self) -> bool:
        """Is the camera device accessible?"""

    async def on_scale_trigger(self, reading: ScaleReading) -> CapturedFrame:
        """
        Called by the scheduler when the scale fires a stable reading.
        Waits for stabilisation_delay_s, then captures.
        Override if you need custom trigger logic.
        """
        import asyncio
        if self._stabilisation_delay_s > 0:
            await asyncio.sleep(self._stabilisation_delay_s)
        return await self.capture()

    async def __aenter__(self):
        await self.open()
        return self

    async def __aexit__(self, *_):
        await self.close()


# ===========================================================================
# WEARABLE / CONTEXT ADAPTER
# ===========================================================================

@dataclass
class ContextReading:
    """
    Contextual health data from wearables/apps for a given date.

    This enriches the daily session with background health metrics —
    sleep quality, recovery, activity — that help the AI interpret
    the scale data in context.
    """
    date: datetime           # date this data refers to (UTC midnight)
    source: str              # "oura" | "garmin" | "fitbit" | "apple_health" | …

    # Sleep
    sleep_score: Optional[int] = None        # 0–100
    sleep_duration_h: Optional[float] = None
    sleep_efficiency_pct: Optional[float] = None
    deep_sleep_h: Optional[float] = None
    rem_sleep_h: Optional[float] = None
    sleep_latency_min: Optional[int] = None

    # Recovery / autonomic
    hrv_ms: Optional[float] = None           # heart rate variability
    resting_hr_bpm: Optional[int] = None
    readiness_score: Optional[int] = None    # 0–100 (Oura, WHOOP, Garmin)
    body_temperature_delta: Optional[float] = None  # vs baseline

    # Activity
    steps: Optional[int] = None
    active_calories_kcal: Optional[int] = None
    total_calories_kcal: Optional[int] = None
    active_minutes: Optional[int] = None
    vo2_max: Optional[float] = None
    training_load: Optional[float] = None    # Garmin / WHOOP strain

    # Overflow bucket
    extras: dict[str, float] = field(default_factory=dict)

    adapter_name: str = "unknown"
    raw_payload: Optional[dict] = None

    def available_metrics(self) -> list[str]:
        skip = {"date", "source", "extras", "adapter_name", "raw_payload"}
        fields = [
            f for f, v in self.__dict__.items()
            if f not in skip and v is not None
        ]
        if self.extras:
            fields += list(self.extras.keys())
        return fields


class WearableAdapter(abc.ABC):
    """
    Interface every wearable/app adapter must implement.

    Unlike scale and camera adapters, wearable adapters are NOT triggered
    in real-time. They are called once per day by the scheduler to fetch
    the previous day's context data.
    """

    def __init__(self, config: dict):
        self.config = config

    @abc.abstractmethod
    async def authenticate(self) -> None:
        """Perform auth (OAuth token refresh, API key check, …)."""

    @abc.abstractmethod
    async def fetch_context(self, date: datetime) -> ContextReading:
        """
        Fetch health context for `date` (UTC).
        Returns a ContextReading with whatever the source provides.
        """

    @abc.abstractmethod
    def is_available(self) -> bool:
        """Is this adapter configured and reachable?"""

    async def __aenter__(self):
        await self.authenticate()
        return self

    async def __aexit__(self, *_):
        pass  # stateless HTTP — nothing to close
