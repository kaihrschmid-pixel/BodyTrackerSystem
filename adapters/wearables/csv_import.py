"""
adapters/wearables/csv_import.py
=================================
CSV import adapter for wearable/context data — the universal fallback.

Reads daily wellness data from a plain CSV file, one row per date.
Useful for:
  - Importing historical data from fitness apps (most export CSV)
  - Manual entry of context data when no API is available
  - Testing without real wearable hardware
  - Migrating data from other tracking systems

CSV format (header row required, date column mandatory):
    date,sleep_duration_h,sleep_score,hrv_ms,resting_hr_bpm,readiness_score,
         steps,active_calories_kcal,total_calories_kcal,active_minutes,vo2_max,
         training_load,deep_sleep_h,rem_sleep_h,sleep_efficiency_pct,
         body_temperature_delta,sleep_latency_min

All columns except `date` are optional. Unknown columns are stored in extras.

Configuration:
    wearables:
      csv:
        enabled: true
        csv_path: "data/wearable_history.csv"
        date_format: "%Y-%m-%d"   # optional, default: auto-detect ISO 8601
        source_label: "csv"       # label used as ContextReading.source

Usage:
    async with CSVWearableAdapter(config["wearables"]["csv"]) as adapter:
        context = await adapter.fetch_context(date=datetime.now(timezone.utc))
"""

from __future__ import annotations

import csv
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from adapters.base import ContextReading, WearableAdapter, utcnow

logger = logging.getLogger(__name__)

# Maps CSV column name → ContextReading field name (None = skip / handled specially)
_FIELD_MAP: dict[str, Optional[str]] = {
    "date":                     None,   # handled separately
    "sleep_duration_h":         "sleep_duration_h",
    "sleep_score":              "sleep_score",
    "sleep_efficiency_pct":     "sleep_efficiency_pct",
    "deep_sleep_h":             "deep_sleep_h",
    "rem_sleep_h":              "rem_sleep_h",
    "sleep_latency_min":        "sleep_latency_min",
    "hrv_ms":                   "hrv_ms",
    "resting_hr_bpm":           "resting_hr_bpm",
    "readiness_score":          "readiness_score",
    "body_temperature_delta":   "body_temperature_delta",
    "steps":                    "steps",
    "active_calories_kcal":     "active_calories_kcal",
    "total_calories_kcal":      "total_calories_kcal",
    "active_minutes":           "active_minutes",
    "vo2_max":                  "vo2_max",
    "training_load":            "training_load",
}

# Fields stored as int in ContextReading
_INT_FIELDS = {
    "sleep_score", "sleep_latency_min", "resting_hr_bpm",
    "readiness_score", "steps", "active_calories_kcal",
    "total_calories_kcal", "active_minutes",
}


class CSVWearableAdapter(WearableAdapter):
    """
    Serves ContextReading objects from a CSV file indexed by date.

    On authenticate(), the CSV is loaded into a date-keyed dict.
    fetch_context() does an O(1) lookup — no I/O per call.
    """

    ADAPTER_NAME = "csv"

    def __init__(self, config: dict):
        super().__init__(config)
        self._csv_path: Optional[Path] = (
            Path(config["csv_path"]) if config.get("csv_path") else None
        )
        self._date_format: str = config.get("date_format", "%Y-%m-%d")
        self._source_label: str = config.get("source_label", "csv")
        # date string → row dict, populated in authenticate()
        self._rows: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # WearableAdapter interface
    # ------------------------------------------------------------------

    async def authenticate(self) -> None:
        """Load and index the CSV file. Raises if path is missing."""
        if not self._csv_path:
            raise ValueError(
                "CSVWearableAdapter requires csv_path in config.\n"
                "Example:  wearables:\n"
                "            csv:\n"
                "              enabled: true\n"
                "              csv_path: data/wearable_history.csv"
            )
        if not self._csv_path.exists():
            raise FileNotFoundError(
                f"Wearable CSV not found: {self._csv_path}"
            )
        self._rows = self._load_csv()
        logger.info(
            "CSVWearableAdapter loaded %d rows from %s",
            len(self._rows), self._csv_path,
        )

    async def fetch_context(self, date: datetime) -> ContextReading:
        """Return ContextReading for the given date, or a minimal one if not found."""
        date_key = date.strftime(self._date_format)
        row = self._rows.get(date_key)

        reading = ContextReading(
            date=date.replace(hour=0, minute=0, second=0, microsecond=0),
            source=self._source_label,
            adapter_name=self.ADAPTER_NAME,
        )

        if row is None:
            logger.debug("CSVWearableAdapter: no data for %s", date_key)
            return reading

        self._populate_reading(row, reading)
        logger.debug(
            "CSVWearableAdapter: %s → %d metrics",
            date_key, len(reading.available_metrics()),
        )
        return reading

    def is_available(self) -> bool:
        return bool(self._csv_path and self._csv_path.exists())

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _load_csv(self) -> dict[str, dict]:
        rows: dict[str, dict] = {}
        with self._csv_path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                date_val = row.get("date", "").strip()
                if not date_val:
                    continue
                # Normalise date to self._date_format
                try:
                    dt = self._parse_date(date_val)
                    key = dt.strftime(self._date_format)
                    rows[key] = row
                except ValueError:
                    logger.warning("CSVWearableAdapter: cannot parse date %r — skipping row", date_val)
        return rows

    def _parse_date(self, date_str: str) -> datetime:
        """Try configured format first, then common ISO variants."""
        formats = [self._date_format, "%Y-%m-%d", "%d.%m.%Y", "%m/%d/%Y", "%Y%m%d"]
        for fmt in formats:
            try:
                return datetime.strptime(date_str.strip(), fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        raise ValueError(f"Cannot parse date: {date_str!r}")

    def _populate_reading(self, row: dict, reading: ContextReading) -> None:
        """Map CSV columns → ContextReading fields; unknown columns → extras."""
        known = set(_FIELD_MAP.keys())
        for col, val in row.items():
            col = col.strip()
            val = (val or "").strip()
            if not val or col == "date":
                continue

            field = _FIELD_MAP.get(col)
            if field is None and col in _FIELD_MAP:
                continue  # explicitly skipped

            if field is not None:
                # Known field
                try:
                    parsed: float | int = (
                        int(float(val)) if field in _INT_FIELDS else float(val)
                    )
                    setattr(reading, field, parsed)
                except (ValueError, TypeError):
                    logger.warning("CSVWearableAdapter: cannot parse %s=%r — skipping", col, val)
            elif col not in known:
                # Unknown column → extras
                try:
                    reading.extras[col] = float(val)
                except (ValueError, TypeError):
                    pass   # non-numeric extras silently ignored
