from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any

from PIL import Image, ImageChops, ImageFilter, ImageOps

from background_compositor import (
    CompositionError,
    normalize_background,
    outside_mask_pixels_equal,
)


DEFAULT_DIFFERENCE_THRESHOLD = 4
DEFAULT_MASK_DILATION = 7
DEFAULT_MIN_REFINEMENT_MASK_RATIO = 0.01
DEFAULT_MAX_REFINEMENT_MASK_RATIO = 0.82


@dataclass(frozen=True)
class RefinementComposition:
    image: Image.Image
    mask: Image.Image
    normalized_background: Image.Image
    metadata: dict[str, Any]

    def png_bytes(self) -> bytes:
        output = io.BytesIO()
        self.image.save(output, "PNG", optimize=True)
        return output.getvalue()


def derive_locked_foreground_mask(
    source: Image.Image,
    selected_background: Image.Image,
    *,
    difference_threshold: int = DEFAULT_DIFFERENCE_THRESHOLD,
    dilation_size: int = DEFAULT_MASK_DILATION,
    min_mask_ratio: float = DEFAULT_MIN_REFINEMENT_MASK_RATIO,
    max_mask_ratio: float = DEFAULT_MAX_REFINEMENT_MASK_RATIO,
) -> tuple[Image.Image, Image.Image, dict[str, Any]]:
    clean_source = ImageOps.exif_transpose(source).convert("RGBA")
    background = normalize_background(selected_background, clean_source.size)
    difference = ImageChops.difference(
        clean_source.convert("RGB"),
        background.convert("RGB"),
    )
    red, green, blue = difference.split()
    maximum = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    threshold = max(0, min(255, int(difference_threshold)))
    mask = maximum.point(lambda value: 255 if value > threshold else 0)
    dilation = int(dilation_size)
    if dilation < 1 or dilation % 2 == 0:
        raise CompositionError(
            "invalid_refinement_mask_dilation",
            "refinement mask dilation must be a positive odd number",
        )
    if dilation > 1:
        mask = mask.filter(ImageFilter.MaxFilter(dilation))

    nonzero = sum(
        count for value, count in enumerate(mask.histogram()) if value > 0
    )
    ratio = nonzero / max(1, mask.width * mask.height)
    if ratio < float(min_mask_ratio):
        raise CompositionError(
            "refinement_foreground_too_small",
            "source image has no usable foreground for locked refinement",
        )
    if ratio > float(max_mask_ratio):
        raise CompositionError(
            "refinement_foreground_too_large",
            "source image does not preserve enough selected background",
        )
    bounds = mask.getbbox()
    if bounds is None:
        raise CompositionError(
            "refinement_foreground_empty",
            "source image has no usable foreground for locked refinement",
        )
    return (
        mask,
        background,
        {
            "sourceMaskRatio": round(ratio, 6),
            "sourceMaskBounds": {
                "x": bounds[0],
                "y": bounds[1],
                "width": bounds[2] - bounds[0],
                "height": bounds[3] - bounds[1],
            },
        },
    )


def compose_locked_refinement(
    source: Image.Image,
    selected_background: Image.Image,
    edited: Image.Image,
    **mask_options: Any,
) -> RefinementComposition:
    clean_source = ImageOps.exif_transpose(source).convert("RGBA")
    clean_edited = ImageOps.exif_transpose(edited).convert("RGBA")
    if clean_edited.size != clean_source.size:
        raise CompositionError(
            "refinement_output_size_mismatch",
            "refinement output must match the source image size",
        )
    mask, background, metadata = derive_locked_foreground_mask(
        clean_source,
        selected_background,
        **mask_options,
    )
    output = Image.composite(clean_edited, background, mask)
    if not outside_mask_pixels_equal(background, output, mask):
        raise CompositionError(
            "refinement_background_identity_mismatch",
            "refinement changed pixels outside the locked foreground",
        )
    return RefinementComposition(
        image=output,
        mask=mask,
        normalized_background=background,
        metadata={
            **metadata,
            "backgroundIdentityVerified": True,
            "outsideMaskPixelsPreserved": True,
            "canonicalFormat": "PNG",
            "canvas": {
                "width": output.width,
                "height": output.height,
            },
        },
    )


__all__ = [
    "RefinementComposition",
    "compose_locked_refinement",
    "derive_locked_foreground_mask",
]
