"""
adapters/wearables/whoop.py
============================
WHOOP API v1 adapter — fetches recovery, sleep and strain data.

WHOOP's API focuses on three core concepts mapped directly to ContextReading:
  - Recovery  → readiness_score, hrv_ms, resting_hr_bpm, body_temperature_delta
  - Sleep     → sleep_duration_h, deep_sleep_h, rem_sleep_h, sleep_efficiency_pct
  - Strain    → training_load (day strain 0–21 scale), active_calories_kcal

Auth: OAuth 2.0 (PKCE flow). Access token expires in 3600 s; refresh_token is
long-lived. This adapter refreshes automatically on 401 responses.

API reference: https://developer.whoop.com/api
Register:      https://developer.whoop.com/

Configuration:
    wearables:
      whoop:
        enabled: true
        client_id: "YOUR_CLIENT_ID"
        client_secret: "YOUR_CLIENT_SECRET"
        access_token: "..."
        refresh_token: "..."
        token_path: "data/whoop_token.json"

Usage:
    async with WhoopAdapter(config["wearables"]["whoop"]) as adapter:
        context = await adapter.fetch_context(date=datetime.now(timezone.utc))
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from adapters.base import ContextReading, WearableAdapter, utcnow

logger = logging.getLogger(__name__)

_TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
_BASE_URL = "https://api.prod.whoop.com/developer/v1"


class WhoopAdapter(WearableAdapter):
    """
    Reads WHOOP recovery, sleep and strain data from the WHOOP API v1.

    WHOOP data is cycle-based (not calendar-day-based). This adapter finds
    the cycle that overlaps the requested date and extracts its data.

    Endpoints used:
      /recovery       → recovery score, HRV, resting HR, skin temp
      /sleep          → sleep stages, duration, performance score
      /cycle          → day strain, kilojoule output
    """

    ADAPTER_NAME = "whoop"

    def __init__(self, config: dict):
        super().__init__(config)
        self._client_id: str = config.get("client_id", "")
        self._client_secret: str = config.get("client_secret", "")
        self._access_token: str = config.get("access_token", "")
        self._refresh_token: str = config.get("refresh_token", "")
        self._token_path: Optional[Path] = (
            Path(config["token_path"]) if config.get("token_path") else None
        )
        self._client = None  # httpx.AsyncClient

    # ------------------------------------------------------------------
    # WearableAdapter interface
    # ------------------------------------------------------------------

    async def authenticate(self) -> None:
        """Load tokens from disk and validate."""
        import httpx  # lazy import

        self._load_tokens()

        if not self._refresh_token:
            raise ValueError(
                "WHOOP refresh_token is missing. "
                "Complete the OAuth flow first: body-tracker auth whoop\n"
                "Register at: https://developer.whoop.com/"
            )

        self._client = httpx.AsyncClient(
            base_url=_BASE_URL,
            timeout=20.0,
        )
        # Proactively refresh — WHOOP access tokens expire after 3600 s
        await self._refresh_access_token()
        logger.info("WhoopAdapter authenticated")

    async def fetch_context(self, date: datetime) -> ContextReading:
        """Fetch WHOOP recovery, sleep and strain data for the given date."""
        if self._client is None:
            await self.authenticate()

        # WHOOP cycles start in the evening; search a 24-h window around the date
        start = date.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        params = {
            "start": start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "end": end.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "limit": 5,
        }

        import asyncio
        recovery_raw, sleep_raw, cycle_raw = await asyncio.gather(
            self._get("/recovery", params),
            self._get("/activity/sleep", params),
            self._get("/cycle", params),
            return_exceptions=True,
        )

        reading = ContextReading(
            date=start,
            source="whoop",
            adapter_name=self.ADAPTER_NAME,
        )

        self._parse_recovery(recovery_raw, reading)
        self._parse_sleep(sleep_raw, reading)
        self._parse_cycle(cycle_raw, reading)

        logger.info(
            "WHOOP context for %s: %d metrics",
            date.strftime("%Y-%m-%d"), len(reading.available_metrics()),
        )
        return reading

    def is_available(self) -> bool:
        return bool(self._client_id and self._refresh_token)

    # ------------------------------------------------------------------
    # Private: HTTP
    # ------------------------------------------------------------------

    async def _get(self, path: str, params: dict | None = None) -> dict | Exception:
        try:
            resp = await self._client.get(
                path,
                params=params or {},
                headers={"Authorization": f"Bearer {self._access_token}"},
            )
            if resp.status_code == 401:
                await self._refresh_access_token()
                resp = await self._client.get(
                    path,
                    params=params or {},
                    headers={"Authorization": f"Bearer {self._access_token}"},
                )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.warning("WHOOP %s failed: %s", path, exc)
            return exc

    async def _refresh_access_token(self) -> None:
        """Exchange refresh_token for a new access + refresh token pair."""
        import httpx  # lazy

        async with httpx.AsyncClient(timeout=20.0) as tmp_client:
            resp = await tmp_client.post(
                _TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "scope": "read:recovery read:sleep read:workout read:cycles "
                             "read:body_measurement",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        if resp.status_code in (400, 401):
            raise RuntimeError(
                "WHOOP token refresh failed — the refresh token may be expired.\n"
                "Re-authorise at: https://app.whoop.com/settings/integrations\n"
                "Or run: body-tracker auth whoop"
            )

        resp.raise_for_status()
        body = resp.json()
        self._access_token = body["access_token"]
        self._refresh_token = body.get("refresh_token", self._refresh_token)
        self._save_tokens()
        logger.debug("WHOOP access token refreshed")

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
            logger.warning("Could not load WHOOP token file: %s", exc)

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
            logger.warning("Could not save WHOOP tokens: %s", exc)

    # ------------------------------------------------------------------
    # Parsers
    # ------------------------------------------------------------------

    def _parse_recovery(self, raw: dict | Exception, reading: ContextReading) -> None:
        if isinstance(raw, Exception):
            return
        records = raw.get("records", [raw]) if isinstance(raw, dict) else raw
        if not records:
            return
        r = records[0]
        score = r.get("score", {}) or {}
        reading.readiness_score = _safe_int(score.get("recovery_score"))
        reading.hrv_ms = score.get("hrv_rmssd_milli")
        reading.resting_hr_bpm = _safe_int(score.get("resting_heart_rate"))
        reading.body_temperature_delta = score.get("skin_temp_celsius")
        reading.raw_payload = reading.raw_payload or {}
        reading.raw_payload["recovery"] = r

    def _parse_sleep(self, raw: dict | Exception, reading: ContextReading) -> None:
        if isinstance(raw, Exception):
            return
        records = raw.get("records", [raw]) if isinstance(raw, dict) else raw
        if not records:
            return
        s = records[0]
        score = s.get("score", {}) or {}
        total_in_bed_ms = s.get("end") and s.get("start") and _duration_ms(
            s["start"], s["end"]
        )
        stage_summary = score.get("stage_summary", {}) or {}
        total_sleep_ms = stage_summary.get("total_in_sleep_time_milli")
        if total_sleep_ms:
            reading.sleep_duration_h = total_sleep_ms / 3_600_000
        reading.sleep_efficiency_pct = score.get("sleep_efficiency_percentage")
        reading.sleep_score = _safe_int(score.get("sleep_performance_percentage"))
        deep_ms = stage_summary.get("total_slow_wave_sleep_time_milli")
        reading.deep_sleep_h = deep_ms / 3_600_000 if deep_ms else None
        rem_ms = stage_summary.get("total_rem_sleep_time_milli")
        reading.rem_sleep_h = rem_ms / 3_600_000 if rem_ms else None
        latency_ms = stage_summary.get("sleep_onset_latency_time_milli")
        reading.sleep_latency_min = latency_ms // 60_000 if latency_ms else None
        reading.raw_payload = reading.raw_payload or {}
        reading.raw_payload["sleep"] = s

    def _parse_cycle(self, raw: dict | Exception, reading: ContextReading) -> None:
        if isinstance(raw, Exception):
            return
        records = raw.get("records", [raw]) if isinstance(raw, dict) else raw
        if not records:
            return
        c = records[0]
        score = c.get("score", {}) or {}
        # WHOOP strain is 0–21; map to training_load directly
        reading.training_load = score.get("strain")
        kj = score.get("kilojoule")
        if kj is not None:
            # Convert kilojoules to kcal (1 kcal ≈ 4.184 kJ)
            reading.active_calories_kcal = _safe_int(kj / 4.184)
        avg_hr = score.get("average_heart_rate")
        if avg_hr is not None:
            reading.extras["avg_hr_bpm"] = float(avg_hr)
        max_hr = score.get("max_heart_rate")
        if max_hr is not None:
            reading.extras["max_hr_bpm"] = float(max_hr)
        reading.raw_payload = reading.raw_payload or {}
        reading.raw_payload["cycle"] = c

    async def __aexit__(self, *_):
        if self._client:
            await self._client.aclose()
            self._client = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_int(value) -> Optional[int]:
    try:
        return int(round(float(value))) if value is not None else None
    except (TypeError, ValueError):
        return None


def _duration_ms(start_str: str, end_str: str) -> Optional[int]:
    """Return duration in milliseconds between two ISO-8601 strings."""
    try:
        fmt = "%Y-%m-%dT%H:%M:%S.%fZ"
        t0 = datetime.strptime(start_str, fmt).replace(tzinfo=timezone.utc)
        t1 = datetime.strptime(end_str, fmt).replace(tzinfo=timezone.utc)
        return int((t1 - t0).total_seconds() * 1000)
    except Exception:
        return None
