"""
adapters/wearables/withings_health.py
=======================================
Withings Health API adapter — wearable context data from Withings devices.

This adapter fetches activity and sleep context from the Withings cloud API,
complementing the Withings scale adapter (adapters/scales/withings.py) which
handles body composition measurements.

Data available via this adapter (not the scale):
  - Daily activity: steps, active calories, distance
  - Sleep tracking: duration, stages, score (requires Withings Sleep Analyzer
    or Sleep Mat; NOT available from Body Scan)
  - Heart rate during activities

Note: The Withings Body Scan does NOT include a sleep tracker. To get sleep
data from Withings, you need a dedicated Withings Sleep device or compatible
watch. If you only have a Body Scan, use another wearable adapter (Oura,
Garmin, WHOOP) for sleep context.

Auth: shares OAuth 2.0 tokens with the scale adapter (same client app).
Configure with the same client_id/secret and token_path.

API reference: https://developer.withings.com/api-reference/

Configuration:
    wearables:
      withings:
        enabled: true
        client_id: "YOUR_CLIENT_ID"
        client_secret: "YOUR_CLIENT_SECRET"
        access_token: ""       # shared with scale adapter's token_path
        refresh_token: ""
        token_path: "data/withings_token.json"

Usage:
    async with WithingsHealthAdapter(config["wearables"]["withings"]) as adapter:
        context = await adapter.fetch_context(date=datetime.now(timezone.utc))
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from adapters.base import ContextReading, WearableAdapter, utcnow

logger = logging.getLogger(__name__)

_TOKEN_URL = "https://wbsapi.withings.net/v2/oauth2"
_MEASURE_URL = "https://wbsapi.withings.net/v2/measure"
_SLEEP_URL = "https://wbsapi.withings.net/v2/sleep"
_ACTIVITY_URL = "https://wbsapi.withings.net/v2/measure"


class WithingsHealthAdapter(WearableAdapter):
    """
    Fetches Withings activity and sleep data for a given date.

    Endpoints:
      /v2/measure?action=getactivity  → steps, calories, distance
      /v2/sleep?action=getsummary     → sleep stages, duration, score
    """

    ADAPTER_NAME = "withings_health"

    def __init__(self, config: dict):
        super().__init__(config)
        self._client_id: str = config.get("client_id", "")
        self._client_secret: str = config.get("client_secret", "")
        self._access_token: str = config.get("access_token", "")
        self._refresh_token: str = config.get("refresh_token", "")
        self._token_path: Optional[Path] = (
            Path(config["token_path"]) if config.get("token_path") else None
        )
        self._client = None

    # ------------------------------------------------------------------
    # WearableAdapter interface
    # ------------------------------------------------------------------

    async def authenticate(self) -> None:
        """Load tokens and refresh the access token."""
        import httpx  # lazy

        self._load_tokens()

        if not self._refresh_token:
            raise ValueError(
                "Withings refresh_token missing. "
                "Run: body-tracker auth withings"
            )

        self._client = httpx.AsyncClient(timeout=20.0)
        await self._refresh_access_token()
        logger.info("WithingsHealthAdapter authenticated")

    async def fetch_context(self, date: datetime) -> ContextReading:
        """Fetch Withings activity and sleep data for the given date."""
        if self._client is None:
            await self.authenticate()

        date_str = date.strftime("%Y-%m-%d")
        headers = {"Authorization": f"Bearer {self._access_token}"}

        import asyncio
        activity_raw, sleep_raw = await asyncio.gather(
            self._get_activity(date_str, headers),
            self._get_sleep(date_str, headers),
            return_exceptions=True,
        )

        reading = ContextReading(
            date=date.replace(hour=0, minute=0, second=0, microsecond=0),
            source="withings",
            adapter_name=self.ADAPTER_NAME,
        )

        self._parse_activity(activity_raw, reading)
        self._parse_sleep(sleep_raw, reading)

        logger.info(
            "Withings health context for %s: %d metrics",
            date_str, len(reading.available_metrics()),
        )
        return reading

    def is_available(self) -> bool:
        return bool(self._client_id and self._refresh_token)

    # ------------------------------------------------------------------
    # Private: HTTP
    # ------------------------------------------------------------------

    async def _get_activity(self, date_str: str, headers: dict) -> dict | Exception:
        try:
            resp = await self._client.get(
                "https://wbsapi.withings.net/v2/measure",
                params={"action": "getactivity", "startdateymd": date_str,
                        "enddateymd": date_str, "data_fields": "steps,calories,distance"},
                headers=headers,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.warning("Withings activity fetch failed: %s", exc)
            return exc

    async def _get_sleep(self, date_str: str, headers: dict) -> dict | Exception:
        try:
            resp = await self._client.get(
                "https://wbsapi.withings.net/v2/sleep",
                params={"action": "getsummary", "startdateymd": date_str,
                        "enddateymd": date_str},
                headers=headers,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.warning("Withings sleep fetch failed: %s", exc)
            return exc

    async def _refresh_access_token(self) -> None:
        resp = await self._client.post(
            _TOKEN_URL,
            data={
                "action": "requesttoken",
                "grant_type": "refresh_token",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": self._refresh_token,
            },
        )
        if resp.status_code == 401:
            raise RuntimeError(
                "Withings token refresh failed. Re-authorise: body-tracker auth withings"
            )
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") != 0:
            raise RuntimeError(f"Withings token error: {body}")
        token_body = body.get("body", {})
        self._access_token = token_body["access_token"]
        self._refresh_token = token_body["refresh_token"]
        self._save_tokens()

    # ------------------------------------------------------------------
    # Token persistence (shared file with scale adapter)
    # ------------------------------------------------------------------

    def _load_tokens(self) -> None:
        if not self._token_path or not self._token_path.exists():
            return
        try:
            data = json.loads(self._token_path.read_text())
            self._access_token = data.get("access_token", self._access_token)
            self._refresh_token = data.get("refresh_token", self._refresh_token)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not load Withings token file: %s", exc)

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
            logger.warning("Could not save Withings tokens: %s", exc)

    # ------------------------------------------------------------------
    # Parsers
    # ------------------------------------------------------------------

    def _parse_activity(self, raw: dict | Exception, reading: ContextReading) -> None:
        if isinstance(raw, Exception):
            return
        if raw.get("status") != 0:
            return
        activities = raw.get("body", {}).get("activities", [])
        if not activities:
            return
        a = activities[0]
        reading.steps = a.get("steps")
        reading.active_calories_kcal = _safe_int(a.get("calories"))
        if a.get("distance"):
            reading.extras["distance_m"] = float(a["distance"])
        reading.raw_payload = reading.raw_payload or {}
        reading.raw_payload["activity"] = a

    def _parse_sleep(self, raw: dict | Exception, reading: ContextReading) -> None:
        if isinstance(raw, Exception):
            return
        if raw.get("status") != 0:
            return
        series = raw.get("body", {}).get("series", [])
        if not series:
            return
        s = series[0]
        data = s.get("data", {})
        reading.sleep_score = data.get("sleep_score")
        total_s = data.get("total_sleep_time")
        if total_s:
            reading.sleep_duration_h = total_s / 3600
        deep_s = data.get("deep_sleep_duration")
        if deep_s:
            reading.deep_sleep_h = deep_s / 3600
        rem_s = data.get("rem_sleep_duration")
        if rem_s:
            reading.rem_sleep_h = rem_s / 3600
        reading.sleep_efficiency_pct = data.get("sleep_efficiency")
        reading.raw_payload = reading.raw_payload or {}
        reading.raw_payload["sleep"] = s

    async def __aexit__(self, *_):
        if self._client:
            await self._client.aclose()
            self._client = None


def _safe_int(value) -> Optional[int]:
    try:
        return int(round(float(value))) if value is not None else None
    except (TypeError, ValueError):
        return None
