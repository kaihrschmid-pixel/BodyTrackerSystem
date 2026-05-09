"""
tests/adapters/test_whoop.py
=============================
Tests for the WhoopAdapter.
Run with: pytest tests/ -v

All HTTP calls are mocked — no network or WHOOP account required.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from adapters.wearables.whoop import WhoopAdapter, _safe_int, _duration_ms


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_adapter(tmp_path: Path, **overrides) -> WhoopAdapter:
    config = {
        "client_id": "test_client_id",
        "client_secret": "test_client_secret",
        "access_token": "test_access_token",
        "refresh_token": "test_refresh_token",
        "token_path": str(tmp_path / "whoop_token.json"),
    }
    config.update(overrides)
    return WhoopAdapter(config)


def _mock_response(json_data, status_code: int = 200) -> MagicMock:
    mock = MagicMock()
    mock.status_code = status_code
    mock.json.return_value = json_data
    mock.raise_for_status = MagicMock()
    return mock


_TEST_DATE = datetime(2025, 6, 1, 0, 0, 0, tzinfo=timezone.utc)

_RECOVERY_RESPONSE = {
    "records": [
        {
            "score": {
                "recovery_score": 78,
                "hrv_rmssd_milli": 52.4,
                "resting_heart_rate": 56,
                "skin_temp_celsius": 0.3,
            }
        }
    ]
}

_SLEEP_RESPONSE = {
    "records": [
        {
            "start": "2025-05-31T22:30:00.000Z",
            "end":   "2025-06-01T06:30:00.000Z",
            "score": {
                "sleep_efficiency_percentage": 88.0,
                "sleep_performance_percentage": 82,
                "stage_summary": {
                    "total_in_sleep_time_milli": 26_100_000,    # 7.25 h
                    "total_slow_wave_sleep_time_milli": 5_400_000,   # 1.5 h
                    "total_rem_sleep_time_milli": 6_000_000,    # 1.667 h
                    "sleep_onset_latency_time_milli": 480_000,  # 8 min
                },
            },
        }
    ]
}

_CYCLE_RESPONSE = {
    "records": [
        {
            "score": {
                "strain": 14.2,
                "kilojoule": 9_630.0,       # ~2300 kcal
                "average_heart_rate": 68,
                "max_heart_rate": 172,
            }
        }
    ]
}


# ---------------------------------------------------------------------------
# 1. is_available
# ---------------------------------------------------------------------------

class TestIsAvailable:
    def test_is_available_with_credentials(self, tmp_path):
        assert _make_adapter(tmp_path).is_available() is True

    def test_is_available_no_client_id(self, tmp_path):
        assert _make_adapter(tmp_path, client_id="").is_available() is False

    def test_is_available_no_refresh_token(self, tmp_path):
        assert _make_adapter(tmp_path, refresh_token="").is_available() is False


# ---------------------------------------------------------------------------
# 2. Helper functions
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_safe_int_normal(self):
        assert _safe_int(78.9) == 79
        assert _safe_int(56) == 56
        assert _safe_int(None) is None
        assert _safe_int("bad") is None

    def test_duration_ms_valid(self):
        result = _duration_ms("2025-05-31T22:30:00.000Z", "2025-06-01T06:30:00.000Z")
        assert result == 8 * 3_600_000   # 8 hours in ms

    def test_duration_ms_invalid(self):
        assert _duration_ms("not a date", "also not") is None


# ---------------------------------------------------------------------------
# 3. Token refresh
# ---------------------------------------------------------------------------

class TestTokenRefresh:
    @pytest.mark.asyncio
    async def test_token_refresh_saves_to_file(self, tmp_path):
        adapter = _make_adapter(tmp_path)

        refresh_body = {
            "access_token": "new_access_whoop",
            "refresh_token": "new_refresh_whoop",
        }

        import httpx
        # _refresh_access_token opens its own AsyncClient context manager
        mock_resp = _mock_response(refresh_body)
        mock_client_instance = AsyncMock()
        mock_client_instance.post = AsyncMock(return_value=mock_resp)
        mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client_instance.__aexit__ = AsyncMock(return_value=False)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("adapters.wearables.whoop.httpx.AsyncClient",
                       lambda **kw: mock_client_instance)
            await adapter._refresh_access_token()

        assert adapter._access_token == "new_access_whoop"
        token_file = tmp_path / "whoop_token.json"
        assert token_file.exists()
        saved = json.loads(token_file.read_text())
        assert saved["access_token"] == "new_access_whoop"

    @pytest.mark.asyncio
    async def test_token_refresh_raises_on_401(self, tmp_path):
        adapter = _make_adapter(tmp_path)

        mock_resp = _mock_response({}, status_code=401)
        mock_client_instance = AsyncMock()
        mock_client_instance.post = AsyncMock(return_value=mock_resp)
        mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client_instance.__aexit__ = AsyncMock(return_value=False)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("adapters.wearables.whoop.httpx.AsyncClient",
                       lambda **kw: mock_client_instance)
            with pytest.raises(RuntimeError, match="refresh token may be expired"):
                await adapter._refresh_access_token()


# ---------------------------------------------------------------------------
# 4. Parsing fetch_context
# ---------------------------------------------------------------------------

class TestFetchContext:
    @pytest.mark.asyncio
    async def test_parse_full_response(self, tmp_path):
        """Recovery, sleep and cycle all parsed into ContextReading."""
        adapter = _make_adapter(tmp_path)

        import httpx
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(side_effect=[
            _mock_response(_RECOVERY_RESPONSE),
            _mock_response(_SLEEP_RESPONSE),
            _mock_response(_CYCLE_RESPONSE),
        ])
        adapter._client = mock_client

        reading = await adapter.fetch_context(_TEST_DATE)

        # Recovery
        assert reading.readiness_score == 78
        assert reading.hrv_ms == pytest.approx(52.4)
        assert reading.resting_hr_bpm == 56
        assert reading.body_temperature_delta == pytest.approx(0.3)

        # Sleep
        assert reading.sleep_duration_h == pytest.approx(7.25)
        assert reading.sleep_efficiency_pct == pytest.approx(88.0)
        assert reading.sleep_score == 82
        assert reading.deep_sleep_h == pytest.approx(1.5)
        assert reading.rem_sleep_h == pytest.approx(100 / 60, abs=0.01)
        assert reading.sleep_latency_min == 8

        # Strain / cycle
        assert reading.training_load == pytest.approx(14.2)
        assert reading.active_calories_kcal is not None   # ~2300 kcal
        assert reading.extras.get("avg_hr_bpm") == pytest.approx(68.0)
        assert reading.extras.get("max_hr_bpm") == pytest.approx(172.0)

        assert reading.source == "whoop"

    @pytest.mark.asyncio
    async def test_partial_response_no_cycle(self, tmp_path):
        """Missing cycle data leaves training_load as None but rest is populated."""
        adapter = _make_adapter(tmp_path)

        import httpx
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(side_effect=[
            _mock_response(_RECOVERY_RESPONSE),
            _mock_response(_SLEEP_RESPONSE),
            _mock_response({"records": []}),    # empty cycle
        ])
        adapter._client = mock_client

        reading = await adapter.fetch_context(_TEST_DATE)
        assert reading.readiness_score == 78
        assert reading.training_load is None
