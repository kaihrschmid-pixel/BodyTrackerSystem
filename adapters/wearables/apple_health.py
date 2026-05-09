"""
adapters/wearables/apple_health.py
====================================
Apple Health XML export adapter — no API, no account, fully offline.

Reads the `export.xml` file from an Apple Health data export and extracts
daily summaries for sleep, heart rate, HRV, steps and active energy.

How to export from iPhone:
  1. Open the Health app
  2. Tap your profile picture (top right)
  3. Tap "Export All Health Data"
  4. AirDrop / cable-copy the resulting `export.zip` to your server
  5. Unzip: `unzip export.zip` → creates `apple_health_export/export.xml`

The XML can be large (several hundred MB for years of data). This adapter
parses it lazily using `xml.etree.ElementTree.iterparse` so memory usage
stays bounded regardless of file size.

Supported record types:
  HKQuantityTypeIdentifierStepCount             → steps
  HKQuantityTypeIdentifierActiveEnergyBurned    → active_calories_kcal
  HKQuantityTypeIdentifierBasalEnergyBurned     → total_calories_kcal (basal)
  HKQuantityTypeIdentifierHeartRate             → resting_hr_bpm (daily min)
  HKQuantityTypeIdentifierHeartRateVariabilitySDNN → hrv_ms (daily avg)
  HKQuantityTypeIdentifierVO2Max                → vo2_max
  HKCategoryTypeIdentifierSleepAnalysis         → sleep_duration_h, sleep stages
  HKQuantityTypeIdentifierBodyTemperature       → body_temperature_delta (delta)

Configuration:
    wearables:
      apple_health:
        enabled: true
        export_path: "data/apple_health_export/export.xml"
        # Rebuild the daily index only when the file is newer than the cache:
        cache_path: "data/apple_health_cache.json"

Usage:
    async with AppleHealthAdapter(config["wearables"]["apple_health"]) as adapter:
        context = await adapter.fetch_context(date=datetime.now(timezone.utc))
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from adapters.base import ContextReading, WearableAdapter, utcnow

logger = logging.getLogger(__name__)

# Apple Health record type → internal key
_RECORD_MAP = {
    "HKQuantityTypeIdentifierStepCount":             "steps",
    "HKQuantityTypeIdentifierActiveEnergyBurned":    "active_kcal",
    "HKQuantityTypeIdentifierBasalEnergyBurned":     "basal_kcal",
    "HKQuantityTypeIdentifierHeartRate":             "hr",
    "HKQuantityTypeIdentifierHeartRateVariabilitySDNN": "hrv",
    "HKQuantityTypeIdentifierVO2Max":                "vo2_max",
    "HKQuantityTypeIdentifierBodyTemperature":       "body_temp",
}

# Sleep analysis values (HKCategoryTypeIdentifierSleepAnalysis)
_SLEEP_INBED    = "HKCategoryValueSleepAnalysisInBed"
_SLEEP_ASLEEP   = "HKCategoryValueSleepAnalysisAsleep"
_SLEEP_DEEP     = "HKCategoryValueSleepAnalysisAsleepDeep"
_SLEEP_REM      = "HKCategoryValueSleepAnalysisAsleepREM"
_SLEEP_CORE     = "HKCategoryValueSleepAnalysisAsleepCore"


class AppleHealthAdapter(WearableAdapter):
    """
    Reads Apple Health export XML and provides daily context summaries.

    The first call to authenticate() parses the entire XML into a
    per-day cache (JSON). Subsequent calls use the cache if it's
    newer than the export file, so only changed files are re-parsed.
    """

    ADAPTER_NAME = "apple_health"

    def __init__(self, config: dict):
        super().__init__(config)
        self._export_path: Optional[Path] = (
            Path(config["export_path"]) if config.get("export_path") else None
        )
        self._cache_path: Optional[Path] = (
            Path(config["cache_path"]) if config.get("cache_path") else None
        )
        # date string (YYYY-MM-DD) → summary dict, populated in authenticate()
        self._daily: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # WearableAdapter interface
    # ------------------------------------------------------------------

    async def authenticate(self) -> None:
        """Parse export XML (or load cache) to build daily summary index."""
        if not self._export_path:
            raise ValueError(
                "apple_health adapter requires export_path in config.\n"
                "Export from iPhone: Health app → profile → Export All Health Data"
            )
        if not self._export_path.exists():
            raise FileNotFoundError(
                f"Apple Health export not found: {self._export_path}\n"
                "Export from iPhone: Health app → profile → Export All Health Data"
            )

        # Try loading from cache first
        if self._cache_path and self._cache_path.exists():
            cache_mtime = self._cache_path.stat().st_mtime
            export_mtime = self._export_path.stat().st_mtime
            if cache_mtime >= export_mtime:
                try:
                    self._daily = json.loads(self._cache_path.read_text())
                    logger.info(
                        "Apple Health: loaded %d days from cache %s",
                        len(self._daily), self._cache_path,
                    )
                    return
                except (json.JSONDecodeError, OSError):
                    logger.warning("Apple Health cache invalid — re-parsing")

        # Parse in executor (CPU-bound, potentially slow for large exports)
        loop = asyncio.get_event_loop()
        self._daily = await loop.run_in_executor(
            None, self._parse_export, self._export_path
        )

        # Write cache
        if self._cache_path:
            try:
                self._cache_path.parent.mkdir(parents=True, exist_ok=True)
                self._cache_path.write_text(json.dumps(self._daily))
                logger.debug("Apple Health cache written: %s", self._cache_path)
            except OSError as exc:
                logger.warning("Could not write Apple Health cache: %s", exc)

        logger.info(
            "Apple Health: parsed %d days from %s",
            len(self._daily), self._export_path,
        )

    async def fetch_context(self, date: datetime) -> ContextReading:
        """Return ContextReading for the given date."""
        date_key = date.strftime("%Y-%m-%d")
        summary = self._daily.get(date_key, {})

        reading = ContextReading(
            date=date.replace(hour=0, minute=0, second=0, microsecond=0),
            source="apple_health",
            adapter_name=self.ADAPTER_NAME,
        )

        if not summary:
            logger.debug("Apple Health: no data for %s", date_key)
            return reading

        reading.steps = _safe_int(summary.get("steps_sum"))
        reading.active_calories_kcal = _safe_int(summary.get("active_kcal_sum"))
        basal = summary.get("basal_kcal_sum")
        active = summary.get("active_kcal_sum")
        if basal is not None and active is not None:
            reading.total_calories_kcal = _safe_int(basal + active)
        reading.resting_hr_bpm = _safe_int(summary.get("hr_min"))
        reading.hrv_ms = summary.get("hrv_avg")
        reading.vo2_max = summary.get("vo2_max_last")

        # Sleep
        sleep_h = summary.get("sleep_asleep_h")
        reading.sleep_duration_h = sleep_h
        reading.deep_sleep_h = summary.get("sleep_deep_h")
        reading.rem_sleep_h = summary.get("sleep_rem_h")

        # Body temp delta (Apple Health stores absolute, we store delta)
        temp_values = summary.get("body_temp_values", [])
        if temp_values:
            # Use deviation from 37°C as proxy for delta
            avg_temp = sum(temp_values) / len(temp_values)
            reading.body_temperature_delta = round(avg_temp - 37.0, 2)

        logger.debug(
            "Apple Health %s: %d metrics",
            date_key, len(reading.available_metrics()),
        )
        return reading

    def is_available(self) -> bool:
        return bool(self._export_path and self._export_path.exists())

    # ------------------------------------------------------------------
    # XML parser (blocking — called in executor)
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_export(export_path: Path) -> dict[str, dict]:
        """
        Stream-parse the Apple Health export XML using iterparse.

        Builds a per-day accumulator dict then collapses it into daily summaries.
        Memory usage: O(days × records_per_day) — typically a few MB.
        """
        import xml.etree.ElementTree as ET

        # day → metric_type → list of float values
        accum: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
        # day → list of (start_dt, end_dt, value) for sleep
        sleep_records: dict[str, list] = defaultdict(list)

        context = ET.iterparse(str(export_path), events=("start",))
        parsed = 0

        for _, elem in context:
            if elem.tag != "Record":
                elem.clear()
                continue

            rtype = elem.get("type", "")
            start_str = elem.get("startDate", "")
            end_str = elem.get("endDate", "")
            val_str = elem.get("value", "")
            date_key = start_str[:10] if start_str else ""

            if not date_key:
                elem.clear()
                continue

            # Standard quantity records
            internal_key = _RECORD_MAP.get(rtype)
            if internal_key and val_str:
                try:
                    accum[date_key][internal_key].append(float(val_str))
                    parsed += 1
                except ValueError:
                    pass

            # Sleep analysis
            elif rtype == "HKCategoryTypeIdentifierSleepAnalysis":
                try:
                    start_dt = _parse_hk_date(start_str)
                    end_dt = _parse_hk_date(end_str)
                    sleep_records[date_key].append((start_dt, end_dt, val_str))
                    parsed += 1
                except ValueError:
                    pass

            elem.clear()

        logger.debug("Apple Health: processed %d records", parsed)

        # Collapse accumulators into per-day summaries
        daily: dict[str, dict] = {}

        for date_key, metrics in accum.items():
            s: dict = {}
            if "steps" in metrics:
                s["steps_sum"] = sum(metrics["steps"])
            if "active_kcal" in metrics:
                s["active_kcal_sum"] = sum(metrics["active_kcal"])
            if "basal_kcal" in metrics:
                s["basal_kcal_sum"] = sum(metrics["basal_kcal"])
            if "hr" in metrics:
                s["hr_min"] = min(metrics["hr"])
                s["hr_avg"] = sum(metrics["hr"]) / len(metrics["hr"])
            if "hrv" in metrics:
                s["hrv_avg"] = sum(metrics["hrv"]) / len(metrics["hrv"])
            if "vo2_max" in metrics:
                s["vo2_max_last"] = metrics["vo2_max"][-1]
            if "body_temp" in metrics:
                s["body_temp_values"] = metrics["body_temp"]
            daily[date_key] = s

        # Merge sleep data
        for date_key, records in sleep_records.items():
            s = daily.setdefault(date_key, {})
            asleep_s = deep_s = rem_s = 0.0
            for start_dt, end_dt, val in records:
                dur = (end_dt - start_dt).total_seconds()
                if val == _SLEEP_ASLEEP or val == _SLEEP_CORE:
                    asleep_s += dur
                elif val == _SLEEP_DEEP:
                    deep_s += dur
                elif val == _SLEEP_REM:
                    rem_s += dur
            if asleep_s > 0:
                s["sleep_asleep_h"] = asleep_s / 3600
            if deep_s > 0:
                s["sleep_deep_h"] = deep_s / 3600
            if rem_s > 0:
                s["sleep_rem_h"] = rem_s / 3600

        return daily


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_hk_date(date_str: str) -> datetime:
    """Parse Apple Health date format: '2025-06-01 22:30:00 +0200'"""
    # Normalise offset
    s = date_str.strip()
    # Replace space before timezone offset with '+'/'−' grouping
    for fmt in (
        "%Y-%m-%d %H:%M:%S %z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    raise ValueError(f"Cannot parse Apple Health date: {date_str!r}")


def _safe_int(value) -> Optional[int]:
    try:
        return int(round(float(value))) if value is not None else None
    except (TypeError, ValueError):
        return None
