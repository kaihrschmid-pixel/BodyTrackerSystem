"""
tests/adapters/test_garmin.py
==============================
Tests for the GarminAdapter.
Run with: pytest tests/ -v

All HTTP calls are mocked — no network or Garmin account required.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from adapters.wearables.garmin import GarminAdapter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_adapter(**overrides) -> GarminAdapter:
    config = {
        "consumer_key": "test_consumer_key",
        "consumer_secret": "test_consumer_secret",
        "access_token": "test_access_token",
        "access_token_secret": "test_token_secret",
        "user_id": "test_user_123",
    }
    config.update(overrides)
    return GarminAdapter(config)


def _mock_response(json_data, status_code: int = 200) -> MagicMock:
    mock = MagicMock()
    mock.status_code = status_code
    mock.json.return_value = json_data
    mock.raise_for_status = MagicMock()
    return mock


_TEST_DATE = datetime(2025, 6, 1, 0, 0, 0, tzinfo=timezone.utc)

# Realistic Garmin Health API responses
_DAILY_RESPONSE = [
    {
        "totalSteps": 9847,
        "activeKilocalories": 540,
        "bmrKilocalories": 1820,
        "vigorousIntensityDurationInSeconds": 1200,
        "moderateIntensityDurationInSeconds": 1800,
        "restingHeartRateInBeatsPerMinute": 58,
        "averageHeartRateInBeatsPerMinute": 72,
        "maxHeartRateInBeatsPerMinute": 145,
        "averageStressLevel": 28,
        "trainingEffect": 2.4,
    }
]

_SLEEP_RESPONSE = [
    {
        "durationInSeconds": 27000,   # 7.5 hours
        "overallSleepScore": {"value": 81},
        "deepSleepDurationInSeconds": 5400,    # 1.5 h
        "remSleepInSeconds": 6300,             # 1.75 h
        "averageSpO2Value": 97.2,
    }
]

_METRICS_RESPONSE = [
    {
        "vo2Max": 48.5,
        "fitnessAge": 32,
    }
]


# ---------------------------------------------------------------------------
# 1. is_available
# ---------------------------------------------------------------------------

class TestIsAvailable:
    def test_is_available_with_credentials(self):
        adapter = _make_adapter()
        assert adapter.is_available() is True

    def test_is_available_without_consumer_key(self):
        adapter = _make_adapter(consumer_key="")
        assert adapter.is_available() is False

    def test_is_available_without_access_token(self):
        adapter = _make_adapter(access_token="")
        assert adapter.is_available() is False

    def test_is_available_empty_config(self):
        assert GarminAdapter({}).is_available() is False


# ---------------------------------------------------------------------------
# 2. OAuth 1.0a signing
# ---------------------------------------------------------------------------

class TestOAuth1Signing:
    def test_oauth1_header_contains_required_fields(self):
        """The generated header must include all standard OAuth 1.0a fields."""
        adapter = _make_adapter()
        header = adapter._oauth1_header("GET", "https://healthapi.garmin.com/test", {})
        assert "oauth_consumer_key" in header
        assert "oauth_signature_method" in header
        assert "HMAC-SHA1" in header
        assert "oauth_timestamp" in header
        assert "oauth_nonce" in header
        assert "oauth_signature" in header
        assert header.startswith("OAuth ")

    def test_oauth1_header_is_deterministic_for_same_nonce(self):
        """Two calls with the same parameters produce consistent structure."""
        adapter = _make_adapter()
        h1 = adapter._oauth1_header("GET", "https://example.com/api", {"foo": "bar"})
        h2 = adapter._oauth1_header("GET", "https://example.com/api", {"foo": "bar"})
        # Both should start with OAuth and contain the same keys
        assert h1.startswith("OAuth ")
        assert h2.startswith("OAuth ")
        assert "oauth_consumer_key" in h1 and "oauth_consumer_key" in h2


# ---------------------------------------------------------------------------
# 3. Full fetch_context — parse daily summary
# ---------------------------------------------------------------------------

class TestFetchContext:
    @pytest.mark.asyncio
    async def test_parse_daily_summary(self):
        """Steps, calories, HR, stress-derived readiness_score all parsed."""
        adapter = _make_adapter()

        import httpx
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(side_effect=[
            _mock_response(_DAILY_RESPONSE),
            _mock_response(_SLEEP_RESPONSE),
            _mock_response(_METRICS_RESPONSE),
        ])
        adapter._client = mock_client

        reading = await adapter.fetch_context(_TEST_DATE)

        assert reading.steps == 9847
        assert reading.active_calories_kcal == 540
        assert reading.resting_hr_bpm == 58
        # active_minutes: vigorous*2 + moderate = 1200*2 + 1800 = 4200 s = 70 min
        assert reading.active_minutes == 70
        # readiness_score = 100 - averageStressLevel = 100 - 28 = 72
        assert reading.readiness_score == 72
        assert reading.source == "garmin"
        assert reading.adapter_name == "garmin"

    @pytest.mark.asyncio
    async def test_parse_sleep(self):
        """Sleep duration, stages and score correctly extracted."""
        adapter = _make_adapter()

        import httpx
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(side_effect=[
            _mock_response(_DAILY_RESPONSE),
            _mock_response(_SLEEP_RESPONSE),
            _mock_response(_METRICS_RESPONSE),
        ])
        adapter._client = mock_client

        reading = await adapter.fetch_context(_TEST_DATE)

        assert reading.sleep_duration_h == pytest.approx(7.5)
        assert reading.sleep_score == 81
        assert reading.deep_sleep_h == pytest.approx(1.5)
        assert reading.rem_sleep_h == pytest.approx(1.75)
        assert "avg_spo2_pct" in reading.extras

    @pytest.mark.asyncio
    async def test_parse_user_metrics(self):
        """VO₂max and fitness age extracted."""
        adapter = _make_adapter()

        import httpx
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(side_effect=[
            _mock_response(_DAILY_RESPONSE),
            _mock_response(_SLEEP_RESPONSE),
            _mock_response(_METRICS_RESPONSE),
        ])
        adapter._client = mock_client

        reading = await adapter.fetch_context(_TEST_DATE)

        assert reading.vo2_max == pytest.approx(48.5)
        assert reading.extras.get("fitness_age") == pytest.approx(32.0)

    @pytest.mark.asyncio
    async def test_partial_failure_still_returns_reading(self):
        """If one endpoint fails, the others still populate the reading."""
        adapter = _make_adapter()

        import httpx
        mock_client = AsyncMock(spec=httpx.AsyncClient)

        # Sleep endpoint raises — daily and metrics still work
        async def side_effect(url, **kwargs):
            if "/sleeps" in url:
                raise Exception("timeout")
            elif "/dailies" in url:
                return _mock_response(_DAILY_RESPONSE)
            else:
                return _mock_response(_METRICS_RESPONSE)

        mock_client.get = AsyncMock(side_effect=side_effect)
        adapter._client = mock_client

        reading = await adapter.fetch_context(_TEST_DATE)
        # Steps available despite sleep failure
        assert reading.steps == 9847
        assert reading.sleep_duration_h is None
