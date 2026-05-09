"""
tests/adapters/test_fitbit.py
==============================
Tests for the FitbitAdapter.
Run with: pytest tests/ -v

All HTTP calls are mocked — no network or Fitbit account required.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from adapters.wearables.fitbit import FitbitAdapter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_adapter(tmp_path: Path, **overrides) -> FitbitAdapter:
    config = {
        "client_id": "test_client_id",
        "client_secret": "test_client_secret",
        "access_token": "test_access_token",
        "refresh_token": "test_refresh_token",
        "token_path": str(tmp_path / "fitbit_token.json"),
        "user_id": "-",
    }
    config.update(overrides)
    return FitbitAdapter(config)


def _mock_response(json_data, status_code: int = 200) -> MagicMock:
    mock = MagicMock()
    mock.status_code = status_code
    mock.json.return_value = json_data
    mock.raise_for_status = MagicMock()
    return mock


_TEST_DATE = datetime(2025, 6, 1, 0, 0, 0, tzinfo=timezone.utc)

_SLEEP_RESPONSE = {
    "sleep": [
        {
            "isMainSleep": True,
            "duration": 25_200_000,          # 7h in ms
            "efficiency": 91,
            "minutesToFallAsleep": 8,
            "levels": {
                "summary": {
                    "deep":  {"minutes": 90},
                    "rem":   {"minutes": 100},
                    "light": {"minutes": 210},
                    "wake":  {"minutes": 20},
                }
            },
        }
    ],
    "summary": {"totalMinutesAsleep": 400},
}

_HR_RESPONSE = {
    "activities-heart": [
        {
            "value": {
                "restingHeartRate": 55,
                "heartRateZones": [
                    {"name": "Out of Range", "minutes": 1200},
                    {"name": "Fat Burn",     "minutes": 45},
                    {"name": "Cardio",       "minutes": 30},
                    {"name": "Peak",         "minutes": 5},
                ],
            }
        }
    ]
}

_ACTIVITY_RESPONSE = {
    "summary": {
        "steps": 11234,
        "activityCalories": 620,
        "caloriesOut": 2440,
        "veryActiveMinutes": 35,
        "fairlyActiveMinutes": 20,
    }
}

_HRV_RESPONSE = {
    "hrv": [
        {"value": {"dailyRmssd": 42.3, "deepRmssd": 38.1}}
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
# 2. Token refresh saves to file
# ---------------------------------------------------------------------------

class TestTokenRefresh:
    @pytest.mark.asyncio
    async def test_token_refresh_saves_to_file(self, tmp_path):
        adapter = _make_adapter(tmp_path)

        refresh_response = {
            "access_token": "new_access_abc",
            "refresh_token": "new_refresh_xyz",
        }

        import httpx
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=_mock_response(refresh_response))
        adapter._client = mock_client

        await adapter._refresh_access_token()

        assert adapter._access_token == "new_access_abc"
        assert adapter._refresh_token == "new_refresh_xyz"

        token_file = tmp_path / "fitbit_token.json"
        assert token_file.exists()
        saved = json.loads(token_file.read_text())
        assert saved["access_token"] == "new_access_abc"
        assert saved["refresh_token"] == "new_refresh_xyz"

    @pytest.mark.asyncio
    async def test_token_refresh_raises_on_401(self, tmp_path):
        adapter = _make_adapter(tmp_path)

        mock_401 = _mock_response({}, status_code=401)

        import httpx
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=mock_401)
        adapter._client = mock_client

        with pytest.raises(RuntimeError, match="refresh token may be expired"):
            await adapter._refresh_access_token()


# ---------------------------------------------------------------------------
# 3. Parsing fetch_context
# ---------------------------------------------------------------------------

class TestFetchContext:
    @pytest.mark.asyncio
    async def test_parse_full_response(self, tmp_path):
        """All four endpoints parsed into a single ContextReading."""
        adapter = _make_adapter(tmp_path)

        import httpx
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(side_effect=[
            _mock_response(_SLEEP_RESPONSE),
            _mock_response(_HR_RESPONSE),
            _mock_response(_ACTIVITY_RESPONSE),
            _mock_response(_HRV_RESPONSE),
        ])
        adapter._client = mock_client

        reading = await adapter.fetch_context(_TEST_DATE)

        # Sleep
        assert reading.sleep_duration_h == pytest.approx(7.0)
        assert reading.sleep_efficiency_pct == 91
        assert reading.sleep_latency_min == 8
        assert reading.deep_sleep_h == pytest.approx(90 / 60)
        assert reading.rem_sleep_h == pytest.approx(100 / 60)

        # Heart rate
        assert reading.resting_hr_bpm == 55
        assert "hr_zone_fat_burn_min" in reading.extras

        # Activity
        assert reading.steps == 11234
        assert reading.active_calories_kcal == 620
        assert reading.total_calories_kcal == 2440
        assert reading.active_minutes == 55   # 35 very + 20 fairly

        # HRV
        assert reading.hrv_ms == pytest.approx(42.3)

        assert reading.source == "fitbit"

    @pytest.mark.asyncio
    async def test_partial_data_no_hrv(self, tmp_path):
        """HRV endpoint returning empty list leaves hrv_ms as None."""
        adapter = _make_adapter(tmp_path)

        import httpx
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(side_effect=[
            _mock_response(_SLEEP_RESPONSE),
            _mock_response(_HR_RESPONSE),
            _mock_response(_ACTIVITY_RESPONSE),
            _mock_response({"hrv": []}),       # empty HRV
        ])
        adapter._client = mock_client

        reading = await adapter.fetch_context(_TEST_DATE)
        assert reading.hrv_ms is None
        assert reading.resting_hr_bpm == 55    # other data still present

    @pytest.mark.asyncio
    async def test_401_triggers_token_refresh_and_retry(self, tmp_path):
        """A 401 mid-session triggers one token refresh, then retries the request."""
        adapter = _make_adapter(tmp_path)

        # First call returns 401; after refresh the retry returns data
        call_count = {"n": 0}

        async def get_side_effect(url, **kwargs):
            call_count["n"] += 1
            # First request for sleep: 401, then ok on retry
            if call_count["n"] == 1:
                return _mock_response({}, status_code=401)
            # Retry after refresh + all others
            if "sleep" in url:
                return _mock_response(_SLEEP_RESPONSE)
            if "heart" in url:
                return _mock_response(_HR_RESPONSE)
            if "activities/date" in url:
                return _mock_response(_ACTIVITY_RESPONSE)
            return _mock_response({"hrv": []})

        refresh_response = {
            "access_token": "refreshed_token",
            "refresh_token": "refreshed_refresh",
        }

        import httpx
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(side_effect=get_side_effect)
        mock_client.post = AsyncMock(return_value=_mock_response(refresh_response))
        adapter._client = mock_client

        # Should not raise
        reading = await adapter.fetch_context(_TEST_DATE)
        assert reading.source == "fitbit"
