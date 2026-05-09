"""
tests/adapters/test_withings.py
================================
Tests for the WithingsScaleAdapter.
Run with: pytest tests/ -v

HTTP calls are mocked with unittest.mock.AsyncMock — no network required.
"""

from __future__ import annotations

import json
import time
from datetime import timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from adapters.scales.withings import WithingsScaleAdapter, _MEASTYPE_MAP, _MEASURE_URL, _TOKEN_URL


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_adapter(tmp_path: Path, **overrides) -> WithingsScaleAdapter:
    """Return a configured adapter; token_path points to tmp_path."""
    config = {
        "client_id": "test_client_id",
        "client_secret": "test_client_secret",
        "access_token": "test_access_token",
        "refresh_token": "test_refresh_token",
        "token_path": str(tmp_path / "withings_token.json"),
    }
    config.update(overrides)
    return WithingsScaleAdapter(config)


def _measure_response(measures: list[dict], grpid: int = 12345, date: int = 1_700_000_000) -> dict:
    """Build a minimal valid Withings /measure?action=getmeas response body."""
    return {
        "status": 0,
        "body": {
            "measuregrps": [
                {
                    "grpid": grpid,
                    "date": date,
                    "measures": measures,
                }
            ]
        },
    }


def _mock_http_response(json_data: dict, status_code: int = 200) -> MagicMock:
    """Return a mock httpx.Response-like object."""
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.json.return_value = json_data
    mock_resp.raise_for_status = MagicMock()  # no-op for 200
    return mock_resp


# ---------------------------------------------------------------------------
# 1. is_available
# ---------------------------------------------------------------------------

class TestIsAvailable:
    def test_is_available_with_tokens(self):
        adapter = WithingsScaleAdapter({
            "client_id": "cid",
            "client_secret": "csec",
            "access_token": "at",
            "refresh_token": "rt",
        })
        assert adapter.is_available() is True

    def test_is_available_without_tokens(self):
        """Missing client_id or refresh_token → not available."""
        no_client_id = WithingsScaleAdapter({
            "client_id": "",
            "client_secret": "csec",
            "refresh_token": "rt",
        })
        assert no_client_id.is_available() is False

        no_refresh = WithingsScaleAdapter({
            "client_id": "cid",
            "client_secret": "csec",
            "refresh_token": "",
        })
        assert no_refresh.is_available() is False

        empty = WithingsScaleAdapter({})
        assert empty.is_available() is False


# ---------------------------------------------------------------------------
# 2 + 3. Parsing
# ---------------------------------------------------------------------------

class TestParseMeasureGrp:
    """Unit-test _parse_measuregrp() directly without any HTTP mocking."""

    def _adapter(self) -> WithingsScaleAdapter:
        return WithingsScaleAdapter({
            "client_id": "cid",
            "client_secret": "csec",
            "access_token": "at",
            "refresh_token": "rt",
        })

    def test_parse_body_scan_response(self):
        """
        Realistic Body Scan response: weight, fat, muscle, phase_angle,
        heart_rate, bone, water, segmental muscles, nerve health, etc.
        """
        adapter = self._adapter()
        grp = {
            "grpid": 12345,
            "date": 1_700_000_000,
            "measures": [
                {"type": 1,   "value": 824,  "unit": -1},   # 82.4 kg
                {"type": 6,   "value": 182,  "unit": -1},   # 18.2 %
                {"type": 76,  "value": 631,  "unit": -1},   # 63.1 kg muscle
                {"type": 77,  "value": 572,  "unit": -1},   # 57.2 % water
                {"type": 88,  "value": 32,   "unit": -1},   # 3.2 kg bone
                {"type": 11,  "value": 62,   "unit": 0},    # 62 bpm
                {"type": 91,  "value": 75,   "unit": -1},   # 7.5 m/s PWV
                {"type": 123, "value": 35,   "unit": 0},    # vascular age 35
                {"type": 135, "value": 920,  "unit": -2},   # 9.20 nerve score
                {"type": 168, "value": 64,   "unit": -1},   # 6.4° phase angle
                {"type": 170, "value": 5,    "unit": 0},    # visceral fat 5
                {"type": 174, "value": 40,   "unit": -1},   # 4.0 kg L arm
                {"type": 175, "value": 41,   "unit": -1},   # 4.1 kg R arm
                {"type": 176, "value": 160,  "unit": -1},   # 16.0 kg L leg
                {"type": 177, "value": 161,  "unit": -1},   # 16.1 kg R leg
                {"type": 178, "value": 285,  "unit": -1},   # 28.5 kg torso
            ],
        }
        reading = adapter._parse_measuregrp(grp)

        assert reading.weight_kg == pytest.approx(82.4)
        assert reading.body_fat_pct == pytest.approx(18.2)
        assert reading.muscle_mass_kg == pytest.approx(63.1)
        assert reading.water_pct == pytest.approx(57.2)
        assert reading.bone_mass_kg == pytest.approx(3.2)
        assert reading.heart_rate_bpm == 62          # int field
        assert reading.pulse_wave_velocity == pytest.approx(7.5)
        assert reading.vascular_age == 35             # int field
        assert reading.nerve_health_score == pytest.approx(9.20)
        assert reading.phase_angle == pytest.approx(6.4)
        assert reading.visceral_fat_index == pytest.approx(5.0)
        assert reading.muscle_mass_left_arm_kg == pytest.approx(4.0)
        assert reading.muscle_mass_right_arm_kg == pytest.approx(4.1)
        assert reading.muscle_mass_left_leg_kg == pytest.approx(16.0)
        assert reading.muscle_mass_right_leg_kg == pytest.approx(16.1)
        assert reading.muscle_mass_torso_kg == pytest.approx(28.5)
        assert reading.adapter_name == "withings"

        # Timestamp derived from Unix epoch in grp["date"]
        from datetime import datetime
        expected_dt = datetime.fromtimestamp(1_700_000_000, tz=timezone.utc)
        assert reading.recorded_at == expected_dt

    def test_parse_minimal_response(self):
        """Only weight_kg present — all other fields remain None."""
        adapter = self._adapter()
        grp = {
            "grpid": 99,
            "date": 1_700_000_000,
            "measures": [
                {"type": 1, "value": 700, "unit": -1},  # 70.0 kg
            ],
        }
        reading = adapter._parse_measuregrp(grp)

        assert reading.weight_kg == pytest.approx(70.0)
        assert reading.body_fat_pct is None
        assert reading.muscle_mass_kg is None
        assert reading.bone_mass_kg is None
        assert reading.water_pct is None
        assert reading.phase_angle is None
        assert reading.heart_rate_bpm is None
        assert reading.vascular_age is None
        assert reading.nerve_health_score is None
        assert reading.muscle_mass_left_arm_kg is None
        assert reading.extras == {}


# ---------------------------------------------------------------------------
# 4. Token refresh saves to file
# ---------------------------------------------------------------------------

class TestTokenRefresh:
    @pytest.mark.asyncio
    async def test_token_refresh_saves_to_file(self, tmp_path):
        """
        After a successful token refresh, the new tokens must be persisted
        to token_path as valid JSON.
        """
        adapter = _make_adapter(tmp_path)

        new_token_response = {
            "status": 0,
            "body": {
                "access_token": "new_access_abc",
                "refresh_token": "new_refresh_xyz",
            },
        }

        mock_post_resp = _mock_http_response(new_token_response)

        import httpx
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=mock_post_resp)
        adapter._client = mock_client

        await adapter._refresh_access_token()

        # In-memory tokens updated
        assert adapter._access_token == "new_access_abc"
        assert adapter._refresh_token == "new_refresh_xyz"

        # Tokens persisted to disk
        token_file = tmp_path / "withings_token.json"
        assert token_file.exists(), "token_path file should have been created"

        saved = json.loads(token_file.read_text())
        assert saved["access_token"] == "new_access_abc"
        assert saved["refresh_token"] == "new_refresh_xyz"
        assert "updated_at" in saved


# ---------------------------------------------------------------------------
# 5. Auth error on read()
# ---------------------------------------------------------------------------

class TestReadErrors:
    @pytest.mark.asyncio
    async def test_read_raises_on_auth_error(self, tmp_path):
        """
        A 401 response from the measure endpoint must raise RuntimeError
        with a helpful re-authorisation message.
        """
        adapter = _make_adapter(tmp_path)

        mock_401 = MagicMock()
        mock_401.status_code = 401
        mock_401.raise_for_status = MagicMock()

        import httpx
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_401)
        adapter._client = mock_client

        with pytest.raises(RuntimeError, match="401"):
            await adapter.read()

    @pytest.mark.asyncio
    async def test_read_raises_when_no_measurements(self, tmp_path):
        """
        An empty measuregrps list (no recent measurement) should raise
        a descriptive RuntimeError — not a KeyError / index error.
        """
        adapter = _make_adapter(tmp_path)

        empty_response = {"status": 0, "body": {"measuregrps": []}}
        mock_resp = _mock_http_response(empty_response)

        import httpx
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_resp)
        adapter._client = mock_client

        with pytest.raises(RuntimeError, match="No Withings measurements"):
            await adapter.read()
