"""
adapters/cameras/rtsp.py
========================
RTSP / IP camera adapter — turns any smartphone or network camera into a
body-tracker capture device.

Supported sources:
  - Android: DroidCam (free), IP Webcam (free), EpocCam
  - iOS:     Camo, EpocCam, IP Camera Lite
  - Network cameras: any camera exposing an RTSP stream
  - Virtual cameras: OBS virtual camera via v4l2loopback

The adapter opens an RTSP stream with OpenCV, grabs a single frame at
capture time, and writes it as JPEG to photos_path. The stream stays
open between captures to avoid re-connection overhead.

Requires:
  - opencv-python-headless: pip install opencv-python-headless
    (already in the `vision` optional dependency group)

Configuration:
    hardware:
      camera:
        adapter: "rtsp"
        rtsp_url: "rtsp://192.168.1.42:8080/h264_pcm.sdp"  # DroidCam example
        photos_path: "data/photos"
        stabilisation_delay_s: 3
        resolution: [1920, 1080]   # optional: resize after capture
        jpeg_quality: 92           # 0–100, default 92
        reconnect_attempts: 3      # retries on stream drop

Popular app URLs:
  DroidCam (Android/iOS):  rtsp://<phone-ip>:4747/h264_pcm.sdp
  IP Webcam (Android):     rtsp://<phone-ip>:8080/h264_pcm.sdp
  Camo (iOS, USB):         rtsp://127.0.0.1:7799/live
  Generic IP camera:       rtsp://user:pass@<ip>:<port>/stream
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

from adapters.base import CameraAdapter, CapturedFrame, utcnow

logger = logging.getLogger(__name__)


class RTSPCameraAdapter(CameraAdapter):
    """
    Captures frames from an RTSP stream using OpenCV's VideoCapture.

    The stream is opened in open() and kept alive across multiple capture()
    calls. If the connection drops, the adapter tries to reconnect up to
    `reconnect_attempts` times before raising.
    """

    ADAPTER_NAME = "rtsp"

    def __init__(self, config: dict):
        super().__init__(config)
        self._rtsp_url: str = config.get("rtsp_url", "")
        self._target_resolution: Optional[tuple[int, int]] = (
            tuple(config["resolution"]) if config.get("resolution") else None  # type: ignore[assignment]
        )
        self._jpeg_quality: int = config.get("jpeg_quality", 92)
        self._reconnect_attempts: int = config.get("reconnect_attempts", 3)
        self._cap = None  # cv2.VideoCapture instance

    # ------------------------------------------------------------------
    # CameraAdapter interface
    # ------------------------------------------------------------------

    async def open(self) -> None:
        """Open the RTSP stream."""
        if not self._rtsp_url:
            raise ValueError(
                "RTSP adapter requires rtsp_url in config. "
                "Example: rtsp://192.168.1.42:8080/h264_pcm.sdp"
            )
        await asyncio.get_event_loop().run_in_executor(None, self._open_stream)
        self._output_dir.mkdir(parents=True, exist_ok=True)
        logger.info("RTSP stream opened: %s", self._rtsp_url)

    async def capture(self, angle: str = "front") -> CapturedFrame:
        """Grab one frame from the stream and save as JPEG."""
        if self._cap is None:
            raise RuntimeError("Call open() before capture()")

        now = utcnow()
        output_dir = self._output_dir / now.strftime("%Y/%m")
        output_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{now.strftime('%Y%m%d_%H%M%S')}_{angle}_rtsp.jpg"
        filepath = output_dir / filename

        width, height = await asyncio.get_event_loop().run_in_executor(
            None, self._grab_frame, filepath
        )

        logger.info("RTSP captured: %s (%dx%d)", filepath, width, height)
        return CapturedFrame(
            captured_at=now,
            image_path=filepath,
            angle=angle,
            width_px=width,
            height_px=height,
            adapter_name=self.ADAPTER_NAME,
        )

    async def close(self) -> None:
        """Release the RTSP stream."""
        if self._cap is not None:
            await asyncio.get_event_loop().run_in_executor(None, self._close_stream)

    def is_available(self) -> bool:
        """True if OpenCV is importable and an rtsp_url is configured."""
        if not self._rtsp_url:
            return False
        try:
            import cv2  # noqa: F401
            return True
        except ImportError:
            return False

    # ------------------------------------------------------------------
    # Sync helpers (run in executor)
    # ------------------------------------------------------------------

    def _open_stream(self) -> None:
        """Blocking: open the RTSP stream with OpenCV."""
        import cv2

        # RTSP streams benefit from lower-latency transport
        self._cap = cv2.VideoCapture(self._rtsp_url, cv2.CAP_FFMPEG)

        # Reduce latency: prefer real-time frames over buffering
        self._cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        if not self._cap.isOpened():
            self._cap = None
            raise RuntimeError(
                f"Could not open RTSP stream: {self._rtsp_url}\n"
                "Check that:\n"
                "  1. Phone and Pi are on the same WiFi network\n"
                "  2. The camera app is running and streaming\n"
                "  3. The IP address and port are correct"
            )

    def _grab_frame(self, filepath: Path) -> tuple[int, int]:
        """
        Blocking: grab the most recent frame (discard buffer), encode as JPEG.
        Returns (width, height).
        """
        import cv2
        import numpy as np

        # Drain stale buffer frames to get the most recent one
        for _ in range(3):
            self._cap.grab()

        ret, frame = self._cap.retrieve()
        if not ret or frame is None:
            # Try to reconnect once
            for attempt in range(self._reconnect_attempts):
                logger.warning(
                    "RTSP frame grab failed, reconnecting (attempt %d/%d)…",
                    attempt + 1, self._reconnect_attempts,
                )
                import time
                time.sleep(1.0)
                self._open_stream()
                self._cap.grab()
                ret, frame = self._cap.retrieve()
                if ret and frame is not None:
                    break
            else:
                raise RuntimeError(
                    f"RTSP stream dropped after {self._reconnect_attempts} reconnect attempts: "
                    f"{self._rtsp_url}"
                )

        # Optional resize
        if self._target_resolution:
            w, h = self._target_resolution
            frame = cv2.resize(frame, (w, h), interpolation=cv2.INTER_LANCZOS4)
        else:
            h, w = frame.shape[:2]

        # Encode and save as JPEG
        encode_params = [cv2.IMWRITE_JPEG_QUALITY, self._jpeg_quality]
        success, buf = cv2.imencode(".jpg", frame, encode_params)
        if not success:
            raise RuntimeError("cv2.imencode failed — could not encode frame as JPEG")

        filepath.write_bytes(buf.tobytes())
        return int(w), int(h)

    def _close_stream(self) -> None:
        """Blocking: release the VideoCapture."""
        try:
            self._cap.release()
        except Exception as exc:
            logger.warning("Error releasing RTSP stream: %s", exc)
        finally:
            self._cap = None
