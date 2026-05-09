"""
tests/adapters/test_imaging.py
==============================
Unit tests for core/imaging.py.

All OpenCV / MediaPipe calls are mocked — no real images or GPU needed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.imaging import (
    SilhouetteExtractionError,
    _largest_component,
    _selfie_segmentation_mask,
    extract_silhouette,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_numpy():
    """Return the real numpy (required for mask arithmetic in tests)."""
    import numpy as np  # noqa: PLC0415
    return np


# ---------------------------------------------------------------------------
# extract_silhouette — async wrapper
# ---------------------------------------------------------------------------

class TestExtractSilhouetteAsync:
    def test_raises_file_not_found_for_missing_image(self, tmp_path):
        missing = tmp_path / "ghost.jpg"
        with pytest.raises(FileNotFoundError, match="ghost.jpg"):
            asyncio.get_event_loop().run_until_complete(
                extract_silhouette(missing)
            )

    def test_delegates_to_sync_in_executor(self, tmp_path):
        """extract_silhouette() must call run_in_executor (not block the loop)."""
        img = tmp_path / "photo.jpg"
        img.write_bytes(b"fake")  # exists on disk

        called_args = []

        async def run():
            with patch("core.imaging._extract_silhouette_sync") as mock_sync:
                mock_sync.return_value = tmp_path / "photo_silhouette.png"
                # Patch run_in_executor to call the function directly
                # (avoids needing a real thread pool in tests)
                import core.imaging as imaging_module  # noqa: PLC0415

                original = asyncio.get_event_loop().run_in_executor

                async def fake_executor(_, fn, *args):
                    called_args.append((fn, args))
                    return fn(*args)

                with patch.object(
                    asyncio.get_event_loop(),
                    "run_in_executor",
                    side_effect=fake_executor,
                ):
                    result = await extract_silhouette(img)

            return result

        result = asyncio.get_event_loop().run_until_complete(run())
        # The sync function was handed off to the executor
        assert len(called_args) == 1
        assert called_args[0][0].__name__ == "_extract_silhouette_sync"

    def test_default_output_dir_is_silhouettes_subdir(self, tmp_path):
        img = tmp_path / "photo.jpg"
        img.write_bytes(b"x")

        expected_sil = tmp_path / "silhouettes" / "photo_silhouette.png"

        async def run():
            with patch("core.imaging._extract_silhouette_sync") as mock_sync:
                mock_sync.return_value = expected_sil

                async def fake_executor(_, fn, *args):
                    return fn(*args)

                with patch.object(
                    asyncio.get_event_loop(), "run_in_executor", side_effect=fake_executor
                ):
                    return await extract_silhouette(img)  # no output_dir given

        result = asyncio.get_event_loop().run_until_complete(run())
        # The sync function was called with output_dir = img.parent/silhouettes
        assert result == expected_sil

    def test_custom_output_dir_passed_through(self, tmp_path):
        img = tmp_path / "photo.jpg"
        img.write_bytes(b"x")
        custom_dir = tmp_path / "custom"

        captured = []

        async def run():
            with patch("core.imaging._extract_silhouette_sync") as mock_sync:
                mock_sync.side_effect = lambda *args: (captured.append(args), args[1])[1]

                async def fake_executor(_, fn, *args):
                    return fn(*args)

                with patch.object(
                    asyncio.get_event_loop(), "run_in_executor", side_effect=fake_executor
                ):
                    await extract_silhouette(img, output_dir=custom_dir)

        asyncio.get_event_loop().run_until_complete(run())
        # Second positional arg to _extract_silhouette_sync is output_dir
        assert captured[0][1] == custom_dir


# ---------------------------------------------------------------------------
# _selfie_segmentation_mask
# ---------------------------------------------------------------------------

class TestSelfieSegmentationMask:
    def test_thresholds_at_0_5(self):
        np = _make_numpy()
        mp = MagicMock()

        fake_mask = np.array([[0.3, 0.7], [0.9, 0.1]], dtype="float32")
        seg_result = MagicMock()
        seg_result.segmentation_mask = fake_mask

        ctx_manager = MagicMock()
        ctx_manager.__enter__ = MagicMock(return_value=MagicMock(process=MagicMock(return_value=seg_result)))
        ctx_manager.__exit__ = MagicMock(return_value=False)
        mp.solutions.selfie_segmentation.SelfieSegmentation.return_value = ctx_manager

        img_rgb = np.zeros((2, 2, 3), dtype="uint8")
        result = _selfie_segmentation_mask(mp, np, img_rgb, model_selection=1)

        expected = np.array([[0, 1], [1, 0]], dtype="uint8")
        assert (result == expected).all()

    def test_all_background_returns_zeros(self):
        np = _make_numpy()
        mp = MagicMock()

        seg_result = MagicMock()
        seg_result.segmentation_mask = np.zeros((4, 4), dtype="float32")

        ctx_manager = MagicMock()
        ctx_manager.__enter__ = MagicMock(return_value=MagicMock(process=MagicMock(return_value=seg_result)))
        ctx_manager.__exit__ = MagicMock(return_value=False)
        mp.solutions.selfie_segmentation.SelfieSegmentation.return_value = ctx_manager

        img_rgb = np.zeros((4, 4, 3), dtype="uint8")
        result = _selfie_segmentation_mask(mp, np, img_rgb)
        assert result.sum() == 0


# ---------------------------------------------------------------------------
# _largest_component
# ---------------------------------------------------------------------------

class TestLargestComponent:
    def test_keeps_largest_blob(self):
        np = _make_numpy()
        try:
            import cv2  # noqa: PLC0415
        except ImportError:
            pytest.skip("cv2 not installed")

        mask = np.zeros((10, 10), dtype="uint8")
        # Large blob: 3x3 in top-left
        mask[0:3, 0:3] = 1
        # Small blob: 1x1 elsewhere
        mask[8, 8] = 1

        result = _largest_component(np, cv2, mask)
        assert result[0, 0] == 1      # large blob kept
        assert result[8, 8] == 0      # small blob removed
        assert result.sum() == 9      # 3x3 = 9 pixels

    def test_empty_mask_returns_unchanged(self):
        np = _make_numpy()
        try:
            import cv2  # noqa: PLC0415
        except ImportError:
            pytest.skip("cv2 not installed")

        mask = np.zeros((5, 5), dtype="uint8")
        result = _largest_component(np, cv2, mask)
        assert result.sum() == 0

    def test_single_blob_returned_intact(self):
        np = _make_numpy()
        try:
            import cv2  # noqa: PLC0415
        except ImportError:
            pytest.skip("cv2 not installed")

        mask = np.zeros((6, 6), dtype="uint8")
        mask[1:5, 1:5] = 1  # one 4x4 block = 16 pixels

        result = _largest_component(np, cv2, mask)
        assert result.sum() == 16


# ---------------------------------------------------------------------------
# Import error
# ---------------------------------------------------------------------------

class TestImportError:
    def test_raises_helpful_import_error_when_cv2_missing(self, tmp_path):
        img = tmp_path / "photo.jpg"
        img.write_bytes(b"x")

        import builtins  # noqa: PLC0415
        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name in ("cv2", "mediapipe"):
                raise ImportError(f"No module named '{name}'")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=mock_import):
            from core.imaging import _extract_silhouette_sync  # noqa: PLC0415
            with pytest.raises(ImportError, match="mediapipe"):
                _extract_silhouette_sync(
                    img, tmp_path / "out", 1, 0.5, 0.05
                )


# ---------------------------------------------------------------------------
# SilhouetteExtractionError
# ---------------------------------------------------------------------------

class TestSilhouetteExtractionError:
    def test_is_runtime_error(self):
        err = SilhouetteExtractionError("test")
        assert isinstance(err, RuntimeError)

    def test_message_preserved(self):
        err = SilhouetteExtractionError("no person found")
        assert "no person found" in str(err)
