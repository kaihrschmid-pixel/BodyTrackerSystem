"""
adapters/scales/manual.py
=========================
Manual entry and CSV import adapter — the universal fallback.

Works for:
  - Any scale without Bluetooth/WiFi
  - Legacy devices with CSV export
  - Testing and development (no hardware needed)
  - Batch importing historical data

Two modes:
  1. Interactive: prompts the user for input on the terminal
  2. CSV import:  reads from a structured CSV file

CSV format (one row per measurement):
    date,weight_kg,body_fat_pct,muscle_mass_kg,bone_mass_kg,water_pct,...
    2025-01-15,82.4,18.2,65.1,3.2,57.4,...

Configuration:
    hardware:
      scale:
        adapter: "manual"
        mode: "interactive"   # or "csv"
        csv_path: "data/import.csv"   # only for csv mode
"""

from __future__ import annotations

import asyncio
import csv
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from adapters.base import ScaleAdapter, ScaleReading, utcnow

logger = logging.getLogger(__name__)

# Field names accepted in CSV (maps CSV column → ScaleReading attribute)
CSV_FIELD_MAP = {
    "date": None,  # handled separately
    "weight_kg": "weight_kg",
    "body_fat_pct": "body_fat_pct",
    "muscle_mass_kg": "muscle_mass_kg",
    "bone_mass_kg": "bone_mass_kg",
    "water_pct": "water_pct",
    "visceral_fat_index": "visceral_fat_index",
    "bmr_kcal": "bmr_kcal",
    "bmi": "bmi",
    "metabolic_age": "metabolic_age",
    "phase_angle": "phase_angle",
    "ecm_bcm_ratio": "ecm_bcm_ratio",
    "heart_rate_bpm": "heart_rate_bpm",
    "pulse_wave_velocity": "pulse_wave_velocity",
    "vascular_age": "vascular_age",
}

INT_FIELDS = {"metabolic_age", "heart_rate_bpm", "vascular_age"}


class ManualAdapter(ScaleAdapter):
    """
    Interactive terminal input or CSV import.
    Useful as the zero-hardware starting point and for testing.
    """

    ADAPTER_NAME = "manual"

    def __init__(self, config: dict):
        super().__init__(config)
        self._mode = config.get("mode", "interactive")
        self._csv_path = Path(config["csv_path"]) if config.get("csv_path") else None
        self._pending_rows: list[dict] = []

    # ------------------------------------------------------------------
    # ScaleAdapter interface
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        if self._mode == "csv":
            if not self._csv_path or not self._csv_path.exists():
                raise FileNotFoundError(
                    f"CSV file not found: {self._csv_path}. "
                    "Set csv_path in config or switch to mode: interactive"
                )
            self._pending_rows = self._load_csv(self._csv_path)
            logger.info("ManualAdapter loaded %d rows from %s", len(self._pending_rows), self._csv_path)
        else:
            logger.info("ManualAdapter ready — interactive mode")

    async def read(self) -> ScaleReading:
        if self._mode == "csv":
            return await self._read_from_csv()
        return await self._read_interactive()

    async def disconnect(self) -> None:
        pass  # nothing to close

    def is_available(self) -> bool:
        if self._mode == "csv":
            return self._csv_path is not None and self._csv_path.exists()
        return True  # interactive is always available

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    async def _read_interactive(self) -> ScaleReading:
        """Prompt user for values on the terminal."""
        print("\n--- Body Tracker: Manual Measurement Entry ---")
        print("Press Enter to skip optional fields.\n")

        weight = await self._prompt_float("Weight (kg)", required=True)
        body_fat = await self._prompt_float("Body fat (%)")
        muscle = await self._prompt_float("Muscle mass (kg)")
        bone = await self._prompt_float("Bone mass (kg)")
        water = await self._prompt_float("Water (%)")
        visceral = await self._prompt_float("Visceral fat index")
        bmr = await self._prompt_float("BMR (kcal)")
        phase = await self._prompt_float("Phase angle")
        hr = await self._prompt_int("Heart rate (bpm)")

        return ScaleReading(
            recorded_at=utcnow(),
            weight_kg=weight,
            body_fat_pct=body_fat,
            muscle_mass_kg=muscle,
            bone_mass_kg=bone,
            water_pct=water,
            visceral_fat_index=visceral,
            bmr_kcal=bmr,
            phase_angle=phase,
            heart_rate_bpm=hr,
            adapter_name=self.ADAPTER_NAME,
        )

    async def _read_from_csv(self) -> ScaleReading:
        """Pop and return the next row from the loaded CSV."""
        if not self._pending_rows:
            raise RuntimeError("No more rows in CSV — import complete.")
        row = self._pending_rows.pop(0)

        # Parse date from row
        date_str = row.get("date", "")
        try:
            recorded_at = datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)
        except ValueError:
            recorded_at = utcnow()
            logger.warning("Could not parse date '%s', using now()", date_str)

        kwargs: dict = {"recorded_at": recorded_at, "adapter_name": self.ADAPTER_NAME}
        for csv_col, attr in CSV_FIELD_MAP.items():
            if attr is None or csv_col not in row or not row[csv_col]:
                continue
            try:
                val: float | int = int(row[csv_col]) if attr in INT_FIELDS else float(row[csv_col])
                kwargs[attr] = val
            except (ValueError, TypeError):
                logger.warning("Could not parse %s=%r — skipping", csv_col, row[csv_col])

        if "weight_kg" not in kwargs:
            raise ValueError(f"CSV row missing required 'weight_kg': {row}")

        return ScaleReading(**kwargs)

    @staticmethod
    def _load_csv(path: Path) -> list[dict]:
        with path.open(newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    @staticmethod
    async def _prompt_float(label: str, required: bool = False) -> Optional[float]:
        while True:
            raw = await asyncio.get_event_loop().run_in_executor(
                None, input, f"  {label}: "
            )
            raw = raw.strip()
            if not raw:
                if required:
                    print("  This field is required.")
                    continue
                return None
            try:
                return float(raw)
            except ValueError:
                print(f"  Please enter a number (e.g. 82.5)")

    @staticmethod
    async def _prompt_int(label: str) -> Optional[int]:
        raw = await asyncio.get_event_loop().run_in_executor(
            None, input, f"  {label}: "
        )
        raw = raw.strip()
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            return None
