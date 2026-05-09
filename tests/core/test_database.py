"""
tests/core/test_database.py
============================
Tests for core/database.py — CRUD operations and schema integrity.
Run with: pytest tests/ -v
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from adapters.base import CapturedFrame, ContextReading, ScaleReading, utcnow
from core.database import Database


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reading(weight: float = 80.0, **kwargs) -> ScaleReading:
    return ScaleReading(
        recorded_at=utcnow(),
        weight_kg=weight,
        adapter_name="manual",
        **kwargs,
    )


def _frame(tmp_path: Path, angle: str = "front") -> CapturedFrame:
    img = tmp_path / f"{angle}.jpg"
    img.write_bytes(b"x")
    return CapturedFrame(
        captured_at=utcnow(),
        image_path=img,
        angle=angle,
        width_px=1920,
        height_px=1080,
        adapter_name="mock",
    )


# ---------------------------------------------------------------------------
# Schema + init
# ---------------------------------------------------------------------------

class TestInit:
    @pytest.mark.asyncio
    async def test_creates_all_tables(self, tmp_path):
        async with Database(tmp_path / "test.db") as db:
            async with db._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ) as cur:
                tables = {r[0] for r in await cur.fetchall()}
        expected = {"profiles", "sessions", "measurements",
                    "measurement_metrics", "photos", "context_readings", "ai_insights"}
        assert expected.issubset(tables)

    @pytest.mark.asyncio
    async def test_init_is_idempotent(self, tmp_path):
        """Calling init() twice should not raise or duplicate schema."""
        db = Database(tmp_path / "test.db")
        await db.init()
        await db.init()   # second call — should be a no-op
        await db.close()

    @pytest.mark.asyncio
    async def test_creates_parent_directory(self, tmp_path):
        nested = tmp_path / "a" / "b" / "c" / "tracker.db"
        async with Database(nested) as db:
            pass
        assert nested.exists()


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------

class TestProfiles:
    @pytest.mark.asyncio
    async def test_create_and_retrieve_profile(self, tmp_path):
        async with Database(tmp_path / "db") as db:
            pid = await db.get_or_create_profile(
                "Kai", height_cm=183, sex="male", birthdate="1990-05-01"
            )
            assert pid > 0
            profile = await db.get_profile(pid)
        assert profile["name"] == "Kai"
        assert profile["height_cm"] == pytest.approx(183)
        assert profile["sex"] == "male"

    @pytest.mark.asyncio
    async def test_get_or_create_is_idempotent(self, tmp_path):
        async with Database(tmp_path / "db") as db:
            id1 = await db.get_or_create_profile("Alice")
            id2 = await db.get_or_create_profile("Alice")
        assert id1 == id2

    @pytest.mark.asyncio
    async def test_separate_profiles_get_separate_ids(self, tmp_path):
        async with Database(tmp_path / "db") as db:
            id1 = await db.get_or_create_profile("Alice")
            id2 = await db.get_or_create_profile("Bob")
        assert id1 != id2

    @pytest.mark.asyncio
    async def test_get_profile_returns_none_for_missing(self, tmp_path):
        async with Database(tmp_path / "db") as db:
            result = await db.get_profile(9999)
        assert result is None


# ---------------------------------------------------------------------------
# Sessions + Measurements
# ---------------------------------------------------------------------------

class TestSessions:
    @pytest.mark.asyncio
    async def test_save_and_retrieve_minimal_reading(self, tmp_path):
        async with Database(tmp_path / "db") as db:
            pid = await db.get_or_create_profile()
            sid = await db.save_session(pid, _reading(72.5))
            sessions = await db.get_sessions(pid)
        assert len(sessions) == 1
        assert sessions[0]["weight_kg"] == pytest.approx(72.5)

    @pytest.mark.asyncio
    async def test_save_full_body_scan_reading(self, tmp_path):
        reading = _reading(
            82.4,
            body_fat_pct=18.2,
            muscle_mass_kg=63.1,
            bone_mass_kg=3.2,
            water_pct=57.0,
            phase_angle=6.4,
            heart_rate_bpm=62,
            vascular_age=35,
            nerve_health_score=9.2,
            muscle_mass_left_arm_kg=4.0,
            muscle_mass_right_arm_kg=4.1,
            extras={"custom_score": 99.0},
        )
        async with Database(tmp_path / "db") as db:
            pid = await db.get_or_create_profile()
            sid = await db.save_session(pid, reading)
            sessions = await db.get_sessions(pid)
        s = sessions[0]
        assert s["body_fat_pct"] == pytest.approx(18.2)
        assert s["phase_angle"] == pytest.approx(6.4)
        assert s["heart_rate_bpm"] == 62
        assert s["nerve_health_score"] == pytest.approx(9.2)

    @pytest.mark.asyncio
    async def test_extras_saved_as_measurement_metrics(self, tmp_path):
        reading = _reading(80.0, extras={"my_metric": 42.5, "other": 7.0})
        async with Database(tmp_path / "db") as db:
            pid = await db.get_or_create_profile()
            sid = await db.save_session(pid, reading)
            async with db._conn.execute(
                "SELECT key, value FROM measurement_metrics WHERE session_id=?", (sid,)
            ) as cur:
                metrics = {r["key"]: r["value"] for r in await cur.fetchall()}
        assert metrics.get("my_metric") == pytest.approx(42.5)
        assert metrics.get("other") == pytest.approx(7.0)

    @pytest.mark.asyncio
    async def test_get_sessions_respects_limit(self, tmp_path):
        async with Database(tmp_path / "db") as db:
            pid = await db.get_or_create_profile()
            for w in [80.0, 79.5, 79.0, 78.5]:
                await db.save_session(pid, _reading(w))
            recent = await db.get_sessions(pid, limit=2)
        assert len(recent) == 2

    @pytest.mark.asyncio
    async def test_session_with_notes(self, tmp_path):
        async with Database(tmp_path / "db") as db:
            pid = await db.get_or_create_profile()
            sid = await db.save_session(pid, _reading(), notes="post-workout")
            sessions = await db.get_sessions(pid)
        assert sessions[0]["notes"] == "post-workout"


# ---------------------------------------------------------------------------
# Photos
# ---------------------------------------------------------------------------

class TestPhotos:
    @pytest.mark.asyncio
    async def test_save_photo_returns_id(self, tmp_path):
        async with Database(tmp_path / "db") as db:
            pid = await db.get_or_create_profile()
            sid = await db.save_session(pid, _reading())
            photo_id = await db.save_photo(sid, _frame(tmp_path))
        assert photo_id > 0

    @pytest.mark.asyncio
    async def test_update_photo_analysis_sets_silhouette(self, tmp_path):
        sil_path = "/data/silhouettes/photo_silhouette.png"
        async with Database(tmp_path / "db") as db:
            pid = await db.get_or_create_profile()
            sid = await db.save_session(pid, _reading())
            photo_id = await db.save_photo(sid, _frame(tmp_path))
            await db.update_photo_analysis(
                photo_id, silhouette_path=sil_path, ai_score=0.92, ai_label="lean"
            )
            async with db._conn.execute(
                "SELECT silhouette_path, ai_score, ai_label FROM photos WHERE id=?",
                (photo_id,)
            ) as cur:
                row = await cur.fetchone()
        assert row["silhouette_path"] == sil_path
        assert row["ai_score"] == pytest.approx(0.92)
        assert row["ai_label"] == "lean"

    @pytest.mark.asyncio
    async def test_save_photo_with_depth_map(self, tmp_path):
        depth = tmp_path / "depth.png"
        depth.write_bytes(b"d")
        frame = CapturedFrame(
            captured_at=utcnow(),
            image_path=tmp_path / "photo.jpg",
            angle="front",
            adapter_name="realsense",
            depth_map_path=depth,
        )
        (tmp_path / "photo.jpg").write_bytes(b"j")
        async with Database(tmp_path / "db") as db:
            pid = await db.get_or_create_profile()
            sid = await db.save_session(pid, _reading())
            photo_id = await db.save_photo(sid, frame)
            async with db._conn.execute(
                "SELECT depth_map_path FROM photos WHERE id=?", (photo_id,)
            ) as cur:
                row = await cur.fetchone()
        assert row["depth_map_path"] == str(depth)


# ---------------------------------------------------------------------------
# Context (wearables)
# ---------------------------------------------------------------------------

class TestContext:
    @pytest.mark.asyncio
    async def test_save_and_retrieve_context(self, tmp_path):
        ctx = ContextReading(
            date=utcnow(),
            source="oura",
            sleep_score=85,
            hrv_ms=47.3,
            steps=9000,
            extras={"fitness_age": 30.0},
        )
        async with Database(tmp_path / "db") as db:
            pid = await db.get_or_create_profile()
            sid = await db.save_session(pid, _reading())
            await db.save_context(sid, ctx)
            full = await db.get_session_full(sid)
        oura = full["context"]["oura"]
        assert oura["sleep_score"] == pytest.approx(85)
        assert oura["hrv_ms"] == pytest.approx(47.3)
        assert oura["steps"] == pytest.approx(9000)
        assert oura["fitness_age"] == pytest.approx(30.0)

    @pytest.mark.asyncio
    async def test_multiple_wearable_sources(self, tmp_path):
        async with Database(tmp_path / "db") as db:
            pid = await db.get_or_create_profile()
            sid = await db.save_session(pid, _reading())
            await db.save_context(sid, ContextReading(
                date=utcnow(), source="garmin", steps=11000, vo2_max=48.5
            ))
            await db.save_context(sid, ContextReading(
                date=utcnow(), source="whoop", readiness_score=72, hrv_ms=52.0
            ))
            full = await db.get_session_full(sid)
        assert "garmin" in full["context"]
        assert "whoop" in full["context"]
        assert full["context"]["garmin"]["steps"] == pytest.approx(11000)
        assert full["context"]["whoop"]["hrv_ms"] == pytest.approx(52.0)


# ---------------------------------------------------------------------------
# Metric history
# ---------------------------------------------------------------------------

class TestMetricHistory:
    @pytest.mark.asyncio
    async def test_weight_trend(self, tmp_path):
        weights = [82.0, 81.5, 81.2, 80.8]
        async with Database(tmp_path / "db") as db:
            pid = await db.get_or_create_profile()
            for w in weights:
                await db.save_session(pid, _reading(w))
            history = await db.get_metric_history(pid, "weight_kg", days=30)
        values = [v for _, v in history]
        assert values == pytest.approx(weights)

    @pytest.mark.asyncio
    async def test_body_fat_history(self, tmp_path):
        async with Database(tmp_path / "db") as db:
            pid = await db.get_or_create_profile()
            await db.save_session(pid, _reading(body_fat_pct=18.5))
            await db.save_session(pid, _reading(body_fat_pct=18.1))
            history = await db.get_metric_history(pid, "body_fat_pct", days=30)
        assert len(history) == 2
        assert history[1][1] == pytest.approx(18.1)
