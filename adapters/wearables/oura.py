"""
adapters/wearables/oura.py
==========================
Oura Ring adapter — fetches sleep, HRV, readiness and activity
from the Oura Cloud API v2.

Oura is the ideal first wearable adapter to implement because:
- Clean REST API, no SDK needed
- Personal Access Token (no OAuth dance for self-hosted use)
- Rich sleep/recovery data that directly contextualises scale readings
- Well documented: https://cloud.ouraring.com/v2/docs

Configuration (in config.yaml):
    wearables:
      oura:
        enabled: true
        personal_access_token: "YOUR_TOKEN_HERE"
        # Get yours at: https://cloud.ouraring.com/personal-access-tokens

Usage:
    async with OuraAdapter(config["wearables"]["oura"]) as adapter:
        context = await adapter.fetch_context(date=datetime.now(timezone.utc))
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from adapters.base import ContextReading, WearableAdapter

logger = logging.getLogger(__name__)

OURA_BASE = "https://api.ouraring.com/v2/usercollection"


class OuraAdapter(WearableAdapter):
    """
    Fetches Oura Ring data for a given date.

    Collects from three endpoints per call:
      /daily_sleep    → sleep score, stages, efficiency
      /daily_readiness → readiness score, HRV, resting HR, temp delta
      /daily_activity  → steps, calories, active minutes, training load
    """

    ADAPTER_NAME = "oura"

    def __init__(self, config: dict):
        super().__init__(config)
        self._token: Optional[str] = config.get("personal_access_token")
        self._client: Optional[httpx.AsyncClient] = None

    # ------------------------------------------------------------------
    # WearableAdapter interface
    # ------------------------------------------------------------------

    async def authenticate(self) -> None:
        """
        Oura PAT needs no OAuth dance — just validate the token works
        by hitting the /personal_info endpoint.
        """
        if not self._token:
            raise ValueError(
                "Oura personal_access_token missing from config. "
                "Get yours at https://cloud.ouraring.com/personal-access-tokens"
            )
        self._client = httpx.AsyncClient(
            base_url=OURA_BASE,
            headers={"Authorization": f"Bearer {self._token}"},
            timeout=15.0,
        )
        # Quick connectivity check
        resp = await self._client.get(
            "https://api.ouraring.com/v2/usercollection/personal_info"
        )
        resp.raise_for_status()
        info = resp.json()
        logger.info(
            "Oura authenticated for user age=%s, biological_sex=%s",
            info.get("age"),
            info.get("biological_sex"),
        )

    async def fetch_context(self, date: datetime) -> ContextReading:
        """
        Fetch all available Oura data for `date`.
        Oura's API expects dates in YYYY-MM-DD format.
        Data for a day is typically available the morning after.
        """
        if self._client is None:
            await self.authenticate()

        date_str = date.strftime("%Y-%m-%d")
        # Oura needs start_date and end_date; we want exactly one day
        params = {"start_date": date_str, "end_date": date_str}

        # Fire all three requests concurrently
        import asyncio
        sleep_raw, readiness_raw, activity_raw = await asyncio.gather(
            self._get("/daily_sleep", params),
            self._get("/daily_readiness", params),
            self._get("/daily_activity", params),
            return_exceptions=True,
        )

        reading = ContextReading(
            date=date.replace(hour=0, minute=0, second=0, microsecond=0),
            source="oura",
            adapter_name=self.ADAPTER_NAME,
        )

        # Parse each endpoint independently — partial data is fine
        self._parse_sleep(sleep_raw, reading)
        self._parse_readiness(readiness_raw, reading)
        self._parse_activity(activity_raw, reading)

        logger.info(
            "Oura context for %s: %s metrics available",
            date_str,
            len(reading.available_metrics()),
        )
        return reading

    def is_available(self) -> bool:
        return bool(self._token)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _get(self, path: str, params: dict) -> dict | Exception:
        """GET with graceful error handling — returns Exception on failure."""
        try:
            resp = await self._client.get(path, params=params)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.warning("Oura %s failed: %s", path, exc)
            return exc

    def _first_item(self, raw: dict | Exception) -> Optional[dict]:
        """Extract first item from a paginated Oura response."""
        if isinstance(raw, Exception):
            return None
        items = raw.get("data", [])
        return items[0] if items else None

    def _parse_sleep(self, raw: dict | Exception, reading: ContextReading) -> None:
        item = self._first_item(raw)
        if not item:
            return
        reading.sleep_score = item.get("score")
        contributors = item.get("contributors", {})
        # Oura v2 returns durations in seconds
        reading.sleep_duration_h = self._sec_to_h(item.get("total_sleep_duration"))
        reading.deep_sleep_h = self._sec_to_h(item.get("deep_sleep_duration"))
        reading.rem_sleep_h = self._sec_to_h(item.get("rem_sleep_duration"))
        reading.sleep_efficiency_pct = item.get("efficiency")
        reading.sleep_latency_min = self._sec_to_min(item.get("latency"))
        reading.raw_payload = reading.raw_payload or {}
        reading.raw_payload["sleep"] = item

    def _parse_readiness(self, raw: dict | Exception, reading: ContextReading) -> None:
        item = self._first_item(raw)
        if not item:
            return
        reading.readiness_score = item.get("score")
        reading.hrv_ms = item.get("contributors", {}).get("hrv_balance")
        reading.resting_hr_bpm = item.get("contributors", {}).get("resting_heart_rate")
        reading.body_temperature_delta = item.get("temperature_deviation")
        # Store HRV directly if available at top level
        if item.get("average_hrv_5min"):
            reading.hrv_ms = item["average_hrv_5min"]
        reading.raw_payload = reading.raw_payload or {}
        reading.raw_payload["readiness"] = item

    def _parse_activity(self, raw: dict | Exception, reading: ContextReading) -> None:
        item = self._first_item(raw)
        if not item:
            return
        reading.steps = item.get("steps")
        reading.active_calories_kcal = item.get("active_calories")
        reading.total_calories_kcal = item.get("total_calories")
        reading.active_minutes = item.get("high_activity_time")
        if reading.active_minutes:
            reading.active_minutes = reading.active_minutes // 60  # sec → min
        # Training load / equivalent walking distance as proxy
        if item.get("equivalent_walking_distance"):
            reading.extras["equivalent_walking_distance_m"] = float(
                item["equivalent_walking_distance"]
            )
        reading.raw_payload = reading.raw_payload or {}
        reading.raw_payload["activity"] = item

    @staticmethod
    def _sec_to_h(seconds: Optional[int]) -> Optional[float]:
        if seconds is None:
            return None
        return round(seconds / 3600, 2)

    @staticmethod
    def _sec_to_min(seconds: Optional[int]) -> Optional[int]:
        if seconds is None:
            return None
        return seconds // 60

    async def __aexit__(self, *_):
        if self._client:
            await self._client.aclose()
