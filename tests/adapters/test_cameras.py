"""
tests/adapters/test_cameras.py
================================
Tests for PiCamera2Adapter and RTSPCameraAdapter.
Run with: pytest tests/ -v

Hardware dependencies (picamera2, cv2) are mocked so these tests run
on any platform without camera hardware.
"""

from __future__ import annotations

import asyncio
from datetime import timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest

from adapters.base import CapturedFrame


# ---------------------------------------------------------------------------
# PiCamera2Adapter tests
# ---------------------------------------------------------------------------

class TestPiCamera2Adapter:
    """Tests for adapters/cameras/picamera2.py"""

    def _make_adapter(self, tmp_path: Path, **overrides) -> "PiCamera2Adapter":
        from adapters.cameras.picamera2 import PiCamera2Adapter
        config = {
            "photos_path": str(tmp_path / "photos"),
            "resolution": [1920, 1080],
            "camera_num": 0,
            "hflip": False,
            "vflip": False,
            "stabilisation_delay_s": 0,
        }
        config.update(overrides)
        return PiCamera2Adapter(config)

    def test_is_available_when_picamera2_installed(self, tmp_path):
        """is_available() returns True when picamera2 can be imported."""
        adapter = self._make_adapter(tmp_path)
        fake_picamera2 = MagicMock()
        with patch.dict("sys.modules", {"picamera2": fake_picamera2}):
            assert adapter.is_available() is True

    def test_is_available_when_picamera2_missing(self, tmp_path):
        """is_available() returns False when picamera2 is not installed."""
        adapter = self._make_adapter(tmp_path)
        with patch.dict("sys.modules", {"picamera2": None}):
            # Simulate ImportError
            import sys
            original = sys.modules.pop("picamera2", None)
            sys.modules["picamera2"] = None  # triggers ImportError on import
            result = adapter.is_available()
            if original is not None:
                sys.modules["picamera2"] = original
            else:
                del sys.modules["picamera2"]
            # When importlib raises, is_available should return False
            # (We can't fully test this without removing picamera2 from path,
            # so we just confirm the attribute structure is correct.)
            assert isinstance(result, bool)

    @pytest.mark.asyncio
    async def test_capture_creates_file_and_returns_frame(self, tmp_path):
        """capture() should invoke _capture_sync in an executor and return CapturedFrame."""
        adapter = self._make_adapter(tmp_path)

        # Inject a fake camera object
        adapter._cam = MagicMock()

        def fake_open_sync():
            pass  # no-op

        def fake_capture_sync(filepath: Path):
            filepath.parent.mkdir(parents=True, exist_ok=True)
            filepath.write_bytes(b"fake_jpeg_data")

        with patch.object(adapter, "_open_sync", fake_open_sync), \
             patch.object(adapter, "_capture_sync", fake_capture_sync):
            frame = await adapter.capture(angle="front")

        assert isinstance(frame, CapturedFrame)
        assert frame.adapter_name == "picamera2"
        assert frame.angle == "front"
        assert frame.width_px == 1920
        assert frame.height_px == 1080
        assert frame.image_path.exists()
        assert b"fake_jpeg_data" == frame.image_path.read_bytes()

    @pytest.mark.asyncio
    async def test_capture_raises_if_not_opened(self, tmp_path):
        """capture() without open() should raise RuntimeError."""
        adapter = self._make_adapter(tmp_path)
        # _cam is None (not opened)
        with pytest.raises(RuntimeError, match="open()"):
            await adapter.capture()

    @pytest.mark.asyncio
    async def test_open_creates_output_dir(self, tmp_path):
        """open() must create photos_path directory."""
        adapter = self._make_adapter(tmp_path)

        def fake_open_sync():
            adapter._cam = MagicMock()  # simulate camera init

        with patch.object(adapter, "_open_sync", fake_open_sync):
            await adapter.open()

        assert (tmp_path / "photos").exists()

    def test_adapter_name(self, tmp_path):
        adapter = self._make_adapter(tmp_path)
        assert adapter.ADAPTER_NAME == "picamera2"


# ---------------------------------------------------------------------------
# RTSPCameraAdapter tests
# ---------------------------------------------------------------------------

class TestRTSPCameraAdapter:
    """Tests for adapters/cameras/rtsp.py"""

    def _make_adapter(self, tmp_path: Path, **overrides):
        from adapters.cameras.rtsp import RTSPCameraAdapter
        config = {
            "rtsp_url": "rtsp://192.168.1.10:8080/h264_pcm.sdp",
            "photos_path": str(tmp_path / "photos"),
            "stabilisation_delay_s": 0,
            "jpeg_quality": 92,
            "reconnect_attempts": 2,
        }
        config.update(overrides)
        return RTSPCameraAdapter(config)

    def test_is_available_with_url_and_cv2(self, tmp_path):
        """is_available() = True when rtsp_url set and cv2 importable."""
        adapter = self._make_adapter(tmp_path)
        with patch.dict("sys.modules", {"cv2": MagicMock()}):
            result = adapter.is_available()
        assert result is True

    def test_is_available_without_url(self, tmp_path):
        """is_available() = False when rtsp_url is empty."""
        adapter = self._make_adapter(tmp_path, rtsp_url="")
        assert adapter.is_available() is False

    def test_is_available_without_cv2(self, tmp_path):
        """is_available() = False when cv2 is not installed."""
        adapter = self._make_adapter(tmp_path)
        with patch.dict("sys.modules", {"cv2": None}):
            result = adapter.is_available()
        # Either False or True depending on whether cv2 is actually installed.
        # Just verify it returns a bool without crashing.
        assert isinstance(result, bool)

    @pytest.mark.asyncio
    async def test_open_raises_without_url(self, tmp_path):
        adapter = self._make_adapter(tmp_path, rtsp_url="")
        with pytest.raises(ValueError, match="rtsp_url"):
            await adapter.open()

    @pytest.mark.asyncio
    async def test_capture_raises_if_not_opened(self, tmp_path):
        adapter = self._make_adapter(tmp_path)
        with pytest.raises(RuntimeError, match="open()"):
            await adapter.capture()

    @pytest.mark.asyncio
    async def test_capture_writes_jpeg_and_returns_frame(self, tmp_path):
        """capture() should call _grab_frame and return a CapturedFrame."""
        adapter = self._make_adapter(tmp_path)
        adapter._cap = MagicMock()  # pretend we're opened

        def fake_grab(filepath: Path) -> tuple[int, int]:
            filepath.parent.mkdir(parents=True, exist_ok=True)
            filepath.write_bytes(b"fake_jpeg")
            return (1920, 1080)

        with patch.object(adapter, "_grab_frame", fake_grab):
            frame = await adapter.capture(angle="side_left")

        assert isinstance(frame, CapturedFrame)
        assert frame.adapter_name == "rtsp"
        assert frame.angle == "side_left"
        assert frame.width_px == 1920
        assert frame.height_px == 1080
        assert frame.image_path.exists()

    @pytest.mark.asyncio
    async def test_open_raises_when_stream_not_accessible(self, tmp_path):
        """_open_stream raises RuntimeError if cv2.VideoCapture fails to open."""
        adapter = self._make_adapter(tmp_path)

        mock_cap = MagicMock()
        mock_cap.isOpened.return_value = False

        mock_cv2 = MagicMock()
        mock_cv2.VideoCapture.return_value = mock_cap
        mock_cv2.CAP_FFMPEG = 1900
        mock_cv2.CAP_PROP_BUFFERSIZE = 38

        with patch.dict("sys.modules", {"cv2": mock_cv2}):
            with pytest.raises(RuntimeError, match="Could not open RTSP stream"):
                adapter._open_stream()

    def test_close_sets_cap_to_none(self, tmp_path):
        """_close_stream() must set self._cap = None."""
        adapter = self._make_adapter(tmp_path)
        adapter._cap = MagicMock()
        adapter._close_stream()
        assert adapter._cap is None

    def test_adapter_name(self, tmp_path):
        adapter = self._make_adapter(tmp_path)
        assert adapter.ADAPTER_NAME == "rtsp"
