"""
core/imaging.py
===============
Silhouette extraction from body photos using MediaPipe Pose + OpenCV.

Takes a raw photo captured by any CameraAdapter and returns a cleaned
silhouette image (white figure on black background) suitable for:
  - Visual progress comparison
  - AI-assisted body composition estimation
  - Consistent UI display regardless of background

Called by core/scheduler.py after db.save_photo():
    silhouette_path = await extract_silhouette(frame.image_path)
    await db.update_photo_analysis(photo_id, silhouette_path=str(silhouette_path))

Configuration (in config.yaml):
    imaging:
      enabled: true
      output_dir: data/silhouettes   # defaults to same dir as photos
      segmentation_model: 1          # 0 = general, 1 = landscape (faster)
      min_detection_confidence: 0.5
      padding_factor: 0.05           # extra padding around detected pose bbox

Dependencies (optional — install only if imaging is needed):
    pip install mediapipe opencv-python-headless

Both are lazy-imported so the project stays installable without them.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def extract_silhouette(
    image_path: Path,
    output_dir: Optional[Path] = None,
    *,
    segmentation_model: int = 1,
    min_detection_confidence: float = 0.5,
    padding_factor: float = 0.05,
) -> Path:
    """
    Extract a body silhouette from a photo.

    Runs in a thread-pool executor to avoid blocking the async event loop
    (MediaPipe and OpenCV are CPU-bound / synchronous).

    Args:
        image_path: Path to the source image (JPEG or PNG).
        output_dir: Where to write the silhouette file.
                    Defaults to image_path.parent / "silhouettes".
        segmentation_model: MediaPipe model complexity (0 or 1).
        min_detection_confidence: Pose detection threshold (0.0–1.0).
        padding_factor: Fractional padding added around pose bounding box
                        before masking, to avoid clipping extremities.

    Returns:
        Path to the generated silhouette image.

    Raises:
        ImportError: If mediapipe or cv2 are not installed.
        FileNotFoundError: If image_path does not exist.
        SilhouetteExtractionError: If no person is detected in the image.
    """
    if not image_path.exists():
        raise FileNotFoundError(f"Image not found: {image_path}")

    if output_dir is None:
        output_dir = image_path.parent / "silhouettes"

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None,
        _extract_silhouette_sync,
        image_path,
        output_dir,
        segmentation_model,
        min_detection_confidence,
        padding_factor,
    )
    return result


# ---------------------------------------------------------------------------
# Synchronous implementation (runs in thread pool)
# ---------------------------------------------------------------------------

def _extract_silhouette_sync(
    image_path: Path,
    output_dir: Path,
    segmentation_model: int,
    min_detection_confidence: float,
    padding_factor: float,
) -> Path:
    """
    CPU-bound silhouette extraction — called via run_in_executor.

    Strategy:
      1. Try MediaPipe Selfie Segmentation (fast, no skeleton required).
         Best when the full body is visible.
      2. Fall back to MediaPipe Pose segmentation mask if selfie segmentation
         produces a low-confidence result (edge case: unusual camera angle).
      3. If neither produces a usable mask, raise SilhouetteExtractionError.
    """
    # Lazy imports — not required for non-imaging installs
    try:
        import cv2  # noqa: PLC0415
        import mediapipe as mp  # noqa: PLC0415
        import numpy as np  # noqa: PLC0415
    except ImportError as exc:
        raise ImportError(
            "Silhouette extraction requires mediapipe and opencv-python-headless.\n"
            "Install them with: pip install mediapipe opencv-python-headless"
        ) from exc

    # ------------------------------------------------------------------
    # Load image
    # ------------------------------------------------------------------
    img_bgr = cv2.imread(str(image_path))
    if img_bgr is None:
        raise FileNotFoundError(f"OpenCV could not read image: {image_path}")

    img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
    h, w = img_bgr.shape[:2]
    logger.debug("Loaded image %s (%dx%d)", image_path.name, w, h)

    # ------------------------------------------------------------------
    # Step 1: MediaPipe Selfie Segmentation
    # ------------------------------------------------------------------
    mask = _selfie_segmentation_mask(mp, np, img_rgb, model_selection=segmentation_model)

    # Quality check: the person should occupy at least 5% of the frame
    coverage = float(mask.sum()) / mask.size
    logger.debug("Segmentation coverage: %.1f%%", coverage * 100)

    if coverage < 0.05:
        logger.debug("Low segmentation coverage — falling back to Pose mask")
        mask = _pose_segmentation_mask(
            mp, np, img_rgb,
            min_detection_confidence=min_detection_confidence,
            padding_factor=padding_factor,
            image_h=h,
            image_w=w,
        )
        coverage = float(mask.sum()) / mask.size
        if coverage < 0.02:
            raise SilhouetteExtractionError(
                f"No person detected in image: {image_path.name}. "
                "Ensure the full body is visible and well-lit."
            )

    # ------------------------------------------------------------------
    # Step 2: Morphological cleanup
    # ------------------------------------------------------------------
    kernel = np.ones((5, 5), np.uint8)
    mask_clean = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
    mask_clean = cv2.morphologyEx(mask_clean, cv2.MORPH_OPEN, kernel, iterations=1)

    # Keep only the largest connected component (ignore small noise blobs)
    mask_clean = _largest_component(np, cv2, mask_clean)

    # ------------------------------------------------------------------
    # Step 3: Render white silhouette on black background
    # ------------------------------------------------------------------
    silhouette = np.zeros_like(img_bgr)
    silhouette[mask_clean == 1] = [255, 255, 255]

    # ------------------------------------------------------------------
    # Step 4: Save
    # ------------------------------------------------------------------
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{image_path.stem}_silhouette.png"
    cv2.imwrite(str(out_path), silhouette)

    logger.info(
        "Silhouette saved: %s (coverage %.1f%%)",
        out_path.name,
        float(mask_clean.sum()) / mask_clean.size * 100,
    )
    return out_path


# ---------------------------------------------------------------------------
# Mask extraction helpers
# ---------------------------------------------------------------------------

def _selfie_segmentation_mask(mp, np, img_rgb, *, model_selection: int = 1):
    """
    Use MediaPipe SelfieSegmentation to produce a binary person mask.

    model_selection:
      0 = general model  (slower, more accurate for close-up shots)
      1 = landscape model (faster, better for full-body from distance)

    Returns a uint8 numpy array (0 = background, 1 = person).
    """
    with mp.solutions.selfie_segmentation.SelfieSegmentation(
        model_selection=model_selection
    ) as seg:
        result = seg.process(img_rgb)

    # result.segmentation_mask is a float32 array in [0, 1]
    mask = (result.segmentation_mask > 0.5).astype(np.uint8)
    return mask


def _pose_segmentation_mask(
    mp, np, img_rgb, *,
    min_detection_confidence: float,
    padding_factor: float,
    image_h: int,
    image_w: int,
):
    """
    Fallback: use MediaPipe Pose landmarks to derive a bounding-box mask.

    This is less precise than selfie segmentation but works for full-body
    shots where the selfie model under-segments.

    Returns a uint8 numpy array (0 = background, 1 = person bbox region).
    """
    with mp.solutions.pose.Pose(
        static_image_mode=True,
        model_complexity=1,
        min_detection_confidence=min_detection_confidence,
        enable_segmentation=True,
    ) as pose:
        result = pose.process(img_rgb)

    # Prefer the built-in segmentation mask if available
    if result.segmentation_mask is not None:
        return (result.segmentation_mask > 0.5).astype(np.uint8)

    # Last resort: bounding box from landmarks
    if not result.pose_landmarks:
        return np.zeros((image_h, image_w), dtype=np.uint8)

    xs = [lm.x for lm in result.pose_landmarks.landmark]
    ys = [lm.y for lm in result.pose_landmarks.landmark]

    pad_x = padding_factor * image_w
    pad_y = padding_factor * image_h

    x1 = max(0, int(min(xs) * image_w - pad_x))
    y1 = max(0, int(min(ys) * image_h - pad_y))
    x2 = min(image_w, int(max(xs) * image_w + pad_x))
    y2 = min(image_h, int(max(ys) * image_h + pad_y))

    mask = np.zeros((image_h, image_w), dtype=np.uint8)
    mask[y1:y2, x1:x2] = 1
    return mask


# ---------------------------------------------------------------------------
# Morphology helpers
# ---------------------------------------------------------------------------

def _largest_component(np, cv2, mask: "np.ndarray") -> "np.ndarray":
    """
    Keep only the largest connected component in a binary mask.
    Eliminates small floating blobs caused by specular highlights or
    partial body parts outside the frame.
    """
    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(
        mask, connectivity=8
    )
    if num_labels <= 1:
        return mask  # no foreground at all

    # stats[0] is background — skip it, find largest foreground component
    areas = stats[1:, cv2.CC_STAT_AREA]
    largest_label = int(areas.argmax()) + 1  # +1 because we skipped background

    clean = np.zeros_like(mask)
    clean[labels == largest_label] = 1
    return clean


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class SilhouetteExtractionError(RuntimeError):
    """Raised when no person is detected in the source image."""
