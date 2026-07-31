from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any

import numpy as np
from PIL import Image, ImageFilter, ImageOps


EXTRACTION_VERSION = 2
EXPECTED_CHROMA_RGB = (0, 255, 255)


class ChromaExtractionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ChromaExtractionResult:
    mask: Image.Image
    metadata: dict[str, Any]


def _border_pixels(array: np.ndarray, width: int) -> np.ndarray:
    return np.concatenate(
        (
            array[:width, :, :].reshape(-1, 3),
            array[-width:, :, :].reshape(-1, 3),
            array[width:-width, :width, :].reshape(-1, 3),
            array[width:-width, -width:, :].reshape(-1, 3),
        ),
        axis=0,
    )


def _edge_reachable(candidate: np.ndarray) -> np.ndarray:
    height, width = candidate.shape
    reachable = np.zeros((height, width), dtype=np.bool_)
    queue: deque[tuple[int, int]] = deque()

    def seed(y: int, x: int) -> None:
        if candidate[y, x] and not reachable[y, x]:
            reachable[y, x] = True
            queue.append((y, x))

    for x in range(width):
        seed(0, x)
        seed(height - 1, x)
    for y in range(1, height - 1):
        seed(y, 0)
        seed(y, width - 1)

    while queue:
        y, x = queue.popleft()
        if y > 0 and candidate[y - 1, x] and not reachable[y - 1, x]:
            reachable[y - 1, x] = True
            queue.append((y - 1, x))
        if y + 1 < height and candidate[y + 1, x] and not reachable[y + 1, x]:
            reachable[y + 1, x] = True
            queue.append((y + 1, x))
        if x > 0 and candidate[y, x - 1] and not reachable[y, x - 1]:
            reachable[y, x - 1] = True
            queue.append((y, x - 1))
        if x + 1 < width and candidate[y, x + 1] and not reachable[y, x + 1]:
            reachable[y, x + 1] = True
            queue.append((y, x + 1))
    return reachable


def _connected_to_seed(candidate: np.ndarray, seeds: np.ndarray) -> np.ndarray:
    height, width = candidate.shape
    connected = np.zeros((height, width), dtype=np.bool_)
    queue: deque[tuple[int, int]] = deque()
    for y, x in np.argwhere(np.logical_and(candidate, seeds)):
        connected[y, x] = True
        queue.append((int(y), int(x)))

    while queue:
        y, x = queue.popleft()
        if y > 0 and candidate[y - 1, x] and not connected[y - 1, x]:
            connected[y - 1, x] = True
            queue.append((y - 1, x))
        if y + 1 < height and candidate[y + 1, x] and not connected[y + 1, x]:
            connected[y + 1, x] = True
            queue.append((y + 1, x))
        if x > 0 and candidate[y, x - 1] and not connected[y, x - 1]:
            connected[y, x - 1] = True
            queue.append((y, x - 1))
        if x + 1 < width and candidate[y, x + 1] and not connected[y, x + 1]:
            connected[y, x + 1] = True
            queue.append((y, x + 1))
    return connected


def _foreground_boundary(foreground: np.ndarray) -> np.ndarray:
    outside = np.logical_not(foreground)
    adjacent_outside = np.zeros_like(foreground)
    adjacent_outside[1:, :] |= outside[:-1, :]
    adjacent_outside[:-1, :] |= outside[1:, :]
    adjacent_outside[:, 1:] |= outside[:, :-1]
    adjacent_outside[:, :-1] |= outside[:, 1:]
    return np.logical_and(foreground, adjacent_outside)


def extract_chroma_mask(
    image: Image.Image,
    *,
    min_foreground_ratio: float = 0.03,
    max_foreground_ratio: float = 0.86,
) -> ChromaExtractionResult:
    rgb = ImageOps.exif_transpose(image).convert("RGB")
    if rgb.width < 128 or rgb.height < 128:
        raise ChromaExtractionError(
            "chroma_image_too_small",
            "foreground image is too small for chroma extraction",
        )

    array = np.asarray(rgb, dtype=np.int16)
    border_width = max(2, round(min(rgb.size) * 0.015))
    border = _border_pixels(array, border_width)
    background_color = np.median(border, axis=0)
    red, green, blue = (float(value) for value in background_color)
    if green < 150 or blue < 150 or ((green + blue) / 2.0 - red) < 45:
        raise ChromaExtractionError(
            "chroma_color_not_detected",
            "provider did not return the requested cyan chroma background",
        )

    border_distance = np.linalg.norm(
        border.astype(np.float32) - background_color.astype(np.float32),
        axis=1,
    )
    border_p95 = float(np.percentile(border_distance, 95))
    border_p99 = float(np.percentile(border_distance, 99))
    if border_p95 > 48:
        raise ChromaExtractionError(
            "chroma_border_not_uniform",
            "chroma background border is not uniform enough",
        )

    threshold = float(max(24, min(72, border_p99 + 18)))
    distance = np.linalg.norm(
        array.astype(np.float32) - background_color.astype(np.float32),
        axis=2,
    )
    candidate_background = distance <= threshold
    border_candidate_ratio = float(
        np.count_nonzero(
            np.concatenate(
                (
                    candidate_background[:border_width, :].ravel(),
                    candidate_background[-border_width:, :].ravel(),
                    candidate_background[border_width:-border_width, :border_width].ravel(),
                    candidate_background[border_width:-border_width, -border_width:].ravel(),
                )
            )
        )
    ) / max(1, border.shape[0])
    if border_candidate_ratio < 0.985:
        raise ChromaExtractionError(
            "chroma_border_subject_contact",
            "foreground or non-uniform pixels touch the image border",
        )

    reachable_background = _edge_reachable(candidate_background)
    foreground = np.logical_not(reachable_background)
    if np.any(foreground[0, :]) or np.any(foreground[-1, :]) or np.any(foreground[:, 0]) or np.any(foreground[:, -1]):
        raise ChromaExtractionError(
            "chroma_subject_touches_border",
            "foreground touches the image border",
        )

    foreground_ratio = float(np.count_nonzero(foreground)) / foreground.size
    if foreground_ratio < float(min_foreground_ratio):
        raise ChromaExtractionError(
            "chroma_foreground_too_small",
            "extracted foreground is too small",
        )
    if foreground_ratio > float(max_foreground_ratio):
        raise ChromaExtractionError(
            "chroma_foreground_too_large",
            "extracted foreground covers too much of the image",
        )

    float_array = array.astype(np.float32)
    pixel_sums = np.maximum(float_array.sum(axis=2, keepdims=True), 1.0)
    chromaticity = float_array / pixel_sums
    background_chromaticity = background_color.astype(np.float32)
    background_chromaticity /= max(float(background_chromaticity.sum()), 1.0)
    chroma_distance = np.linalg.norm(
        chromaticity - background_chromaticity.reshape(1, 1, 3),
        axis=2,
    )
    red_channel = float_array[:, :, 0]
    green_channel = float_array[:, :, 1]
    blue_channel = float_array[:, :, 2]
    cyan_axis = np.logical_and.reduce(
        (
            np.minimum(green_channel, blue_channel) - red_channel
            >= np.maximum(25.0, np.maximum(green_channel, blue_channel) * 0.18),
            np.abs(green_channel - blue_channel)
            <= np.maximum(24.0, np.maximum(green_channel, blue_channel) * 0.18),
            np.maximum(green_channel, blue_channel) >= 55.0,
        )
    )
    residual_chroma = np.logical_and.reduce(
        (
            foreground,
            np.logical_not(candidate_background),
            np.logical_or(chroma_distance <= 0.055, cyan_axis),
            float_array.sum(axis=2) >= 90.0,
        )
    )
    boundary_residual = _connected_to_seed(
        residual_chroma,
        _foreground_boundary(foreground),
    )
    residual_ratio = float(np.count_nonzero(boundary_residual)) / foreground.size
    residual_foreground_ratio = float(np.count_nonzero(boundary_residual)) / max(
        1,
        np.count_nonzero(foreground),
    )
    if residual_ratio > 0.012 and residual_foreground_ratio > 0.04:
        raise ChromaExtractionError(
            "chroma_spill_too_large",
            "extracted foreground still contains a large chroma-colored edge region",
        )

    ys, xs = np.nonzero(foreground)
    left = int(xs.min())
    top = int(ys.min())
    right = int(xs.max()) + 1
    bottom = int(ys.max()) + 1
    min_margin = max(2, round(min(rgb.size) * 0.01))
    if (
        left < min_margin
        or top < min_margin
        or rgb.width - right < min_margin
        or rgb.height - bottom < min_margin
    ):
        raise ChromaExtractionError(
            "chroma_foreground_margin_too_small",
            "foreground does not have a safe chroma margin",
        )

    binary = Image.fromarray(
        np.where(foreground, 255, 0).astype(np.uint8),
        mode="L",
    )
    eroded = binary.filter(ImageFilter.MinFilter(3))
    softened = eroded.filter(ImageFilter.GaussianBlur(radius=0.8))
    if softened.getbbox() is None:
        raise ChromaExtractionError(
            "chroma_foreground_empty_after_cleanup",
            "foreground disappeared during edge cleanup",
        )

    return ChromaExtractionResult(
        mask=softened,
        metadata={
            "provider": "local-chroma-key",
            "action": "LocalChromaKeyMask",
            "extractionVersion": EXTRACTION_VERSION,
            "backgroundColor": [
                int(round(red)),
                int(round(green)),
                int(round(blue)),
            ],
            "borderP95Distance": round(border_p95, 3),
            "distanceThreshold": round(threshold, 3),
            "borderCandidateRatio": round(border_candidate_ratio, 6),
            "foregroundRatio": round(foreground_ratio, 6),
            "residualChromaRatio": round(residual_ratio, 6),
            "residualChromaForegroundRatio": round(residual_foreground_ratio, 6),
            "foregroundBounds": {
                "x": left,
                "y": top,
                "width": right - left,
                "height": bottom - top,
            },
        },
    )
