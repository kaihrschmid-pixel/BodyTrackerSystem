"""
adapters/cameras/picamera2.py
==============================
Raspberry Pi CSI camera adapter using the libcamera-based picamera2 library.

Compatible with:
  - Raspberry Pi Camera Module 2 (IMX219, 8 MP)
  - Raspberry Pi Camera Module 3 (IMX708, 12 MP, AF)
  - HQ Camera (IMX477, 12.3 MP)
  - Any CSI camera supported by libcamera on Raspberry Pi OS

Requires:
  - Raspberry Pi with camera enabled (raspi-config → Interface Options → Camera)
  - picamera2 package: already available on Raspberry Pi OS Bookworm,
    or install with: sudo apt install python3-picamera2

Configuration:
    hardware:
      camera:
        adapter: "picamera2"
        photos_path: "data/photos"
        resolution: [1920, 1080]        # default: full HD
        stabilisation_delay_s: 3       # seconds to wait after scale trigger
        camera_num: 0                  # camera index (0 = first/only camera)
        hflip: false                   # mirror horizontally
        vflip: false                   # mirror vertically
        tuning_file: null              # path to custom libcamera tuning JSON

Usage:
    async with PiCamera2Adapter(config["hardware"]["camera"]) as cam:
        frame = await cam.capture(angle="front")
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

from adapters.base import CameraAdapter, CapturedFrame, utcnow

logger = logging.getLogger(__name__)


class PiCamera2Adapter(CameraAdapter):
    """
    Captures images from the Raspberry Pi CSI camera using picamera2.

    picamera2 is a blocking library; all camera calls are dispatched to a
    thread-pool executor so they don't block the asyncio event loop.
    """

    ADAPTER_NAME = "picamera2"

    def __init__(self, config: dict):
        super().__init__(config)
        self._resolution: tuple[int, int] = tuple(config.get("resolution", [1920, 1080]))  # type: ignore[assignment]
        self._camera_num: int = config.get("camera_num", 0)
        self._hflip: bool = config.get("hflip", False)
        self._vflip: bool = config.get("vflip", False)
        self._tuning_file: Optional[str] = config.get("tuning_file")
        self._cam = None   # picamera2.Picamera2 instance

    # ------------------------------------------------------------------
    # CameraAdapter interface
    # ------------------------------------------------------------------

    async def open(self) -> None:
        """Initialise the picamera2 device and apply configuration."""
        await asyncio.get_event_loop().run_in_executor(None, self._open_sync)
        self._output_dir.mkdir(parents=True, exist_ok=True)
        logger.info(
            "PiCamera2 opened: camera_num=%d, resolution=%s",
            self._camera_num, self._resolution,
        )

    async def capture(self, angle: str = "front") -> CapturedFrame:
        """Capture a single JPEG image and save it to photos_path."""
        if self._cam is None:
            raise RuntimeError("Call open() before capture()")

        now = utcnow()
        output_dir = self._output_dir / now.strftime("%Y/%m")
        output_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{now.strftime('%Y%m%d_%H%M%S')}_{angle}_picam.jpg"
        filepath = output_dir / filename

        await asyncio.get_event_loop().run_in_executor(
            None, self._capture_sync, filepath
        )

        w, h = self._resolution
        logger.info("PiCamera2 captured: %s", filepath)
        return CapturedFrame(
            captured_at=now,
            image_path=filepath,
            angle=angle,
            width_px=w,
            height_px=h,
            adapter_name=self.ADAPTER_NAME,
        )

    async def close(self) -> None:
        """Release the camera device."""
        if self._cam is not None:
            await asyncio.get_event_loop().run_in_executor(None, self._close_sync)

    def is_available(self) -> bool:
        """
        True if picamera2 is importable.
        Does NOT check whether the CSI camera is physically connected —
        that would require opening the device.
        """
        try:
            import picamera2  # noqa: F401
            return True
        except ImportError:
            return False

    # ------------------------------------------------------------------
    # Sync helpers (run in executor)
    # ------------------------------------------------------------------

    def _open_sync(self) -> None:
        """Blocking: initialise picamera2 and start the camera."""
        # Lazy import — only available on Raspberry Pi
        import picamera2
        from picamera2 import Picamera2

        kwargs: dict = {"camera_num": self._camera_num}
        if self._tuning_file:
            tuning = Picamera2.load_tuning_file(self._tuning_file)
            kwargs["tuning"] = tuning

        self._cam = Picamera2(**kwargs)

        # Build a still-capture configuration at the requested resolution
        config = self._cam.create_still_configuration(
            main={"size": self._resolution, "format": "RGB888"},
        )
        self._cam.configure(config)

        # Apply orientation transforms
        from libcamera import Transform
        self._cam.set_controls({
            "Transform": Transform(hflip=int(self._hflip), vflip=int(self._vflip))
        })

        self._cam.start()
        logger.debug("picamera2 started — warming up")
        import time
        time.sleep(1.0)  # allow AGC / AWB to settle

    def _capture_sync(self, filepath: Path) -> None:
        """Blocking: capture one frame and write JPEG to disk."""
        import io

        # picamera2 can save directly to a file path as JPEG
        self._cam.capture_file(str(filepath))

    def _close_sync(self) -> None:
        """Blocking: stop and release the camera."""
        try:
            self._cam.stop()
            self._cam.close()
        except Exception as exc:
            logger.warning("Error closing picamera2: %s", exc)
        finally:
            self._cam = None
