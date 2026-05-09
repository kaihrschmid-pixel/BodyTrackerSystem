"""
core/scheduler.py
=================
Daily measurement orchestrator — the heart of body-tracker.

Responsibilities:
  1. Listen for scale trigger event (stable weight reading)
  2. Fire camera capture after stabilisation delay
  3. Persist scale reading + photo to database
  4. Fetch wearable context for the day (async, non-blocking)
  5. Optionally trigger AI analysis
  6. Emit events for the UI (via asyncio.Queue)

Flow:
    Scale.read()
        └─> CameraAdapter.on_scale_trigger()  ← concurrent
        └─> DB.save_session()
        └─> WearableAdapters.fetch_context()  ← concurrent, best-effort
        └─> AI.analyse()                      ← optional, async
        └─> EventBus.emit("session_complete")

Design notes:
  - Everything is async — no blocking calls on the main loop
  - Wearable fetch failures are logged but never crash the session
  - The scheduler can run as a one-shot (cron/systemd) or persistent daemon
  - APScheduler handles the optional daily reminder trigger
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from adapters.base import ScaleReading, CapturedFrame, ContextReading
from adapters.registry import get_scale_adapter, get_camera_adapter, get_wearable_adapters
from core.database import Database

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Simple in-process event bus
# ---------------------------------------------------------------------------

class EventBus:
    """
    Lightweight pub/sub for internal events.
    The UI subscribes to this to get real-time updates via WebSocket.
    """

    def __init__(self):
        self._listeners: dict[str, list] = {}

    def on(self, event: str, callback) -> None:
        self._listeners.setdefault(event, []).append(callback)

    async def emit(self, event: str, payload: dict) -> None:
        for cb in self._listeners.get(event, []):
            try:
                if asyncio.iscoroutinefunction(cb):
                    await cb(payload)
                else:
                    cb(payload)
            except Exception as exc:
                logger.error("EventBus handler error (%s): %s", event, exc)


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

class Scheduler:
    """
    Orchestrates a full measurement session.

    Usage (one-shot — e.g. triggered by systemd timer):
        scheduler = Scheduler(config)
        await scheduler.run_session()

    Usage (daemon with daily reminder):
        scheduler = Scheduler(config)
        await scheduler.start_daemon()  # blocks until Ctrl+C
    """

    def __init__(self, config: dict):
        self.config = config
        self.db = Database(config["storage"]["db_path"])
        self.event_bus = EventBus()

        # Adapters (lazy-init in run_session)
        self._scale = get_scale_adapter(config["hardware"]["scale"])
        self._camera = get_camera_adapter(config["hardware"]["camera"])
        self._wearables = get_wearable_adapters(config.get("wearables", {}))

        self._profile_id: Optional[int] = None
        self._ai_enabled: bool = config.get("ai", {}).get("enabled", False)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def setup(self) -> None:
        """Initialise DB and resolve profile. Call once before run_session()."""
        await self.db.init()
        profile_cfg = config_get(self.config, "profile", {})
        self._profile_id = await self.db.get_or_create_profile(
            name=profile_cfg.get("name", "Default"),
            height_cm=profile_cfg.get("height_cm"),
            sex=profile_cfg.get("sex"),
            birthdate=profile_cfg.get("birthdate"),
            scale_adapter=self.config["hardware"]["scale"].get("adapter", "manual"),
            camera_adapter=self.config["hardware"]["camera"].get("adapter", "mock"),
            timezone=profile_cfg.get("timezone", "UTC"),
        )
        logger.info("Scheduler ready. Profile id=%d", self._profile_id)

    async def run_session(self, notes: Optional[str] = None) -> dict:
        """
        Execute one full measurement session.
        Returns a summary dict suitable for the UI / API response.
        """
        logger.info("─── Session starting ───")
        await self.event_bus.emit("session_start", {"profile_id": self._profile_id})

        async with self._scale, self._camera:
            # Step 1: Get scale reading + camera capture concurrently
            scale_reading, frame = await self._measure_and_capture()

        # Step 2: Persist to DB
        session_id = await self.db.save_session(
            self._profile_id, scale_reading, notes=notes
        )
        photo_id = await self.db.save_photo(session_id, frame)
        logger.info("Session %d saved (photo_id=%d)", session_id, photo_id)

        # Step 3: Silhouette extraction (optional, non-fatal)
        silhouette_path = await self._extract_silhouette(photo_id, frame)

        # Step 4: Fetch wearable context (best-effort, non-blocking)
        context_results = await self._fetch_wearable_context(session_id)

        # Step 5: Optional AI analysis
        ai_result = None
        if self._ai_enabled:
            ai_result = await self._run_ai_analysis(session_id, scale_reading, context_results)

        # Build summary
        summary = {
            "session_id": session_id,
            "recorded_at": scale_reading.recorded_at.isoformat(),
            "weight_kg": scale_reading.weight_kg,
            "available_metrics": scale_reading.available_metrics(),
            "photo_path": str(frame.image_path),
            "silhouette_path": str(silhouette_path) if silhouette_path else None,
            "wearable_sources": [c.source for c in context_results],
            "ai_summary": ai_result.get("summary") if ai_result else None,
        }

        await self.event_bus.emit("session_complete", summary)
        logger.info(
            "─── Session %d complete: %.1f kg, %d metrics, %d wearable sources ───",
            session_id,
            scale_reading.weight_kg,
            len(scale_reading.available_metrics()),
            len(context_results),
        )
        return summary

    async def start_daemon(self) -> None:
        """
        Run as a persistent daemon.
        Listens for scale triggers and optionally fires a daily reminder.
        """
        await self.setup()
        logger.info("Body-tracker daemon running. Step on the scale to trigger a session.")

        scheduler_cfg = self.config.get("scheduler", {})
        reminder_time = scheduler_cfg.get("daily_reminder_time")

        tasks = [self._scale_listen_loop()]
        if reminder_time:
            tasks.append(self._daily_reminder_loop(reminder_time))

        await asyncio.gather(*tasks)

    # ------------------------------------------------------------------
    # Private: measurement flow
    # ------------------------------------------------------------------

    async def _measure_and_capture(self) -> tuple[ScaleReading, CapturedFrame]:
        """
        Start scale.read() and have the camera wait for the scale trigger.
        The CameraAdapter.on_scale_trigger() fires automatically when the
        scale produces a stable reading (via the shared asyncio event).
        """
        trigger_event = asyncio.Event()
        reading_holder: list[ScaleReading] = []

        async def scale_task():
            reading = await self._scale.read()
            reading_holder.append(reading)
            trigger_event.set()
            return reading

        async def camera_task():
            # Wait for scale to signal stable weight
            await trigger_event.wait()
            reading = reading_holder[0]
            frame = await self._camera.on_scale_trigger(reading)
            return frame

        results = await asyncio.gather(scale_task(), camera_task())
        scale_reading: ScaleReading = results[0]
        frame: CapturedFrame = results[1]

        logger.info(
            "Measurement: %.1f kg | %d metrics | photo: %s",
            scale_reading.weight_kg,
            len(scale_reading.available_metrics()),
            frame.image_path.name,
        )
        return scale_reading, frame

    # ------------------------------------------------------------------
    # Private: silhouette extraction
    # ------------------------------------------------------------------

    async def _extract_silhouette(
        self, photo_id: int, frame: CapturedFrame
    ) -> Optional[Path]:
        """
        Run silhouette extraction after photo save.
        Skipped if imaging.enabled is false (default) or if the vision
        dependencies (mediapipe / opencv) are not installed.
        Failures are logged but never crash the session.
        """
        imaging_cfg = self.config.get("imaging", {})
        if not imaging_cfg.get("enabled", False):
            return None

        try:
            from core.imaging import extract_silhouette, SilhouetteExtractionError
            output_dir_str = imaging_cfg.get("output_dir")
            kwargs: dict = {
                "segmentation_model": imaging_cfg.get("segmentation_model", 1),
                "min_detection_confidence": imaging_cfg.get(
                    "min_detection_confidence", 0.5
                ),
            }
            if output_dir_str:
                kwargs["output_dir"] = Path(output_dir_str)

            silhouette_path = await extract_silhouette(frame.image_path, **kwargs)
            await self.db.update_photo_analysis(
                photo_id, silhouette_path=str(silhouette_path)
            )
            logger.info("Silhouette saved: %s", silhouette_path.name)
            return silhouette_path

        except ImportError:
            logger.debug(
                "Imaging skipped — mediapipe/opencv not installed. "
                "Run: pip install body-tracker[vision]"
            )
        except Exception as exc:
            logger.warning("Silhouette extraction failed (non-fatal): %s", exc)

        return None

    # ------------------------------------------------------------------
    # Private: wearable context
    # ------------------------------------------------------------------

    async def _fetch_wearable_context(
        self, session_id: int
    ) -> list[ContextReading]:
        """
        Fetch today's context from all enabled wearable adapters.
        Failures are logged but never raise — wearable data is optional.
        """
        if not self._wearables:
            return []

        today = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

        async def fetch_one(adapter) -> Optional[ContextReading]:
            try:
                async with adapter:
                    ctx = await adapter.fetch_context(today)
                await self.db.save_context(session_id, ctx)
                logger.info(
                    "Context from %s: %d metrics",
                    ctx.source, len(ctx.available_metrics()),
                )
                return ctx
            except Exception as exc:
                logger.warning(
                    "Wearable adapter %s failed (non-fatal): %s",
                    type(adapter).__name__, exc,
                )
                return None

        results = await asyncio.gather(*[fetch_one(w) for w in self._wearables])
        return [r for r in results if r is not None]

    # ------------------------------------------------------------------
    # Private: AI analysis
    # ------------------------------------------------------------------

    async def _run_ai_analysis(
        self,
        session_id: int,
        reading: ScaleReading,
        context: list[ContextReading],
    ) -> Optional[dict]:
        """
        Build a prompt from today's data + recent history and call the AI.
        Saves the result to ai_insights table.
        """
        try:
            from core.analysis import analyse_session
            result = await analyse_session(
                db=self.db,
                session_id=session_id,
                reading=reading,
                context=context,
                config=self.config.get("ai", {}),
            )
            return result
        except Exception as exc:
            logger.warning("AI analysis failed (non-fatal): %s", exc)
            return None

    # ------------------------------------------------------------------
    # Private: daemon loops
    # ------------------------------------------------------------------

    async def _scale_listen_loop(self) -> None:
        """Continuously wait for scale readings (persistent daemon mode)."""
        while True:
            try:
                logger.debug("Waiting for scale trigger…")
                await self.run_session()
                # Brief cooldown to avoid double-triggers
                await asyncio.sleep(30)
            except KeyboardInterrupt:
                break
            except Exception as exc:
                logger.error("Session error: %s — retrying in 60s", exc)
                await asyncio.sleep(60)

    async def _daily_reminder_loop(self, time_str: str) -> None:
        """
        Emit a reminder event at the configured time if no session has
        been recorded today. Uses simple polling (checks every minute).
        """
        import datetime as dt

        hour, minute = (int(x) for x in time_str.split(":"))

        while True:
            now = dt.datetime.now()
            if now.hour == hour and now.minute == minute:
                # Check if session already recorded today
                sessions = await self.db.get_sessions(self._profile_id, limit=1)
                if sessions:
                    last = sessions[0]["recorded_at"]
                    last_dt = dt.datetime.fromisoformat(last)
                    if last_dt.date() == now.date():
                        await asyncio.sleep(60)
                        continue

                logger.info("Daily reminder: no session recorded yet today")
                await self.event_bus.emit("daily_reminder", {
                    "message": "Time for your daily measurement!",
                    "time": now.isoformat(),
                })
            await asyncio.sleep(60)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def config_get(config: dict, key: str, default=None):
    """Safe nested config access."""
    return config.get(key, default)
