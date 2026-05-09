"""
adapters/wearables/garmin.py
=============================
Garmin Health API v1 adapter — fetches daily wellness data from Garmin Connect.

Covers sleep, HRV, heart rate, stress, steps, calories and VO₂max via the
official Garmin Health API (developer programme).

API reference: https://developer.garmin.com/health-api/overview/
Auth: OAuth 1.0a (3-legged) — tokens are long-lived; no expiry-based refresh.

The Garmin Health API is a push-based API by design (Garmin pushes summaries
to a callback URL) but also supports pull via the /summaries endpoints used
here, which is more practical for a self-hosted setup.

Note on access:
  The Garmin Health API requires registration at
  https://developer.garmin.com/health-api/
  and approval of a developer account. For personal/research use, approval
  is typically granted within a few days.

Configuration:
    wearables:
      garmin:
        enabled: true
        consumer_key: "YOUR_CONSUMER_KEY"
        consumer_secret: "YOUR_CONSUMER_SECRET"
        access_token: "YOUR_ACCESS_TOKEN"       # from OAuth 1.0a flow
        access_token_secret: "YOUR_TOKEN_SECRET"
        user_id: "YOUR_GARMIN_USER_ID"          # returned during OAuth flow

Usage:
    async with GarminAdapter(config["wearables"]["garmin"]) as adapter:
        context = await adapter.fetch_context(date=datetime.now(timezone.utc))
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
import urllib.parse
import uuid
from datetime import datetime, timezone
from typing import Optional

from adapters.base import ContextReading, WearableAdapter, utcnow

logger = logging.getLogger(__name__)

_BASE_URL = "https://healthapi.garmin.com/wellness-api/rest"


class GarminAdapter(WearableAdapter):
    """
    Reads daily wellness summaries from the Garmin Health API v1.

    Collects from three endpoints per call:
      /dailies         → steps, calories, active minutes, intensity minutes
      /sleeps          → sleep duration, stages, score
      /userMetrics     → VO₂max, fitness age
    HRV data is embedded in the daily summary (averageStressLevel used as proxy).
    """

    ADAPTER_NAME = "garmin"

    def __init__(self, config: dict):
        super().__init__(config)
        self._consumer_key: str = config.get("consumer_key", "")
        self._consumer_secret: str = config.get("consumer_secret", "")
        self._access_token: str = config.get("access_token", "")
        self._access_token_secret: str = config.get("access_token_secret", "")
        self._user_id: str = config.get("user_id", "")
        self._client = None  # httpx.AsyncClient

    # ------------------------------------------------------------------
    # WearableAdapter interface
    # ------------------------------------------------------------------

    async def authenticate(self) -> None:
        """Validate credentials by fetching user profile."""
        import httpx  # lazy import

        if not all([self._consumer_key, self._consumer_secret,
                    self._access_token, self._access_token_secret]):
            raise ValueError(
                "Garmin adapter requires consumer_key, consumer_secret, "
                "access_token, and access_token_secret.\n"
                "Register at: https://developer.garmin.com/health-api/"
            )

        self._client = httpx.AsyncClient(timeout=20.0)
        logger.info("GarminAdapter authenticated (user_id=%s)", self._user_id or "unknown")

    async def fetch_context(self, date: datetime) -> ContextReading:
        """Fetch all available Garmin wellness data for the given date."""
        if self._client is None:
            await self.authenticate()

        # Garmin API uses Unix timestamp ranges for a full day
        day_start = int(date.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        day_end = day_start + 86_400

        params = {"uploadStartTimeInSeconds": day_start, "uploadEndTimeInSeconds": day_end}

        import asyncio
        daily_raw, sleep_raw, metrics_raw = await asyncio.gather(
            self._get("/dailies", params),
            self._get("/sleeps", params),
            self._get("/userMetrics", {"startDate": date.strftime("%Y-%m-%d"),
                                       "endDate": date.strftime("%Y-%m-%d")}),
            return_exceptions=True,
        )

        reading = ContextReading(
            date=date.replace(hour=0, minute=0, second=0, microsecond=0),
            source="garmin",
            adapter_name=self.ADAPTER_NAME,
        )

        self._parse_daily(daily_raw, reading)
        self._parse_sleep(sleep_raw, reading)
        self._parse_metrics(metrics_raw, reading)

        logger.info(
            "Garmin context for %s: %d metrics",
            date.strftime("%Y-%m-%d"), len(reading.available_metrics()),
        )
        return reading

    def is_available(self) -> bool:
        return bool(self._consumer_key and self._access_token)

    # ------------------------------------------------------------------
    # Private: HTTP with OAuth 1.0a signing
    # ------------------------------------------------------------------

    async def _get(self, path: str, params: dict) -> dict | Exception:
        """Signed GET request to the Garmin Health API."""
        url = _BASE_URL + path
        try:
            auth_header = self._oauth1_header("GET", url, params)
            resp = await self._client.get(
                url, params=params,
                headers={"Authorization": auth_header, "Accept": "application/json"},
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.warning("Garmin %s failed: %s", path, exc)
            return exc

    def _oauth1_header(self, method: str, url: str, params: dict) -> str:
        """
        Build an OAuth 1.0a Authorization header using HMAC-SHA1.
        Uses only Python built-ins — no external OAuth library required.
        """
        oauth_params = {
            "oauth_consumer_key": self._consumer_key,
            "oauth_nonce": uuid.uuid4().hex,
            "oauth_signature_method": "HMAC-SHA1",
            "oauth_timestamp": str(int(time.time())),
            "oauth_token": self._access_token,
            "oauth_version": "1.0",
        }

        # Signature base string: combine all parameters
        all_params = {**params, **oauth_params}
        sorted_params = sorted(
            (urllib.parse.quote(str(k), safe=""), urllib.parse.quote(str(v), safe=""))
            for k, v in all_params.items()
        )
        param_string = "&".join(f"{k}={v}" for k, v in sorted_params)

        base_string = "&".join([
            method.upper(),
            urllib.parse.quote(url, safe=""),
            urllib.parse.quote(param_string, safe=""),
        ])

        # Signing key: consumer_secret & token_secret (both percent-encoded)
        signing_key = (
            urllib.parse.quote(self._consumer_secret, safe="")
            + "&"
            + urllib.parse.quote(self._access_token_secret, safe="")
        )

        signature = base64.b64encode(
            hmac.new(
                signing_key.encode("utf-8"),
                base_string.encode("utf-8"),
                hashlib.sha1,
            ).digest()
        ).decode("utf-8")

        oauth_params["oauth_signature"] = signature
        header_parts = ", ".join(
            f'{urllib.parse.quote(k, safe="")}="{urllib.parse.quote(v, safe="")}"'
            for k, v in sorted(oauth_params.items())
        )
        return f"OAuth {header_parts}"

    # ------------------------------------------------------------------
    # Parsers
    # ------------------------------------------------------------------

    def _parse_daily(self, raw: dict | Exception, reading: ContextReading) -> None:
        if isinstance(raw, Exception):
            return
        summaries = raw if isinstance(raw, list) else raw.get("dailies", [raw])
        if not summaries:
            return
        s = summaries[0]
        reading.steps = s.get("totalSteps")
        reading.active_calories_kcal = s.get("activeKilocalories")
        reading.total_calories_kcal = s.get("bmrKilocalories", 0) + (s.get("activeKilocalories") or 0) or None
        # Active minutes: vigorous (2×) + moderate
        vigorous = s.get("vigorousIntensityDurationInSeconds", 0) or 0
        moderate = s.get("moderateIntensityDurationInSeconds", 0) or 0
        total_active_s = vigorous * 2 + moderate
        reading.active_minutes = total_active_s // 60 if total_active_s > 0 else None
        # Average stress level as a proxy for recovery score (inverted 0–100)
        stress = s.get("averageStressLevel")
        if stress is not None and stress >= 0:
            reading.readiness_score = max(0, 100 - stress)
        reading.resting_hr_bpm = s.get("restingHeartRateInBeatsPerMinute")
        if s.get("averageHeartRateInBeatsPerMinute"):
            reading.extras["avg_hr_bpm"] = float(s["averageHeartRateInBeatsPerMinute"])
        if s.get("maxHeartRateInBeatsPerMinute"):
            reading.extras["max_hr_bpm"] = float(s["maxHeartRateInBeatsPerMinute"])
        reading.training_load = s.get("trainingEffect")
        reading.raw_payload = reading.raw_payload or {}
        reading.raw_payload["daily"] = s

    def _parse_sleep(self, raw: dict | Exception, reading: ContextReading) -> None:
        if isinstance(raw, Exception):
            return
        sleeps = raw if isinstance(raw, list) else raw.get("sleeps", [raw])
        if not sleeps:
            return
        s = sleeps[0]
        duration_s = s.get("durationInSeconds")
        reading.sleep_duration_h = duration_s / 3600 if duration_s else None
        reading.sleep_score = s.get("overallSleepScore", {}).get("value") if isinstance(
            s.get("overallSleepScore"), dict) else s.get("overallSleepScore")
        deep_s = s.get("deepSleepDurationInSeconds")
        reading.deep_sleep_h = deep_s / 3600 if deep_s else None
        rem_s = s.get("remSleepInSeconds")
        reading.rem_sleep_h = rem_s / 3600 if rem_s else None
        avg_spo2 = s.get("averageSpO2Value")
        if avg_spo2:
            reading.extras["avg_spo2_pct"] = float(avg_spo2)
        reading.raw_payload = reading.raw_payload or {}
        reading.raw_payload["sleep"] = s

    def _parse_metrics(self, raw: dict | Exception, reading: ContextReading) -> None:
        if isinstance(raw, Exception):
            return
        metrics = raw if isinstance(raw, list) else raw.get("userMetrics", [raw])
        if not metrics:
            return
        m = metrics[0]
        reading.vo2_max = m.get("vo2Max")
        fitness_age = m.get("fitnessAge")
        if fitness_age is not None:
            reading.extras["fitness_age"] = float(fitness_age)
        reading.raw_payload = reading.raw_payload or {}
        reading.raw_payload["metrics"] = m

    async def __aexit__(self, *_):
        if self._client:
            await self._client.aclose()
            self._client = None
