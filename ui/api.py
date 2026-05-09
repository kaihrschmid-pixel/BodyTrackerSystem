"""
ui/api.py
=========
FastAPI backend — REST API and WebSocket for the dashboard.

Endpoints:
    GET  /api/sessions              Recent sessions
    GET  /api/sessions/{id}         Full session detail
    POST /api/sessions/trigger      Manually trigger a session
    GET  /api/metrics/available     Which metrics have data
    GET  /api/metrics/{metric}      Historical data (scale or wearable.key)
    GET  /api/photos/{id}           Serve photo file
    GET  /api/photos/{id}/silhouette  Serve silhouette
    GET  /api/system/status         Adapter status + last session
    GET  /api/system/profile        Profile settings
    WS   /ws                        Live session events

Run:
    uvicorn ui.api:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from adapters.registry import list_available_adapters
from core.database import Database

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level state (set via create_app)
# ---------------------------------------------------------------------------

_db: Optional[Database] = None
_scheduler = None
_config: dict = {}
_ws_clients: list[WebSocket] = []


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(db: Database, scheduler=None, config: dict = {}) -> FastAPI:
    global _db, _scheduler, _config
    _db = db
    _scheduler = scheduler
    _config = config

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if _scheduler:
            _scheduler.event_bus.on("session_complete", _broadcast_ws)
            _scheduler.event_bus.on("daily_reminder", _broadcast_ws)
        yield

    app = FastAPI(title="Body Tracker API", version="0.1.0", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://localhost:3000", "http://localhost:8000"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(_sessions_router())
    app.include_router(_metrics_router())
    app.include_router(_photos_router())
    app.include_router(_system_router())
    app.add_api_websocket_route("/ws", _ws_handler)

    static_dir = Path(__file__).parent / "static" / "dist"
    if static_dir.exists() and any(static_dir.iterdir()):
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")

    return app


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def _sessions_router() -> APIRouter:
    r = APIRouter(prefix="/api/sessions", tags=["sessions"])

    @r.get("")
    async def list_sessions(limit: int = 90, offset: int = 0, profile_id: int = 1):
        _require_db()
        sessions = await _db.get_sessions(profile_id, limit=limit, offset=offset)
        return {"sessions": sessions, "total": len(sessions)}

    @r.get("/{session_id}")
    async def get_session(session_id: int):
        _require_db()
        data = await _db.get_session_full(session_id)
        if not data:
            raise HTTPException(404, f"Session {session_id} not found")
        return data

    class TriggerReq(BaseModel):
        notes: Optional[str] = None
        profile_id: int = 1

    @r.post("/trigger")
    async def trigger_session(req: TriggerReq):
        if _scheduler is None:
            raise HTTPException(503, "Scheduler not running — start the daemon first")
        asyncio.create_task(_scheduler.run_session(notes=req.notes))
        return {"status": "triggered"}

    return r


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def _metrics_router() -> APIRouter:
    r = APIRouter(prefix="/api/metrics", tags=["metrics"])

    @r.get("/available")
    async def available(profile_id: int = 1):
        _require_db()
        return await _db.get_available_metrics(profile_id)

    @r.get("/{metric}")
    async def history(metric: str, days: int = 90, profile_id: int = 1):
        _require_db()
        if "." in metric:
            source, key = metric.split(".", 1)
            data = await _db.get_context_history(profile_id, source, key, days)
        else:
            data = await _db.get_metric_history(profile_id, metric, days)
        return {
            "metric": metric,
            "days": days,
            "points": [{"date": d, "value": round(v, 4)} for d, v in data],
        }

    return r


# ---------------------------------------------------------------------------
# Photos
# ---------------------------------------------------------------------------

def _photos_router() -> APIRouter:
    r = APIRouter(prefix="/api/photos", tags=["photos"])

    @r.get("/{photo_id}")
    async def serve_photo(photo_id: int):
        _require_db()
        cur = await _db._conn.execute(
            "SELECT filepath FROM photos WHERE id=?", (photo_id,)
        )
        row = await cur.fetchone()
        if not row:
            raise HTTPException(404, "Photo not found")
        path = Path(row["filepath"])
        if not path.exists():
            raise HTTPException(404, "Photo file missing from disk")
        return FileResponse(path, media_type=mimetypes.guess_type(str(path))[0] or "image/jpeg")

    @r.get("/{photo_id}/silhouette")
    async def serve_silhouette(photo_id: int):
        _require_db()
        cur = await _db._conn.execute(
            "SELECT silhouette_path FROM photos WHERE id=?", (photo_id,)
        )
        row = await cur.fetchone()
        if not row or not row["silhouette_path"]:
            raise HTTPException(404, "Silhouette not generated yet")
        path = Path(row["silhouette_path"])
        if not path.exists():
            raise HTTPException(404, "Silhouette file missing")
        return FileResponse(path, media_type="image/png")

    return r


# ---------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------

def _system_router() -> APIRouter:
    r = APIRouter(prefix="/api/system", tags=["system"])

    @r.get("/status")
    async def status(profile_id: int = 1):
        _require_db()
        sessions = await _db.get_sessions(profile_id, limit=1)
        hw = _config.get("hardware", {})
        wearables_cfg = _config.get("wearables", {})
        active_wearables = [
            name for name, cfg in wearables_cfg.items()
            if isinstance(cfg, dict) and cfg.get("enabled", False)
        ]
        return {
            "version": "0.1.0",
            "last_session": sessions[0] if sessions else None,
            "scale_adapter": hw.get("scale", {}).get("adapter", "unknown"),
            "camera_adapter": hw.get("camera", {}).get("adapter", "unknown"),
            "active_wearables": active_wearables,
            "ai_enabled": _config.get("ai", {}).get("enabled", False),
            "available_adapters": list_available_adapters(),
        }

    @r.get("/profile")
    async def profile(profile_id: int = 1):
        _require_db()
        p = await _db.get_profile(profile_id)
        if not p:
            raise HTTPException(404, "Profile not found")
        return p

    return r


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

async def _ws_handler(websocket: WebSocket):
    await websocket.accept()
    _ws_clients.append(websocket)
    logger.info("WS client connected (%d total)", len(_ws_clients))
    try:
        while True:
            await websocket.receive_text()  # keep-alive / ping
    except WebSocketDisconnect:
        pass
    finally:
        if websocket in _ws_clients:
            _ws_clients.remove(websocket)


async def _broadcast_ws(payload: dict) -> None:
    import json
    msg = json.dumps(payload)
    dead = []
    for ws in _ws_clients:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in _ws_clients:
            _ws_clients.remove(ws)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_db():
    if _db is None:
        raise HTTPException(503, "Database not initialised")
