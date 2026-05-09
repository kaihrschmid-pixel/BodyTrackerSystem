"""
adapters/cameras/v4l2.py
========================
USB/V4L2 camera adapter — works with any UVC-compatible device:
  - USB webcams (Logitech, Microsoft, generic)
  - Built-in laptop cameras
  - Pi Camera via V4L2 driver (legacy)

Uses OpenCV (cv2) which abstracts V4L2 on Linux, DirectShow on Windows,
and AVFoundation on macOS — so this adapter actually works cross-platform
despite the V4L2 name.

Requires: pip install opencv-python-headless

Configuration:
    hardware:
      camera:
        adapter: "v4l2"
        device: 0                    # int index OR "/dev/video0" string
        resolution: [1920, 1080]     # [width, height]
        stabilisation_delay_s: 3
        photos_path: "data/photos"
        warmup_frames: 5             # discard first N frames (auto-exposure settle)
"""

from __future__ import annotations

import logging
from datetime import timezone
from pathlib import Path

from adapters.base import CameraAdapter, CapturedFrame, utcnow

logger = logging.getLogger(__name__)


class V4L2CameraAdapter(CameraAdapter):
    """
    OpenCV-based camera adapter for USB/V4L2 cameras.

    Handles:
    - Device open/close
    - Resolution setting
    - Auto-exposure warmup (discards first N frames)
    - JPEG save with timestamped filename
    - Directory creation
    """

    ADAPTER_NAME = "v4l2"

    def __init__(self, config: dict):
        super().__init__(config)
        # Accept either int index or "/dev/video0" string
        device = config.get("device", 0)
        self._device_index = int(device) if str(device).lstrip("-").isdigit() else device
        resolution = config.get("resolution", [1920, 1080])
        self._width, self._height = resolution[0], resolution[1]
        self._warmup_frames: int = config.get("warmup_frames", 5)
        self._cap = None  # cv2.VideoCapture instance

    # ------------------------------------------------------------------
    # CameraAdapter interface
    # ------------------------------------------------------------------

    async def open(self) -> None:
        try:
            import cv2
        except ImportError:
            raise ImportError(
                "opencv-python-headless is required for the V4L2 camera adapter. "
                "Install: pip install opencv-python-headless"
            )

        import cv2

        logger.info("Opening camera device %r at %dx%d", self._device_index, self._width, self._height)
        self._cap = cv2.VideoCapture(self._device_index)

        if not self._cap.isOpened():
            raise RuntimeError(
                f"Could not open camera device {self._device_index!r}. "
                "Check: ls /dev/video* | Check the device index in config."
            )

        # Set resolution
        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, self._width)
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self._height)

        # Read back actual resolution (camera may not support the requested one)
        actual_w = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if actual_w != self._width or actual_h != self._height:
            logger.warning(
                "Camera does not support %dx%d — using %dx%d instead",
                self._width, self._height, actual_w, actual_h,
            )
            self._width, self._height = actual_w, actual_h

        # Warmup: discard first N frames so auto-exposure/white-balance settle
        logger.debug("Camera warmup (%d frames)…", self._warmup_frames)
        for _ in range(self._warmup_frames):
            self._cap.read()

        logger.info(
            "Camera ready: device=%r  resolution=%dx%d",
            self._device_index, self._width, self._height,
        )

    async def capture(self, angle: str = "front") -> CapturedFrame:
        """Capture a single frame and save as JPEG."""
        if self._cap is None or not self._cap.isOpened():
            raise RuntimeError("Camera is not open. Call open() first.")

        import cv2

        ret, frame = self._cap.read()
        if not ret or frame is None:
            raise RuntimeError(
                "Failed to capture frame. "
                "Check camera connection and ensure no other process is using it."
            )

        now = utcnow()
        filename = f"{now.strftime('%Y%m%d_%H%M%S')}_{angle}.jpg"
        output_dir = self._output_dir / now.strftime("%Y/%m")
        output_dir.mkdir(parents=True, exist_ok=True)
        filepath = output_dir / filename

        # Save with high quality JPEG
        encode_params = [cv2.IMWRITE_JPEG_QUALITY, 92]
        success = cv2.imwrite(str(filepath), frame, encode_params)
        if not success:
            raise RuntimeError(f"Failed to save image to {filepath}")

        h, w = frame.shape[:2]
        logger.info("Captured %s: %s (%dx%d)", angle, filepath, w, h)

        return CapturedFrame(
            captured_at=now,
            image_path=filepath,
            angle=angle,
            width_px=w,
            height_px=h,
            adapter_name=self.ADAPTER_NAME,
        )

    async def close(self) -> None:
        if self._cap:
            self._cap.release()
            self._cap = None
            logger.debug("Camera released")

    def is_available(self) -> bool:
        """
        Quick availability check without opening the device.
        On Linux: check if /dev/videoN exists.
        On other platforms: always return True (can't check without opening).
        """
        import platform
        if platform.system() == "Linux":
            device = self._device_index
            if isinstance(device, int):
                return Path(f"/dev/video{device}").exists()
            return Path(device).exists()
        return True  # Windows/macOS — assume available
