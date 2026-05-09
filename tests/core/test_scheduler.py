"""
tests/core/test_scheduler.py
==============================
Tests for core/scheduler.py — session orchestration and EventBus.
Run with: pytest tests/ -v
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from adapters.base import CapturedFrame, ContextReading, ScaleReading, utcnow
from core.scheduler import EventBus, Scheduler


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _minimal_config(tmp_path: Path) -> dict:
    return {
        "storage": {"db_path": str(tmp_path / "test.db")},
        "profile": {"name": "TestUser"},
        "hardware": {
            "scale": {"adapter": "manual"},
            "camera": {"adapter": "mock"},
        },
        "wearables": {},
        "ai": {"enabled": False},
    }


def _mock_reading(weight: float = 75.0) -> ScaleReading:
    return ScaleReading(
        recorded_at=utcnow(),
        weight_kg=weight,
        adapter_name="manual",
    )


def _mock_frame(tmp_path: Path) -> CapturedFrame:
    img = tmp_path / "photo.jpg"
    img.write_bytes(b"x")
    return CapturedFrame(
        captured_at=utcnow(),
        image_path=img,
        angle="front",
        width_px=1920,
        height_px=1080,
        adapter_name="mock",
    )


# ---------------------------------------------------------------------------
# EventBus
# ---------------------------------------------------------------------------

class TestEventBus:
    @pytest.mark.asyncio
    async def test_sync_callback_called(self):
        bus = EventBus()
        received = []
        bus.on("test", lambda payload: received.append(payload))
        await bus.emit("test", {"x": 1})
        assert received == [{"x": 1}]

    @pytest.mark.asyncio
    async def test_async_callback_called(self):
        bus = EventBus()
        received = []

        async def handler(payload):
            received.append(payload)

        bus.on("test", handler)
        await bus.emit("test", {"y": 2})
        assert received == [{"y": 2}]

    @pytest.mark.asyncio
    async def test_multiple_listeners(self):
        bus = EventBus()
        results = []
        bus.on("evt", lambda p: results.append("a"))
        bus.on("evt", lambda p: results.append("b"))
        await bus.emit("evt", {})
        assert set(results) == {"a", "b"}

    @pytest.mark.asyncio
    async def test_emit_unknown_event_is_noop(self):
        bus = EventBus()
        # Should not raise
        await bus.emit("nonexistent", {})

    @pytest.mark.asyncio
    async def test_handler_exception_does_not_propagate(self):
        bus = EventBus()

        def bad_handler(payload):
            raise RuntimeError("boom")

        bus.on("evt", bad_handler)
        # Should log error but not raise
        await bus.emit("evt", {})


# ---------------------------------------------------------------------------
# Scheduler — setup
# ---------------------------------------------------------------------------

class TestSchedulerSetup:
    @pytest.mark.asyncio
    async def test_setup_creates_profile(self, tmp_path):
        config = _minimal_config(tmp_path)

        with (
            patch("core.scheduler.get_scale_adapter") as mock_scale,
            patch("core.scheduler.get_camera_adapter") as mock_cam,
            patch("core.scheduler.get_wearable_adapters", return_value=[]),
        ):
            mock_scale.return_value = MagicMock()
            mock_cam.return_value = MagicMock()

            s = Scheduler(config)
            await s.setup()

            assert s._profile_id is not None
            assert s._profile_id > 0

    @pytest.mark.asyncio
    async def test_setup_is_idempotent(self, tmp_path):
        config = _minimal_config(tmp_path)

        with (
            patch("core.scheduler.get_scale_adapter") as mock_scale,
            patch("core.scheduler.get_camera_adapter") as mock_cam,
            patch("core.scheduler.get_wearable_adapters", return_value=[]),
        ):
            mock_scale.return_value = MagicMock()
            mock_cam.return_value = MagicMock()

            s = Scheduler(config)
            await s.setup()
            pid1 = s._profile_id
            await s.setup()
            pid2 = s._profile_id
            assert pid1 == pid2


# ---------------------------------------------------------------------------
# Scheduler — run_session
# ---------------------------------------------------------------------------

class TestRunSession:
    def _make_scheduler(self, config: dict, tmp_path: Path):
        """Build a Scheduler with fully mocked adapters."""
        reading = _mock_reading(80.0)
        frame = _mock_frame(tmp_path)

        scale_mock = AsyncMock()
        scale_mock.read = AsyncMock(return_value=reading)
        scale_mock.__aenter__ = AsyncMock(return_value=scale_mock)
        scale_mock.__aexit__ = AsyncMock(return_value=False)

        camera_mock = AsyncMock()
        camera_mock.on_scale_trigger = AsyncMock(return_value=frame)
        camera_mock.__aenter__ = AsyncMock(return_value=camera_mock)
        camera_mock.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("core.scheduler.get_scale_adapter", return_value=scale_mock),
            patch("core.scheduler.get_camera_adapter", return_value=camera_mock),
            patch("core.scheduler.get_wearable_adapters", return_value=[]),
        ):
            s = Scheduler(config)

        # Patch the already-assigned instance attributes
        s._scale = scale_mock
        s._camera = camera_mock
        s._wearables = []
        return s, reading, frame

    @pytest.mark.asyncio
    async def test_run_session_returns_summary(self, tmp_path):
        config = _minimal_config(tmp_path)
        s, reading, frame = self._make_scheduler(config, tmp_path)
        await s.setup()

        summary = await s.run_session()

        assert "session_id" in summary
        assert summary["weight_kg"] == pytest.approx(80.0)
        assert summary["photo_path"] == str(frame.image_path)
        assert summary["wearable_sources"] == []
        assert summary["ai_summary"] is None

    @pytest.mark.asyncio
    async def test_run_session_persists_to_db(self, tmp_path):
        config = _minimal_config(tmp_path)
        s, reading, frame = self._make_scheduler(config, tmp_path)
        await s.setup()

        summary = await s.run_session()

        # Verify row is in DB
        sessions = await s.db.get_sessions(s._profile_id)
        assert len(sessions) == 1
        assert sessions[0]["weight_kg"] == pytest.approx(80.0)

    @pytest.mark.asyncio
    async def test_run_session_emits_events(self, tmp_path):
        config = _minimal_config(tmp_path)
        s, _, _ = self._make_scheduler(config, tmp_path)
        await s.setup()

        events = []
        s.event_bus.on("session_start", lambda p: events.append(("start", p)))
        s.event_bus.on("session_complete", lambda p: events.append(("complete", p)))

        await s.run_session()

        event_names = [e[0] for e in events]
        assert "start" in event_names
        assert "complete" in event_names

    @pytest.mark.asyncio
    async def test_run_session_with_notes(self, tmp_path):
        config = _minimal_config(tmp_path)
        s, _, _ = self._make_scheduler(config, tmp_path)
        await s.setup()

        await s.run_session(notes="morning fasted")

        sessions = await s.db.get_sessions(s._profile_id)
        assert sessions[0]["notes"] == "morning fasted"

    @pytest.mark.asyncio
    async def test_silhouette_skipped_when_disabled(self, tmp_path):
        config = _minimal_config(tmp_path)
        config["imaging"] = {"enabled": False}
        s, _, _ = self._make_scheduler(config, tmp_path)
        await s.setup()

        summary = await s.run_session()
        assert summary["silhouette_path"] is None

    @pytest.mark.asyncio
    async def test_silhouette_import_error_is_nonfatal(self, tmp_path):
        config = _minimal_config(tmp_path)
        config["imaging"] = {"enabled": True}
        s, _, _ = self._make_scheduler(config, tmp_path)
        await s.setup()

        with patch("builtins.__import__", side_effect=ImportError("no mediapipe")):
            # ImportError inside _extract_silhouette → returns None, doesn't raise
            path = await s._extract_silhouette(1, MagicMock(image_path=tmp_path / "x.jpg"))
        assert path is None


# ---------------------------------------------------------------------------
# Scheduler — wearable context
# ---------------------------------------------------------------------------

class TestWearableContext:
    @pytest.mark.asyncio
    async def test_wearable_failure_is_nonfatal(self, tmp_path):
        config = _minimal_config(tmp_path)

        failing_adapter = AsyncMock()
        failing_adapter.__aenter__ = AsyncMock(return_value=failing_adapter)
        failing_adapter.__aexit__ = AsyncMock(return_value=False)
        failing_adapter.fetch_context = AsyncMock(
            side_effect=RuntimeError("API down")
        )

        with (
            patch("core.scheduler.get_scale_adapter", return_value=AsyncMock()),
            patch("core.scheduler.get_camera_adapter", return_value=AsyncMock()),
            patch("core.scheduler.get_wearable_adapters",
                  return_value=[failing_adapter]),
        ):
            s = Scheduler(config)

        s._wearables = [failing_adapter]

        # _fetch_wearable_context must not raise
        await s.db.init()
        s._profile_id = await s.db.get_or_create_profile("Test")
        session_id = await s.db.save_session(
            s._profile_id,
            ScaleReading(recorded_at=utcnow(), weight_kg=75.0, adapter_name="m"),
        )
        results = await s._fetch_wearable_context(session_id)
        assert results == []

    @pytest.mark.asyncio
    async def test_successful_wearable_context_saved(self, tmp_path):
        config = _minimal_config(tmp_path)

        ctx = ContextReading(date=utcnow(), source="mock_wearable", steps=8000)

        good_adapter = AsyncMock()
        good_adapter.__aenter__ = AsyncMock(return_value=good_adapter)
        good_adapter.__aexit__ = AsyncMock(return_value=False)
        good_adapter.fetch_context = AsyncMock(return_value=ctx)

        with (
            patch("core.scheduler.get_scale_adapter", return_value=AsyncMock()),
            patch("core.scheduler.get_camera_adapter", return_value=AsyncMock()),
            patch("core.scheduler.get_wearable_adapters", return_value=[good_adapter]),
        ):
            s = Scheduler(config)

        s._wearables = [good_adapter]
        await s.db.init()
        s._profile_id = await s.db.get_or_create_profile("Test")
        session_id = await s.db.save_session(
            s._profile_id,
            ScaleReading(recorded_at=utcnow(), weight_kg=75.0, adapter_name="m"),
        )

        results = await s._fetch_wearable_context(session_id)
        assert len(results) == 1
        assert results[0].source == "mock_wearable"

        full = await s.db.get_session_full(session_id)
        assert "mock_wearable" in full["context"]
