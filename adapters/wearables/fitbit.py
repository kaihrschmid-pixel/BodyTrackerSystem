"""
adapters/wearables/fitbit.py
=============================
Fitbit Web API v1 adapter — fetches sleep, heart rate and activity data.

Fitbit uses OAuth 2.0 with an 8-hour access token and long-lived refresh token.
This adapter handles token refresh automatically and persists tokens to disk.

Supported Fitbit devices:
  - All Fitbit trackers and smartwatches (Charge, Versa, Sense, Inspire, Luxe…)
  - Premium features (sleep stages, HRV, SpO2) require a Fitbit Premium account

API reference: https://dev.fitbit.com/build/reference/web-api/
Register your app: https://dev.fitbit.com/apps/new
  → Application Type: Personal  (gives access to intraday & sleep data)
  → OAuth 2.0 Application Type: Personal

Configuration:
    wearables:
      fitbit:
        enabled: true
        client_id: "YOUR_CLIENT_ID"
        client_secret: "YOUR_CLIENT_SECRET"
        access_token: "..."          # stored after first auth
        refresh_token: "..."
        token_path: "data/fitbit_token.json"
        user_id: "-"                 # "-" = current authenticated user

Usage:
    async with FitbitAdapter(config["wearables"]["fitbit"]) as adapter:
        context = await adapter.fetch_context(date=datetime.now(timezone.utc))
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from adapters.base import ContextReading, WearableAdapter, utcnow

logger = logging.getLogger(__name__)

_TOKEN_URL = "https://api.fitbit.com/oauth2/token"
_BASE_URL = "https://api.fitbit.com"


class FitbitAdapter(WearableAdapter):
    """
    Reads Fitbit data for a given date using the Fitbit Web API v1.

    Endpoints used:
      /1/user/{user_id}/sleep/date/{date}.json     → sleep stages, duration, efficiency
      /1/user/{user_id}/activities/heart/date/{date}/1d.json   → resting HR, HR zones
      /1/user/{user_id}/activities/date/{date}.json             → steps, calories
      /1/user/{user_id}/hrv/date/{date}.json        → daily HRV (Premium)
    """

    ADAPTER_NAME = "fitbit"

    def __init__(self, config: dict):
        super().__init__(config)
        self._client_id: str = config.get("client_id", "")
        self._client_secret: str = config.get("client_secret", "")
        self._access_token: str = config.get("access_token", "")
        self._refresh_token: str = config.get("refresh_token", "")
        self._token_path: Optional[Path] = (
            Path(config["token_path"]) if config.get("token_path") else None
        )
        self._user_id: str = config.get("user_id", "-")
        self._client = None  # httpx.AsyncClient

    # ------------------------------------------------------------------
    # WearableAdapter interface
    # ------------------------------------------------------------------

    async def authenticate(self) -> None:
        """Load tokens from disk and refresh the access token."""
        import httpx  # lazy import

        self._load_tokens()

        if not self._refresh_token:
            raise ValueError(
                "Fitbit refresh_token is missing. "
                "Complete the OAuth flow first: body-tracker auth fitbit\n"
                "Register at: https://dev.fitbit.com/apps/new"
            )

        self._client = httpx.AsyncClient(timeout=20.0)
        await self._refresh_access_token()
        logger.info("FitbitAdapter authenticated (user_id=%s)", self._user_id)

    async def fetch_context(self, date: datetime) -> ContextReading:
        """Fetch sleep, heart rate, activity and HRV for the given date."""
        if self._client is None:
            await self.authenticate()

        date_str = date.strftime("%Y-%m-%d")
        uid = self._user_id

        import asyncio
        sleep_raw, hr_raw, activity_raw, hrv_raw = await asyncio.gather(
            self._get(f"/1/user/{uid}/sleep/date/{date_str}.json"),
            self._get(f"/1/user/{uid}/activities/heart/date/{date_str}/1d.json"),
            self._get(f"/1/user/{uid}/activities/date/{date_str}.json"),
            self._get(f"/1/user/{uid}/hrv/date/{date_str}.json"),
            return_exceptions=True,
        )

        reading = ContextReading(
            date=date.replace(hour=0, minute=0, second=0, microsecond=0),
            source="fitbit",
            adapter_name=self.ADAPTER_NAME,
        )

        self._parse_sleep(sleep_raw, reading)
        self._parse_heart_rate(hr_raw, reading)
        self._parse_activity(activity_raw, reading)
        self._parse_hrv(hrv_raw, reading)

        logger.info(
            "Fitbit context for %s: %d metrics",
            date_str, len(reading.available_metrics()),
        )
        return reading

    def is_available(self) -> bool:
        return bool(self._client_id and self._refresh_token)

    # ------------------------------------------------------------------
    # Private: HTTP
    # ------------------------------------------------------------------

    async def _get(self, path: str) -> dict | Exception:
        try:
            resp = await self._client.get(
                _BASE_URL + path,
                headers={"Authorization": f"Bearer {self._access_token}"},
            )
            if resp.status_code == 401:
                # Try one token refresh, then retry
                await self._refresh_access_token()
                resp = await self._client.get(
                    _BASE_URL + path,
                    headers={"Authorization": f"Bearer {self._access_token}"},
                )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.warning("Fitbit %s failed: %s", path, exc)
            return exc

    async def _refresh_access_token(self) -> None:
        """Exchange refresh_token for a new access + refresh token pair."""
        # Fitbit uses HTTP Basic Auth with client_id:client_secret
        credentials = base64.b64encode(
            f"{self._client_id}:{self._client_secret}".encode()
        ).decode()

        resp = await self._client.post(
            _TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
            },
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )

        if resp.status_code in (400, 401):
            raise RuntimeError(
                "Fitbit token refresh failed — the refresh token may be expired.\n"
                "Re-authorise at: https://www.fitbit.com/oauth2/authorize\n"
                "Or run: body-tracker auth fitbit"
            )

        resp.raise_for_status()
        body = resp.json()

        self._access_token = body["access_token"]
        self._refresh_token = body["refresh_token"]
        self._save_tokens()
        logger.debug("Fitbit access token refreshed")

    # ------------------------------------------------------------------
    # Token persistence
    # ------------------------------------------------------------------

    def _load_tokens(self) -> None:
        if not self._token_path or not self._token_path.exists():
            return
        try:
            data = json.loads(self._token_path.read_text())
            self._access_token = data.get("access_token", self._access_token)
            self._refresh_token = data.get("refresh_token", self._refresh_token)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not load Fitbit token file: %s", exc)

    def _save_tokens(self) -> None:
        if not self._token_path:
            return
        try:
            self._token_path.parent.mkdir(parents=True, exist_ok=True)
            self._token_path.write_text(json.dumps({
                "access_token": self._access_token,
                "refresh_token": self._refresh_token,
                "updated_at": utcnow().isoformat(),
            }, indent=2))
        except OSError as exc:
            logger.warning("Could not save Fitbit tokens: %s", exc)

    # ------------------------------------------------------------------
    # Parsers
    # ------------------------------------------------------------------

    def _parse_sleep(self, raw: dict | Exception, reading: ContextReading) -> None:
        if isinstance(raw, Exception):
            return
        summary = raw.get("summary", {})
        main = next(
            (s for s in raw.get("sleep", []) if s.get("isMainSleep")),
            raw.get("sleep", [None])[0] if raw.get("sleep") else None,
        )
        if not main:
            return
        duration_ms = main.get("duration", 0)
        reading.sleep_duration_h = duration_ms / 3_600_000 if duration_ms else None
        reading.sleep_efficiency_pct = main.get("efficiency")
        reading.sleep_latency_min = main.get("minutesToFallAsleep")
        levels = main.get("levels", {}).get("summary", {})
        deep_min = levels.get("deep", {}).get("minutes")
        reading.deep_sleep_h = deep_min / 60 if deep_min else None
        rem_min = levels.get("rem", {}).get("minutes")
        reading.rem_sleep_h = rem_min / 60 if rem_min else None
        reading.sleep_score = main.get("efficiency")  # Fitbit uses efficiency as proxy
        reading.raw_payload = reading.raw_payload or {}
        reading.raw_payload["sleep"] = main

    def _parse_heart_rate(self, raw: dict | Exception, reading: ContextReading) -> None:
        if isinstance(raw, Exception):
            return
        activities = raw.get("activities-heart", [{}])
        if not activities:
            return
        hr_data = activities[0].get("value", {})
        reading.resting_hr_bpm = hr_data.get("restingHeartRate")
        zones = hr_data.get("heartRateZones", [])
        for zone in zones:
            name = zone.get("name", "").lower().replace(" ", "_")
            minutes = zone.get("minutes")
            if minutes is not None:
                reading.extras[f"hr_zone_{name}_min"] = float(minutes)
        reading.raw_payload = reading.raw_payload or {}
        reading.raw_payload["heart_rate"] = hr_data

    def _parse_activity(self, raw: dict | Exception, reading: ContextReading) -> None:
        if isinstance(raw, Exception):
            return
        summary = raw.get("summary", {})
        reading.steps = summary.get("steps")
        reading.active_calories_kcal = summary.get("activityCalories")
        reading.total_calories_kcal = summary.get("caloriesOut")
        active_min = (
            (summary.get("veryActiveMinutes") or 0)
            + (summary.get("fairlyActiveMinutes") or 0)
        )
        reading.active_minutes = active_min if active_min > 0 else None
        reading.raw_payload = reading.raw_payload or {}
        reading.raw_payload["activity"] = summary

    def _parse_hrv(self, raw: dict | Exception, reading: ContextReading) -> None:
        if isinstance(raw, Exception):
            return
        hrv_data = raw.get("hrv", [])
        if not hrv_data:
            return
        daily = hrv_data[0].get("value", {})
        reading.hrv_ms = daily.get("dailyRmssd") or daily.get("deepRmssd")
        reading.raw_payload = reading.raw_payload or {}
        reading.raw_payload["hrv"] = daily

    async def __aexit__(self, *_):
        if self._client:
            await self._client.aclose()
            self._client = None
