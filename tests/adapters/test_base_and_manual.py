"""
tests/adapters/test_base_and_manual.py
======================================
Tests for the base dataclasses, ManualAdapter, and Database.
Run with: pytest tests/ -v
"""

from __future__ import annotations

import asyncio
import csv
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from adapters.base import ScaleReading, ContextReading, utcnow
from adapters.scales.manual import ManualAdapter
from adapters.registry import list_available_adapters


# ---------------------------------------------------------------------------
# ScaleReading
# ---------------------------------------------------------------------------

class TestScaleReading:
    def test_minimal_reading(self):
        r = ScaleReading(recorded_at=utcnow(), weight_kg=82.4)
        assert r.weight_kg == 82.4
        assert r.body_fat_pct is None

    def test_available_metrics_minimal(self):
        r = ScaleReading(recorded_at=utcnow(), weight_kg=75.0)
        assert r.available_metrics() == []

    def test_available_metrics_full(self):
        r = ScaleReading(
            recorded_at=utcnow(),
            weight_kg=75.0,
            body_fat_pct=18.2,
            phase_angle=6.4,
            extras={"cardiac_output": 5.1},
        )
        metrics = r.available_metrics()
        assert "body_fat_pct" in metrics
        assert "phase_angle" in metrics
        assert "cardiac_output" in metrics

    def test_extras_preserved(self):
        r = ScaleReading(
            recorded_at=utcnow(),
            weight_kg=80.0,
            extras={"custom_metric": 42.0},
        )
        assert r.extras["custom_metric"] == 42.0


# ---------------------------------------------------------------------------
# ContextReading
# ---------------------------------------------------------------------------

class TestContextReading:
    def test_available_metrics(self):
        c = ContextReading(
            date=utcnow(),
            source="oura",
            sleep_score=82,
            hrv_ms=45.3,
        )
        metrics = c.available_metrics()
        assert "sleep_score" in metrics
        assert "hrv_ms" in metrics
        assert "steps" not in metrics  # None, not included


# ---------------------------------------------------------------------------
# ManualAdapter — CSV mode
# ---------------------------------------------------------------------------

class TestManualAdapterCSV:
    def _write_csv(self, rows: list[dict], path: Path):
        with path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)

    @pytest.mark.asyncio
    async def test_csv_basic(self, tmp_path):
        csv_file = tmp_path / "import.csv"
        self._write_csv([
            {"date": "2025-01-15", "weight_kg": "82.4", "body_fat_pct": "18.2",
             "muscle_mass_kg": "65.1", "bone_mass_kg": "3.2", "water_pct": "57.4"},
        ], csv_file)

        adapter = ManualAdapter({"mode": "csv", "csv_path": str(csv_file)})
        await adapter.connect()
        reading = await adapter.read()

        assert reading.weight_kg == pytest.approx(82.4)
        assert reading.body_fat_pct == pytest.approx(18.2)
        assert reading.muscle_mass_kg == pytest.approx(65.1)
        assert reading.adapter_name == "manual"

    @pytest.mark.asyncio
    async def test_csv_date_parsed(self, tmp_path):
        csv_file = tmp_path / "import.csv"
        self._write_csv([
            {"date": "2025-06-01", "weight_kg": "70.0"},
        ], csv_file)

        adapter = ManualAdapter({"mode": "csv", "csv_path": str(csv_file)})
        await adapter.connect()
        reading = await adapter.read()

        assert reading.recorded_at.year == 2025
        assert reading.recorded_at.month == 6

    @pytest.mark.asyncio
    async def test_csv_multiple_rows(self, tmp_path):
        csv_file = tmp_path / "import.csv"
        self._write_csv([
            {"date": "2025-01-01", "weight_kg": "80.0"},
            {"date": "2025-01-02", "weight_kg": "79.8"},
            {"date": "2025-01-03", "weight_kg": "79.5"},
        ], csv_file)

        adapter = ManualAdapter({"mode": "csv", "csv_path": str(csv_file)})
        await adapter.connect()

        readings = [await adapter.read() for _ in range(3)]
        weights = [r.weight_kg for r in readings]
        assert weights == pytest.approx([80.0, 79.8, 79.5])

    @pytest.mark.asyncio
    async def test_csv_exhausted_raises(self, tmp_path):
        csv_file = tmp_path / "import.csv"
        self._write_csv([{"date": "2025-01-01", "weight_kg": "80.0"}], csv_file)

        adapter = ManualAdapter({"mode": "csv", "csv_path": str(csv_file)})
        await adapter.connect()
        await adapter.read()

        with pytest.raises(StopIteration):
            await adapter.read()

    @pytest.mark.asyncio
    async def test_csv_skips_bad_fields(self, tmp_path):
        csv_file = tmp_path / "import.csv"
        self._write_csv([
            {"date": "2025-01-01", "weight_kg": "80.0", "body_fat_pct": "not_a_number"},
        ], csv_file)

        adapter = ManualAdapter({"mode": "csv", "csv_path": str(csv_file)})
        await adapter.connect()
        reading = await adapter.read()

        assert reading.weight_kg == pytest.approx(80.0)
        assert reading.body_fat_pct is None  # bad value silently skipped

    def test_is_available_csv(self, tmp_path):
        csv_file = tmp_path / "data.csv"
        csv_file.write_text("date,weight_kg\n2025-01-01,80.0\n")
        adapter = ManualAdapter({"mode": "csv", "csv_path": str(csv_file)})
        assert adapter.is_available() is True

    def test_is_available_missing_file(self, tmp_path):
        adapter = ManualAdapter({"mode": "csv", "csv_path": str(tmp_path / "nope.csv")})
        assert adapter.is_available() is False

    def test_is_available_interactive(self):
        adapter = ManualAdapter({"mode": "interactive"})
        assert adapter.is_available() is True


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class TestRegistry:
    def test_list_adapters(self):
        adapters = list_available_adapters()
        assert "manual" in adapters["scales"]
        assert "oura" in adapters["wearables"]
        assert "v4l2" in adapters["cameras"]

    def test_get_scale_adapter_manual(self):
        from adapters.registry import get_scale_adapter
        adapter = get_scale_adapter({"adapter": "manual", "mode": "interactive"})
        assert isinstance(adapter, ManualAdapter)

    def test_get_scale_adapter_unknown_raises(self):
        from adapters.registry import get_scale_adapter
        with pytest.raises(ValueError, match="Unknown scale adapter"):
            get_scale_adapter({"adapter": "telekinesis"})

    def test_get_wearable_adapters_disabled(self):
        from adapters.registry import get_wearable_adapters
        adapters = get_wearable_adapters({
            "oura": {"enabled": False, "personal_access_token": "x"},
        })
        assert adapters == []


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

class TestDatabase:
    @pytest.mark.asyncio
    async def test_init_creates_tables(self, tmp_path):
        from core.database import Database
        db = Database(tmp_path / "test.db")
        await db.init()

        async with db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ) as cur:
            tables = {r[0] for r in await cur.fetchall()}

        assert "profiles" in tables
        assert "sessions" in tables
        assert "measurements" in tables
        assert "context_readings" in tables
        assert "photos" in tables
        await db.close()

    @pytest.mark.asyncio
    async def test_save_and_retrieve_session(self, tmp_path):
        from core.database import Database
        async with Database(tmp_path / "test.db") as db:
            profile_id = await db.get_or_create_profile("TestUser")

            reading = ScaleReading(
                recorded_at=datetime(2025, 6, 1, 7, 0, tzinfo=timezone.utc),
                weight_kg=80.5,
                body_fat_pct=18.2,
                phase_angle=6.1,
                extras={"custom_score": 99.0},
            )
            session_id = await db.save_session(profile_id, reading)
            assert session_id > 0

            sessions = await db.get_sessions(profile_id)
            assert len(sessions) == 1
            assert sessions[0]["weight_kg"] == pytest.approx(80.5)

    @pytest.mark.asyncio
    async def test_save_context(self, tmp_path):
        from core.database import Database
        async with Database(tmp_path / "test.db") as db:
            profile_id = await db.get_or_create_profile()
            reading = ScaleReading(recorded_at=utcnow(), weight_kg=78.0)
            session_id = await db.save_session(profile_id, reading)

            context = ContextReading(
                date=utcnow(),
                source="oura",
                sleep_score=85,
                hrv_ms=52.3,
                steps=8200,
            )
            await db.save_context(session_id, context)

            full = await db.get_session_full(session_id)
            assert "oura" in full["context"]
            assert full["context"]["oura"]["sleep_score"] == pytest.approx(85)
            assert full["context"]["oura"]["hrv_ms"] == pytest.approx(52.3)

    @pytest.mark.asyncio
    async def test_metric_history(self, tmp_path):
        from core.database import Database
        async with Database(tmp_path / "test.db") as db:
            profile_id = await db.get_or_create_profile()
            for w in [80.0, 79.5, 79.0]:
                r = ScaleReading(recorded_at=utcnow(), weight_kg=w)
                await db.save_session(profile_id, r)

            history = await db.get_metric_history(profile_id, "weight_kg", days=30)
            assert len(history) == 3
            values = [v for _, v in history]
            assert values == pytest.approx([80.0, 79.5, 79.0])
