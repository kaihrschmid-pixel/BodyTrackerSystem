"""
core/database.py
================
Local SQLite database — schema and all CRUD operations.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiosqlite

from adapters.base import ScaleReading, CapturedFrame, ContextReading

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

CREATE_TABLES = """
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS profiles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL DEFAULT 'Default',
    sex             TEXT,
    birthdate       TEXT,
    height_cm       REAL,
    scale_adapter   TEXT    NOT NULL DEFAULT 'manual',
    camera_adapter  TEXT    NOT NULL DEFAULT 'mock',
    timezone        TEXT    NOT NULL DEFAULT 'UTC',
    created_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id      INTEGER NOT NULL REFERENCES profiles(id),
    recorded_at     TEXT    NOT NULL,
    trigger_source  TEXT    NOT NULL DEFAULT 'scale',
    notes           TEXT,
    created_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS measurements (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id               INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    weight_kg                REAL    NOT NULL,
    body_fat_pct             REAL,
    muscle_mass_kg           REAL,
    bone_mass_kg             REAL,
    water_pct                REAL,
    visceral_fat_index       REAL,
    bmr_kcal                 REAL,
    bmi                      REAL,
    metabolic_age            INTEGER,
    phase_angle              REAL,
    ecm_bcm_ratio            REAL,
    muscle_mass_left_arm_kg  REAL,
    muscle_mass_right_arm_kg REAL,
    muscle_mass_left_leg_kg  REAL,
    muscle_mass_right_leg_kg REAL,
    muscle_mass_torso_kg     REAL,
    fat_mass_left_arm_kg     REAL,
    fat_mass_right_arm_kg    REAL,
    fat_mass_left_leg_kg     REAL,
    fat_mass_right_leg_kg    REAL,
    fat_mass_torso_kg        REAL,
    heart_rate_bpm           INTEGER,
    pulse_wave_velocity      REAL,
    vascular_age             INTEGER,
    nerve_health_score       REAL,
    adapter_name             TEXT    NOT NULL DEFAULT 'unknown',
    raw_payload              TEXT
);

CREATE TABLE IF NOT EXISTS measurement_metrics (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    key         TEXT    NOT NULL,
    value       REAL    NOT NULL,
    unit        TEXT
);

CREATE TABLE IF NOT EXISTS photos (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    filepath        TEXT    NOT NULL,
    angle           TEXT    NOT NULL DEFAULT 'front',
    width_px        INTEGER,
    height_px       INTEGER,
    depth_map_path  TEXT,
    silhouette_path TEXT,
    ai_score        REAL,
    ai_label        TEXT,
    captured_at     TEXT    NOT NULL,
    adapter_name    TEXT    NOT NULL DEFAULT 'unknown'
);

CREATE TABLE IF NOT EXISTS context_readings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    source      TEXT    NOT NULL,
    key         TEXT    NOT NULL,
    value       REAL    NOT NULL,
    unit        TEXT,
    recorded_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_insights (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    model_used      TEXT    NOT NULL,
    summary         TEXT,
    trends          TEXT,
    recommendations TEXT,
    raw_response    TEXT,
    generated_at    TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_profile_date
    ON sessions(profile_id, recorded_at);
CREATE INDEX IF NOT EXISTS idx_context_session_source
    ON context_readings(session_id, source);
CREATE INDEX IF NOT EXISTS idx_metrics_session_key
    ON measurement_metrics(session_id, key);
"""


class Database:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._conn: Optional[aiosqlite.Connection] = None

    async def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(CREATE_TABLES)
        await self._migrate()
        await self._conn.commit()
        logger.info("Database ready: %s", self.db_path)

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()

    async def __aenter__(self):
        await self.init()
        return self

    async def __aexit__(self, *_):
        await self.close()

    # ------------------------------------------------------------------
    # Profiles
    # ------------------------------------------------------------------

    async def get_or_create_profile(self, name: str = "Default", **kwargs) -> int:
        cur = await self._conn.execute(
            "SELECT id FROM profiles WHERE name = ?", (name,)
        )
        row = await cur.fetchone()
        if row:
            return row["id"]
        now = _now()
        await self._conn.execute(
            """INSERT INTO profiles
               (name, sex, birthdate, height_cm, scale_adapter,
                camera_adapter, timezone, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                name,
                kwargs.get("sex"),
                kwargs.get("birthdate"),
                kwargs.get("height_cm"),
                kwargs.get("scale_adapter", "manual"),
                kwargs.get("camera_adapter", "mock"),
                kwargs.get("timezone", "UTC"),
                now,
            ),
        )
        await self._conn.commit()
        cur = await self._conn.execute("SELECT last_insert_rowid() AS id")
        row = await cur.fetchone()
        return row["id"]

    async def get_profile(self, profile_id: int) -> Optional[dict]:
        cur = await self._conn.execute(
            "SELECT * FROM profiles WHERE id = ?", (profile_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # Sessions + Measurements
    # ------------------------------------------------------------------

    async def save_session(
        self,
        profile_id: int,
        reading: ScaleReading,
        trigger_source: str = "scale",
        notes: Optional[str] = None,
    ) -> int:
        now = _now()
        await self._conn.execute(
            """INSERT INTO sessions
               (profile_id, recorded_at, trigger_source, notes, created_at)
               VALUES (?,?,?,?,?)""",
            (profile_id, reading.recorded_at.isoformat(), trigger_source, notes, now),
        )
        cur = await self._conn.execute("SELECT last_insert_rowid() AS id")
        row = await cur.fetchone()
        session_id = row["id"]

        raw = json.dumps(reading.raw_payload) if reading.raw_payload else None
        await self._conn.execute(
            """INSERT INTO measurements (
                session_id, weight_kg, body_fat_pct, muscle_mass_kg, bone_mass_kg,
                water_pct, visceral_fat_index, bmr_kcal, bmi, metabolic_age,
                phase_angle, ecm_bcm_ratio,
                muscle_mass_left_arm_kg, muscle_mass_right_arm_kg,
                muscle_mass_left_leg_kg, muscle_mass_right_leg_kg, muscle_mass_torso_kg,
                fat_mass_left_arm_kg, fat_mass_right_arm_kg,
                fat_mass_left_leg_kg, fat_mass_right_leg_kg, fat_mass_torso_kg,
                heart_rate_bpm, pulse_wave_velocity, vascular_age, nerve_health_score,
                adapter_name, raw_payload
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                session_id,
                reading.weight_kg,
                reading.body_fat_pct,
                reading.muscle_mass_kg,
                reading.bone_mass_kg,
                reading.water_pct,
                reading.visceral_fat_index,
                reading.bmr_kcal,
                reading.bmi,
                reading.metabolic_age,
                reading.phase_angle,
                reading.ecm_bcm_ratio,
                reading.muscle_mass_left_arm_kg,
                reading.muscle_mass_right_arm_kg,
                reading.muscle_mass_left_leg_kg,
                reading.muscle_mass_right_leg_kg,
                reading.muscle_mass_torso_kg,
                reading.fat_mass_left_arm_kg,
                reading.fat_mass_right_arm_kg,
                reading.fat_mass_left_leg_kg,
                reading.fat_mass_right_leg_kg,
                reading.fat_mass_torso_kg,
                reading.heart_rate_bpm,
                reading.pulse_wave_velocity,
                reading.vascular_age,
                reading.nerve_health_score,
                reading.adapter_name,
                raw,
            ),
        )

        for key, value in reading.extras.items():
            await self._conn.execute(
                "INSERT INTO measurement_metrics (session_id, key, value) VALUES (?,?,?)",
                (session_id, key, value),
            )

        await self._conn.commit()
        logger.debug("Saved session %d (%.1f kg)", session_id, reading.weight_kg)
        return session_id

    # ------------------------------------------------------------------
    # Photos
    # ------------------------------------------------------------------

    async def save_photo(self, session_id: int, frame: CapturedFrame) -> int:
        await self._conn.execute(
            """INSERT INTO photos
               (session_id, filepath, angle, width_px, height_px,
                depth_map_path, captured_at, adapter_name)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                session_id,
                str(frame.image_path),
                frame.angle,
                frame.width_px,
                frame.height_px,
                str(frame.depth_map_path) if frame.depth_map_path else None,
                frame.captured_at.isoformat(),
                frame.adapter_name,
            ),
        )
        await self._conn.commit()
        cur = await self._conn.execute("SELECT last_insert_rowid() AS id")
        row = await cur.fetchone()
        return row["id"]

    async def update_photo_analysis(
        self,
        photo_id: int,
        silhouette_path: Optional[str] = None,
        ai_score: Optional[float] = None,
        ai_label: Optional[str] = None,
    ) -> None:
        await self._conn.execute(
            "UPDATE photos SET silhouette_path=?, ai_score=?, ai_label=? WHERE id=?",
            (silhouette_path, ai_score, ai_label, photo_id),
        )
        await self._conn.commit()

    # ------------------------------------------------------------------
    # Context (wearables)
    # ------------------------------------------------------------------

    async def save_context(self, session_id: int, context: ContextReading) -> None:
        skip = {"date", "source", "extras", "adapter_name", "raw_payload"}
        rows = []
        for field_name, value in context.__dict__.items():
            if field_name in skip or value is None:
                continue
            rows.append((
                session_id, context.source, field_name,
                float(value), None, context.date.isoformat()
            ))
        for key, value in context.extras.items():
            rows.append((
                session_id, context.source, key,
                float(value), None, context.date.isoformat()
            ))
        await self._conn.executemany(
            """INSERT INTO context_readings
               (session_id, source, key, value, unit, recorded_at)
               VALUES (?,?,?,?,?,?)""",
            rows,
        )
        await self._conn.commit()
        logger.debug("Saved %d context metrics from %s", len(rows), context.source)

    # ------------------------------------------------------------------
    # AI insights
    # ------------------------------------------------------------------

    async def save_ai_insight(
        self,
        session_id: int,
        model_used: str,
        summary: str,
        trends: Optional[str] = None,
        recommendations: Optional[str] = None,
        raw_response: Optional[str] = None,
    ) -> int:
        await self._conn.execute(
            """INSERT INTO ai_insights
               (session_id, model_used, summary, trends,
                recommendations, raw_response, generated_at)
               VALUES (?,?,?,?,?,?,?)""",
            (session_id, model_used, summary, trends,
             recommendations, raw_response, _now()),
        )
        await self._conn.commit()
        cur = await self._conn.execute("SELECT last_insert_rowid() AS id")
        row = await cur.fetchone()
        return row["id"]

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    async def get_sessions(
        self,
        profile_id: int,
        limit: int = 90,
        offset: int = 0,
    ) -> list[dict]:
        cur = await self._conn.execute(
            """SELECT s.id, s.recorded_at, s.trigger_source, s.notes,
                      m.weight_kg, m.body_fat_pct, m.muscle_mass_kg,
                      m.bone_mass_kg, m.water_pct, m.phase_angle,
                      m.visceral_fat_index, m.bmr_kcal,
                      m.heart_rate_bpm, m.adapter_name
               FROM sessions s
               JOIN measurements m ON m.session_id = s.id
               WHERE s.profile_id = ?
               ORDER BY s.recorded_at DESC
               LIMIT ? OFFSET ?""",
            (profile_id, limit, offset),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_session_full(self, session_id: int) -> dict:
        cur = await self._conn.execute(
            "SELECT * FROM sessions WHERE id=?", (session_id,)
        )
        row = await cur.fetchone()
        session = dict(row) if row else {}

        cur = await self._conn.execute(
            "SELECT * FROM measurements WHERE session_id=?", (session_id,)
        )
        row = await cur.fetchone()
        session["measurement"] = dict(row) if row else {}

        cur = await self._conn.execute(
            "SELECT key, value, unit FROM measurement_metrics WHERE session_id=?",
            (session_id,),
        )
        rows = await cur.fetchall()
        session["extras"] = {r["key"]: r["value"] for r in rows}

        cur = await self._conn.execute(
            "SELECT * FROM photos WHERE session_id=?", (session_id,)
        )
        rows = await cur.fetchall()
        session["photos"] = [dict(r) for r in rows]

        cur = await self._conn.execute(
            """SELECT source, key, value FROM context_readings
               WHERE session_id=? ORDER BY source, key""",
            (session_id,),
        )
        rows = await cur.fetchall()
        context: dict[str, dict] = {}
        for r in rows:
            context.setdefault(r["source"], {})[r["key"]] = r["value"]
        session["context"] = context

        cur = await self._conn.execute(
            """SELECT * FROM ai_insights WHERE session_id=?
               ORDER BY generated_at DESC LIMIT 1""",
            (session_id,),
        )
        row = await cur.fetchone()
        session["ai_insight"] = dict(row) if row else None

        return session

    async def get_metric_history(
        self,
        profile_id: int,
        metric: str,
        days: int = 90,
    ) -> list[tuple[str, float]]:
        safe = metric.replace(";", "").replace("'", "").replace('"', "")
        try:
            cur = await self._conn.execute(
                f"""SELECT s.recorded_at, m.{safe}
                    FROM sessions s JOIN measurements m ON m.session_id = s.id
                    WHERE s.profile_id = ?
                      AND m.{safe} IS NOT NULL
                      AND s.recorded_at >= datetime('now', '-{days} days')
                    ORDER BY s.recorded_at""",
                (profile_id,),
            )
            rows = await cur.fetchall()
            if rows:
                return [(r[0], r[1]) for r in rows]
        except Exception:
            pass

        cur = await self._conn.execute(
            """SELECT s.recorded_at, mm.value
               FROM sessions s
               JOIN measurement_metrics mm ON mm.session_id = s.id
               WHERE s.profile_id = ? AND mm.key = ?
                 AND s.recorded_at >= datetime('now', ? || ' days')
               ORDER BY s.recorded_at""",
            (profile_id, metric, f"-{days}"),
        )
        rows = await cur.fetchall()
        return [(r[0], r[1]) for r in rows]

    async def get_context_history(
        self,
        profile_id: int,
        source: str,
        key: str,
        days: int = 90,
    ) -> list[tuple[str, float]]:
        cur = await self._conn.execute(
            """SELECT s.recorded_at, c.value
               FROM sessions s
               JOIN context_readings c ON c.session_id = s.id
               WHERE s.profile_id = ? AND c.source = ? AND c.key = ?
                 AND s.recorded_at >= datetime('now', ? || ' days')
               ORDER BY s.recorded_at""",
            (profile_id, source, key, f"-{days}"),
        )
        rows = await cur.fetchall()
        return [(r[0], r[1]) for r in rows]

    async def get_available_metrics(self, profile_id: int) -> dict:
        scale_cols = [
            "weight_kg", "body_fat_pct", "muscle_mass_kg", "bone_mass_kg",
            "water_pct", "visceral_fat_index", "bmr_kcal", "bmi", "metabolic_age",
            "phase_angle", "ecm_bcm_ratio", "heart_rate_bpm",
            "pulse_wave_velocity", "vascular_age", "nerve_health_score",
        ]
        available_scale = []
        for col in scale_cols:
            cur = await self._conn.execute(
                f"""SELECT 1 FROM sessions s
                    JOIN measurements m ON m.session_id = s.id
                    WHERE s.profile_id = ? AND m.{col} IS NOT NULL LIMIT 1""",
                (profile_id,),
            )
            if await cur.fetchone():
                available_scale.append(col)

        cur = await self._conn.execute(
            """SELECT DISTINCT mm.key FROM measurement_metrics mm
               JOIN sessions s ON s.id = mm.session_id
               WHERE s.profile_id = ?""",
            (profile_id,),
        )
        rows = await cur.fetchall()
        extras = [r[0] for r in rows]

        cur = await self._conn.execute(
            """SELECT DISTINCT c.source, c.key FROM context_readings c
               JOIN sessions s ON s.id = c.session_id
               WHERE s.profile_id = ? ORDER BY c.source, c.key""",
            (profile_id,),
        )
        rows = await cur.fetchall()
        wearables: dict[str, list[str]] = {}
        for r in rows:
            wearables.setdefault(r["source"], []).append(r["key"])

        return {"scale": available_scale, "extras": extras, "wearables": wearables}

    # ------------------------------------------------------------------
    # Migrations
    # ------------------------------------------------------------------

    async def _migrate(self) -> None:
        cur = await self._conn.execute("PRAGMA user_version")
        row = await cur.fetchone()
        current = row[0]
        if current < SCHEMA_VERSION:
            logger.info("DB migration: v%d → v%d", current, SCHEMA_VERSION)
            await self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            await self._conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
