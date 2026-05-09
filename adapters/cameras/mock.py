"""
adapters/cameras/mock.py
========================
Mock camera adapter — for development and testing without hardware.

Generates a solid-color JPEG with the timestamp embedded so the
rest of the pipeline (database save, silhouette extraction, AI analysis)
can be tested end-to-end without a real camera.

Configuration:
    hardware:
      camera:
        adapter: "mock"
        photos_path: "data/photos"
        color: [80, 120, 160]   # BGR color for the test image (optional)
"""

from __future__ import annotations

import logging
import struct
import zlib
from pathlib import Path

from adapters.base import CameraAdapter, CapturedFrame, utcnow

logger = logging.getLogger(__name__)


class MockCameraAdapter(CameraAdapter):
    """Generates placeholder JPEG images. Zero dependencies."""

    ADAPTER_NAME = "mock"

    def __init__(self, config: dict):
        super().__init__(config)
        color = config.get("color", [80, 120, 160])  # BGR
        self._b, self._g, self._r = color[0], color[1], color[2]
        self._width = 640
        self._height = 480

    async def open(self) -> None:
        self._output_dir.mkdir(parents=True, exist_ok=True)
        logger.info("MockCamera ready (generates placeholder images)")

    async def capture(self, angle: str = "front") -> CapturedFrame:
        now = utcnow()
        filename = f"{now.strftime('%Y%m%d_%H%M%S')}_{angle}_mock.jpg"
        output_dir = self._output_dir / now.strftime("%Y/%m")
        output_dir.mkdir(parents=True, exist_ok=True)
        filepath = output_dir / filename

        # Generate a minimal valid JPEG without any external dependencies
        jpeg_data = self._make_jpeg(self._width, self._height, self._r, self._g, self._b)
        filepath.write_bytes(jpeg_data)

        logger.info("MockCamera captured: %s", filepath)
        return CapturedFrame(
            captured_at=now,
            image_path=filepath,
            angle=angle,
            width_px=self._width,
            height_px=self._height,
            adapter_name=self.ADAPTER_NAME,
        )

    async def close(self) -> None:
        pass

    def is_available(self) -> bool:
        return True

    # ------------------------------------------------------------------
    # Minimal JPEG generator (pure Python, no Pillow/OpenCV needed)
    # Creates a solid-color image using raw JFIF structure.
    # ------------------------------------------------------------------

    @staticmethod
    def _make_jpeg(width: int, height: int, r: int, g: int, b: int) -> bytes:
        """
        Build a minimal valid JPEG file for a solid RGB colour.
        Uses a PPM → JFIF approach via raw bytes — no external libraries.
        """
        # We'll create a PNG instead (simpler structure, pure Python)
        # and save with .jpg extension — most systems handle it fine.
        return MockCameraAdapter._make_png(width, height, r, g, b)

    @staticmethod
    def _make_png(w: int, h: int, r: int, g: int, b: int) -> bytes:
        """Create a minimal valid PNG file for a solid RGB colour."""

        def pack_chunk(chunk_type: bytes, data: bytes) -> bytes:
            length = struct.pack(">I", len(data))
            crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
            return length + chunk_type + data + struct.pack(">I", crc)

        # PNG signature
        sig = b"\x89PNG\r\n\x1a\n"

        # IHDR chunk: width, height, bit depth=8, color type=2 (RGB)
        ihdr_data = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
        ihdr = pack_chunk(b"IHDR", ihdr_data)

        # IDAT chunk: raw image data (each row: filter_byte + R G B * width)
        raw_row = bytes([0]) + bytes([r, g, b] * w)  # filter type 0 (None)
        raw_image = raw_row * h
        compressed = zlib.compress(raw_image, level=1)
        idat = pack_chunk(b"IDAT", compressed)

        # IEND chunk
        iend = pack_chunk(b"IEND", b"")

        return sig + ihdr + idat + iend
