"""
adapters/cameras/realsense.py
==============================
Intel RealSense depth camera adapter — captures RGB + depth map simultaneously.

Compatible cameras:
  - Intel RealSense D415  (narrow FOV, best for 1–4 m, structured light)
  - Intel RealSense D435i (wider FOV + IMU, best all-round choice)
  - Intel RealSense D455  (wider baseline, better at >1.5 m)
  - Intel RealSense L515  (LiDAR, higher accuracy but shorter range, EOL)

Depth map usage:
  The 16-bit PNG depth map saved alongside each RGB photo contains
  per-pixel distance in millimetres. Post-processing can use this to:
    - Improve background segmentation (depth-gated mask)
    - Estimate body circumferences (waist, hip, chest) from 3D point cloud
    - Calculate body volume for density-based lean mass estimation

Requires:
  Intel RealSense SDK 2.0: https://github.com/IntelRealSense/librealsense
  Python wrapper:          pip install pyrealsense2

Setup:
  - Camera must be connected via USB 3.0 (blue port)
  - On Linux: add udev rules from librealsense (run scripts/setup_udev_rules.sh)
  - Raspberry Pi 4/5: USB bandwidth may limit depth resolution; use 848×480
  - On x86: 1280×720 depth works reliably

Configuration:
    hardware:
      camera:
        adapter: "realsense"
        photos_path: "data/photos"
        resolution: [1280, 720]
        fps: 30
        stabilisation_delay_s: 4
        depth_preset: "HIGH_ACCURACY"   # DEFAULT | HIGH_ACCURACY | HIGH_DENSITY | MEDIUM_DENSITY
        save_depth_map: true
        depth_clip_m: 4.0
        emitter_enabled: true

Usage:
    async with RealSenseCameraAdapter(config["hardware"]["camera"]) as cam:
        frame = await cam.capture(angle="front")
        # frame.depth_map_path is set when save_depth_map=true
"""

from __future__ import annotations

import asyncio
import logging
import struct
import zlib
from pathlib import Path
from typing import Optional

from adapters.base import CameraAdapter, CapturedFrame, utcnow

logger = logging.getLogger(__name__)

# Maps config string → pyrealsense2 preset enum value (RS2_RS400_VISUAL_PRESET_*)
_DEPTH_PRESETS = {
    "DEFAULT":        0,
    "HAND":           1,
    "HIGH_ACCURACY":  3,
    "HIGH_DENSITY":   4,
    "MEDIUM_DENSITY": 5,
}


class RealSenseCameraAdapter(CameraAdapter):
    """
    Captures aligned RGB + depth frames from an Intel RealSense camera.

    Both streams are aligned to the RGB sensor coordinate system so that
    each depth pixel corresponds exactly to the matching RGB pixel.

    All pyrealsense2 calls are blocking and dispatched to a thread-pool
    executor so they do not block the asyncio event loop.
    """

    ADAPTER_NAME = "realsense"

    def __init__(self, config: dict):
        super().__init__(config)
        res = config.get("resolution", [1280, 720])
        self._width: int = int(res[0])
        self._height: int = int(res[1])
        self._fps: int = config.get("fps", 30)
        self._depth_preset: str = config.get("depth_preset", "HIGH_ACCURACY").upper()
        self._save_depth_map: bool = config.get("save_depth_map", True)
        self._depth_clip_m: float = config.get("depth_clip_m", 4.0)
        self._emitter_enabled: bool = config.get("emitter_enabled", True)
        self._pipeline = None   # rs2.pipeline
        self._align = None      # rs2.align

    # ------------------------------------------------------------------
    # CameraAdapter interface
    # ------------------------------------------------------------------

    async def open(self) -> None:
        """Configure and start the RealSense pipeline."""
        await asyncio.get_event_loop().run_in_executor(None, self._open_sync)
        self._output_dir.mkdir(parents=True, exist_ok=True)
        logger.info(
            "RealSense opened: %dx%d @ %d fps, preset=%s",
            self._width, self._height, self._fps, self._depth_preset,
        )

    async def capture(self, angle: str = "front") -> CapturedFrame:
        """Capture one aligned RGB+depth frame and save both to disk."""
        if self._pipeline is None:
            raise RuntimeError("Call open() before capture()")

        now = utcnow()
        output_dir = self._output_dir / now.strftime("%Y/%m")
        output_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{now.strftime('%Y%m%d_%H%M%S')}_{angle}_realsense"

        rgb_path = output_dir / f"{stem}.jpg"
        depth_path = output_dir / f"{stem}_depth.png" if self._save_depth_map else None

        await asyncio.get_event_loop().run_in_executor(
            None, self._capture_sync, rgb_path, depth_path
        )

        logger.info("RealSense captured: %s", rgb_path.name)
        return CapturedFrame(
            captured_at=now,
            image_path=rgb_path,
            angle=angle,
            width_px=self._width,
            height_px=self._height,
            depth_map_path=depth_path,
            adapter_name=self.ADAPTER_NAME,
        )

    async def close(self) -> None:
        """Stop the RealSense pipeline."""
        if self._pipeline is not None:
            await asyncio.get_event_loop().run_in_executor(None, self._close_sync)

    def is_available(self) -> bool:
        """True if pyrealsense2 is importable and at least one device is connected."""
        try:
            import pyrealsense2 as rs
            ctx = rs.context()
            return len(ctx.devices) > 0
        except (ImportError, Exception):
            return False

    # ------------------------------------------------------------------
    # Sync helpers (run in executor)
    # ------------------------------------------------------------------

    def _open_sync(self) -> None:
        """Blocking: configure and start the RealSense pipeline."""
        import pyrealsense2 as rs

        self._pipeline = rs.pipeline()
        cfg = rs.config()

        # Enable both streams at the requested resolution
        cfg.enable_stream(rs.stream.color, self._width, self._height,
                          rs.format.bgr8, self._fps)
        cfg.enable_stream(rs.stream.depth, self._width, self._height,
                          rs.format.z16, self._fps)

        profile = self._pipeline.start(cfg)

        # Apply depth visual preset
        depth_sensor = profile.get_device().first_depth_sensor()
        preset_val = _DEPTH_PRESETS.get(self._depth_preset, 0)
        depth_sensor.set_option(rs.option.visual_preset, preset_val)
        depth_sensor.set_option(rs.option.emitter_enabled,
                                1.0 if self._emitter_enabled else 0.0)

        # Align depth to colour sensor so pixels correspond 1:1
        self._align = rs.align(rs.stream.color)

        # Warm up: discard first few frames while AGC/AWB settle
        import time
        time.sleep(1.5)
        for _ in range(10):
            self._pipeline.wait_for_frames()
        logger.debug("RealSense pipeline warmed up")

    def _capture_sync(self, rgb_path: Path, depth_path: Optional[Path]) -> None:
        """Blocking: grab aligned frames and save RGB as JPEG, depth as 16-bit PNG."""
        import pyrealsense2 as rs
        import numpy as np

        # Discard a few frames to get a fresh one
        for _ in range(3):
            self._pipeline.wait_for_frames()

        frames = self._pipeline.wait_for_frames()
        aligned = self._align.process(frames)

        # --- RGB ---
        colour_frame = aligned.get_color_frame()
        if not colour_frame:
            raise RuntimeError("RealSense: no colour frame received")

        colour_data = np.asanyarray(colour_frame.get_data())  # BGR uint8

        # Encode as JPEG using cv2 if available, else fall back to png-in-jpg trick
        try:
            import cv2
            cv2.imwrite(str(rgb_path), colour_data,
                        [cv2.IMWRITE_JPEG_QUALITY, 92])
        except ImportError:
            # Minimal PNG fallback (no cv2)
            _write_png_rgb(rgb_path, colour_data)

        # --- Depth map (16-bit PNG) ---
        if depth_path is not None:
            depth_frame = aligned.get_depth_frame()
            if depth_frame:
                # Clip to depth_clip_m to remove background
                clip_mm = int(self._depth_clip_m * 1000)
                depth_data = np.asanyarray(depth_frame.get_data())  # uint16, mm
                depth_data[depth_data > clip_mm] = 0
                _write_depth_png(depth_path, depth_data)

    def _close_sync(self) -> None:
        """Blocking: stop the pipeline."""
        try:
            self._pipeline.stop()
        except Exception as exc:
            logger.warning("Error stopping RealSense pipeline: %s", exc)
        finally:
            self._pipeline = None
            self._align = None


# ---------------------------------------------------------------------------
# Minimal PNG writers (no Pillow/cv2 required as fallback)
# ---------------------------------------------------------------------------

def _write_png_rgb(path: Path, bgr: "np.ndarray") -> None:
    """Write an RGB image as PNG using only Python built-ins."""
    import numpy as np
    h, w = bgr.shape[:2]
    rgb = bgr[:, :, ::-1]  # BGR → RGB

    def pack_chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = pack_chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    raw_rows = b"".join(b"\x00" + bytes(row.flatten()) for row in rgb)
    idat = pack_chunk(b"IDAT", zlib.compress(raw_rows, 1))
    iend = pack_chunk(b"IEND", b"")
    path.write_bytes(sig + ihdr + idat + iend)


def _write_depth_png(path: Path, depth_mm: "np.ndarray") -> None:
    """
    Write a 16-bit greyscale PNG depth map.
    Each pixel value = distance in millimetres (0 = no data / clipped).
    """
    import numpy as np
    import struct, zlib
    h, w = depth_mm.shape

    def pack_chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    sig = b"\x89PNG\r\n\x1a\n"
    # color type 0 = greyscale, bit depth 16
    ihdr = pack_chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 16, 0, 0, 0, 0))

    # Build raw image: filter byte (0) + big-endian uint16 per pixel
    rows = []
    for row in depth_mm:
        row_bytes = b"\x00" + struct.pack(f">{w}H", *row.astype(np.uint16).tolist())
        rows.append(row_bytes)
    idat = pack_chunk(b"IDAT", zlib.compress(b"".join(rows), 1))
    iend = pack_chunk(b"IEND", b"")
    path.write_bytes(sig + ihdr + idat + iend)
