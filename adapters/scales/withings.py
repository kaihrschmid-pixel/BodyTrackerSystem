"""
adapters/scales/withings.py
===========================
Withings Health API v2 adapter — reads body composition measurements
from Withings connected scales via the official cloud API.

Supports all Withings smart scales, with best coverage from:
  - Body Scan (BIA + segmental + ECG + nerve health)
  - Body Comp   (BIA + PWV + heart rate)
  - Body+        (BIA: weight, fat, muscle, bone, water)
  - Body         (weight + BMI only)

Metric availability by model
-----------------------------
All models:
  weight_kg, body_fat_pct, bone_mass_kg, muscle_mass_kg, water_pct

Body+ and above:
  visceral_fat_index, heart_rate_bpm

Body Comp and above:
  pulse_wave_velocity, vascular_age

Body Scan only (8-electrode segmental BIA + ECG):
  phase_angle, ecm_bcm_ratio,
  muscle_mass_left_arm_kg, muscle_mass_right_arm_kg,
  muscle_mass_left_leg_kg, muscle_mass_right_leg_kg,
  muscle_mass_torso_kg, nerve_health_score

OAuth 2.0 note
--------------
Access tokens expire after ~3 hours. This adapter auto-refreshes using
the stored refresh_token and persists the new tokens to `token_path`.
If the refresh fails (revoked token, changed password), a RuntimeError is
raised with a link to the Withings developer portal to re-authorise.

Configuration (in config.yaml):
    hardware:
      scale:
        adapter: "withings"
        client_id: "YOUR_CLIENT_ID"
        client_secret: "YOUR_CLIENT_SECRET"
        access_token: "..."        # persisted after first auth
        refresh_token: "..."       # persisted after first auth
        token_path: "data/withings_token.json"

First-time authorisation: complete the OAuth flow manually or with
`body-tracker auth withings` and the tokens will be stored automatically.
See https://developer.withings.com/developer-guide/v3/integration-guide/public-health-data-api/get-access/
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from adapters.base import ScaleAdapter, ScaleReading, utcnow

logger = logging.getLogger(__name__)

# Withings API v2 endpoints
_TOKEN_URL = "https://wbsapi.withings.net/v2/oauth2"
_MEASURE_URL = "https://wbsapi.withings.net/v2/measure"

# Withings measuretype → (ScaleReading field name, conversion factor)
# All Withings values are integers; multiply by 10**unit to get the real value.
# The conversion is applied in _convert_measure().
#
# Note on conflicts in the official docs:
#   - meastype 88  = Bone Mass (kg)  — NOT BMR (Withings does not expose BMR
#                                       as a standard getmeas type)
#   - meastype 170 = Visceral Fat Index — ECM/BCM ratio uses the same endpoint
#                    but is returned in a separate "more" payload on Body Scan;
#                    we map 170 → visceral_fat_index and handle ecm_bcm_ratio
#                    via the "more" response field if present.
_MEASTYPE_MAP: dict[int, str] = {
    1:   "weight_kg",
    6:   "body_fat_pct",
    11:  "heart_rate_bpm",
    76:  "muscle_mass_kg",
    77:  "water_pct",
    88:  "bone_mass_kg",
    91:  "pulse_wave_velocity",
    123: "vascular_age",
    135: "nerve_health_score",
    168: "phase_angle",
    170: "visceral_fat_index",
    174: "muscle_mass_left_arm_kg",
    175: "muscle_mass_right_arm_kg",
    176: "muscle_mass_left_leg_kg",
    177: "muscle_mass_right_leg_kg",
    178: "muscle_mass_torso_kg",
}

# Fields that are integers in ScaleReading (not float)
_INT_FIELDS = {"heart_rate_bpm", "vascular_age"}


class WithingsScaleAdapter(ScaleAdapter):
    """
    Reads body composition data from Withings Health API v2.

    Handles OAuth 2.0 token refresh transparently — if the access token
    is expired the adapter refreshes it during connect() and persists
    the new credentials to token_path.
    """

    ADAPTER_NAME = "withings"

    def __init__(self, config: dict):
        super().__init__(config)
        self._client_id: str = config.get("client_id", "")
        self._client_secret: str = config.get("client_secret", "")
        self._access_token: str = config.get("access_token", "")
        self._refresh_token: str = config.get("refresh_token", "")
        self._token_path: Optional[Path] = (
            Path(config["token_path"]) if config.get("token_path") else None
        )
        # httpx.AsyncClient — created in connect(), closed in disconnect()
        self._client = None

    # ------------------------------------------------------------------
    # ScaleAdapter interface
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """
        Load persisted tokens (if token_path exists), then validate /
        refresh the access token so read() can proceed immediately.
        """
        import httpx  # lazy import — keeps base package lightweight

        # Load fresher tokens from disk if available
        self._load_tokens_from_file()

        if not self._refresh_token:
            raise ValueError(
                "Withings refresh_token is missing. "
                "Complete the OAuth flow first: body-tracker auth withings\n"
                "Docs: https://developer.withings.com/developer-guide/v3/integration-guide/"
                "public-health-data-api/get-access/"
            )

        self._client = httpx.AsyncClient(timeout=20.0)

        # Proactively refresh — access tokens last only ~3 h, so refreshing
        # on every connect() is cheap and avoids mid-session failures.
        await self._refresh_access_token()
        logger.info("WithingsScaleAdapter connected — tokens valid")

    async def read(self) -> ScaleReading:
        """
        Fetch the most recent measurement group from the last 24 hours.

        Raises RuntimeError if no measurement is found in that window or
        if the API returns an authentication error.
        """
        if self._client is None:
            raise RuntimeError("Call connect() before read()")

        now = int(time.time())
        yesterday = now - 86_400

        params = {
            "action": "getmeas",
            "meastype": ",".join(str(t) for t in _MEASTYPE_MAP),
            "category": 1,          # real measurements (not targets)
            "startdate": yesterday,
            "enddate": now,
            "lastupdate": yesterday,
        }
        headers = {"Authorization": f"Bearer {self._access_token}"}

        resp = await self._client.get(_MEASURE_URL, params=params, headers=headers)

        if resp.status_code == 401:
            raise RuntimeError(
                "Withings API returned 401 Unauthorized. "
                "Your access token may be revoked. Re-authorise at:\n"
                "https://account.withings.com/connectionuser/account_manager_authorize"
            )

        resp.raise_for_status()
        body = resp.json()

        if body.get("status") != 0:
            error_msg = body.get("error", "unknown error")
            raise RuntimeError(
                f"Withings API error (status={body.get('status')}): {error_msg}"
            )

        groups = body.get("body", {}).get("measuregrps", [])
        if not groups:
            raise RuntimeError(
                "No Withings measurements found in the last 24 hours. "
                "Step on your scale and sync with the Withings app first."
            )

        # Use the most recent group (API returns newest first)
        latest = groups[0]
        return self._parse_measuregrp(latest)

    async def disconnect(self) -> None:
        """Close the underlying HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def is_available(self) -> bool:
        """True when both client_id and refresh_token are configured."""
        return bool(self._client_id and self._refresh_token)

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    def _load_tokens_from_file(self) -> None:
        """Overwrite in-memory tokens with fresher values from token_path."""
        if not self._token_path or not self._token_path.exists():
            return
        try:
            data = json.loads(self._token_path.read_text())
            self._access_token = data.get("access_token", self._access_token)
            self._refresh_token = data.get("refresh_token", self._refresh_token)
            logger.debug("Withings tokens loaded from %s", self._token_path)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not load Withings token file: %s", exc)

    async def _refresh_access_token(self) -> None:
        """
        Exchange refresh_token for a new access_token + refresh_token pair
        and persist to token_path.

        Raises RuntimeError with a user-friendly re-auth link on failure.
        """
        payload = {
            "action": "requesttoken",
            "grant_type": "refresh_token",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "refresh_token": self._refresh_token,
        }

        resp = await self._client.post(_TOKEN_URL, data=payload)

        if resp.status_code == 401:
            raise RuntimeError(
                "Withings token refresh failed (401). "
                "The refresh token may be expired or revoked. "
                "Re-authorise your account at:\n"
                "https://account.withings.com/connectionuser/account_manager_authorize"
            )

        resp.raise_for_status()
        body = resp.json()

        if body.get("status") != 0:
            raise RuntimeError(
                f"Withings token refresh failed (status={body.get('status')}). "
                "Re-authorise your account at:\n"
                "https://account.withings.com/connectionuser/account_manager_authorize"
            )

        token_body = body.get("body", {})
        new_access = token_body.get("access_token")
        new_refresh = token_body.get("refresh_token")

        if not new_access or not new_refresh:
            raise RuntimeError(
                "Withings returned an incomplete token response. "
                "Please re-authorise: body-tracker auth withings"
            )

        self._access_token = new_access
        self._refresh_token = new_refresh
        self._save_tokens_to_file()
        logger.info("Withings access token refreshed successfully")

    def _save_tokens_to_file(self) -> None:
        """Persist current access + refresh tokens to token_path."""
        if not self._token_path:
            return
        try:
            self._token_path.parent.mkdir(parents=True, exist_ok=True)
            self._token_path.write_text(
                json.dumps(
                    {
                        "access_token": self._access_token,
                        "refresh_token": self._refresh_token,
                        "updated_at": utcnow().isoformat(),
                    },
                    indent=2,
                )
            )
            logger.debug("Withings tokens saved to %s", self._token_path)
        except OSError as exc:
            logger.warning("Could not save Withings tokens to %s: %s", self._token_path, exc)

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _parse_measuregrp(self, grp: dict) -> ScaleReading:
        """
        Convert a single Withings measuregrp dict into a ScaleReading.

        Withings encodes real values as:  real_value = value * 10^unit
        e.g. {"value": 824, "unit": -1}  →  82.4 kg
        """
        # Timestamp from the measurement group
        ts = grp.get("date")
        if ts:
            recorded_at = datetime.fromtimestamp(ts, tz=timezone.utc)
        else:
            recorded_at = utcnow()

        # Build a type → real_value lookup from the measures list
        measures: dict[int, float] = {}
        for m in grp.get("measures", []):
            mtype = m.get("type")
            value = m.get("value")
            unit = m.get("unit", 0)
            if mtype is not None and value is not None:
                measures[mtype] = value * (10 ** unit)

        # We need at least weight
        if 1 not in measures:
            raise ValueError(
                f"Withings measurement group {grp.get('grpid')} has no weight (meastype 1). "
                "Cannot create a ScaleReading without weight_kg."
            )

        kwargs: dict = {
            "recorded_at": recorded_at,
            "weight_kg": measures[1],
            "adapter_name": self.ADAPTER_NAME,
            "raw_payload": grp,
        }

        # Map known meattypes to ScaleReading fields
        for mtype, field_name in _MEASTYPE_MAP.items():
            if mtype == 1:  # already handled
                continue
            val = measures.get(mtype)
            if val is not None:
                kwargs[field_name] = int(round(val)) if field_name in _INT_FIELDS else val

        # ECM/BCM ratio — Withings Body Scan exposes this in the "more" sub-object
        more = grp.get("more", {}) or {}
        if "ecm_bcm" in more:
            kwargs["ecm_bcm_ratio"] = float(more["ecm_bcm"])

        reading = ScaleReading(**kwargs)

        logger.debug(
            "Withings reading: weight=%.1f kg, metrics=%s",
            reading.weight_kg,
            reading.available_metrics(),
        )
        return reading
