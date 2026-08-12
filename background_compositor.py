from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from PIL import Image, ImageChops, ImageFilter, ImageOps

from prompt_compiler import PlacementSpec


DEFAULT_MIN_MASK_RATIO = 0.01
DEFAULT_MAX_MASK_RATIO = 0.92


class CompositionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CompositionResult:
    image: Image.Image
    foreground_mask: Image.Image
    normalized_background: Image.Image
    metadata: dict[str, Any]


def normalize_background(background: Image.Image, target_size: tuple[int, int]) -> Image.Image:
    width, height = (int(target_size[0]), int(target_size[1]))
    if width <= 0 or height <= 0:
        raise CompositionError("invalid_target_size", "target size must be positive")
    if background.width <= 0 or background.height <= 0:
        raise CompositionError("invalid_background", "background image is empty")
    source = ImageOps.exif_transpose(background).convert("RGBA")
    return ImageOps.fit(source, (width, height), Image.Resampling.LANCZOS, centering=(0.5, 0.5))


def compose_selected_background(
    background: Image.Image,
    foreground: Image.Image,
    mask: Image.Image | None,
    *,
    target_size: tuple[int, int] | None = None,
    max_subject_width_ratio: float = 0.82,
    max_subject_height_ratio: float = 0.76,
    bottom_margin_ratio: float = 0.06,
    min_mask_ratio: float = DEFAULT_MIN_MASK_RATIO,
    max_mask_ratio: float = DEFAULT_MAX_MASK_RATIO,
    placement: PlacementSpec | None = None,
) -> CompositionResult:
    if mask is None:
        raise CompositionError("foreground_mask_required", "a foreground mask is required")

    source_foreground = ImageOps.exif_transpose(foreground).convert("RGBA")
    source_mask = ImageOps.exif_transpose(mask).convert("L")
    if source_foreground.size != source_mask.size:
        raise CompositionError("foreground_mask_size_mismatch", "foreground and mask sizes must match")

    alpha = source_foreground.getchannel("A")
    source_mask = ImageChops.multiply(source_mask, alpha)
    mask_pixels = source_mask.width * source_mask.height
    nonzero_pixels = sum(count for value, count in enumerate(source_mask.histogram()) if value > 0)
    mask_ratio = nonzero_pixels / max(1, mask_pixels)
    if mask_ratio < float(min_mask_ratio):
        raise CompositionError("foreground_mask_too_small", "foreground mask is empty or too small")
    if mask_ratio > float(max_mask_ratio):
        raise CompositionError("foreground_mask_too_large", "foreground mask covers too much of the source")

    bounds = source_mask.getbbox()
    if bounds is None:
        raise CompositionError("foreground_mask_empty", "foreground mask is empty")

    crop_foreground = source_foreground.crop(bounds)
    crop_mask = source_mask.crop(bounds)
    canvas_size = target_size or background.size
    normalized_background = normalize_background(background, canvas_size)
    canvas_width, canvas_height = normalized_background.size

    resolved_width_ratio = (
        placement.max_subject_width_ratio
        if placement is not None
        else float(max_subject_width_ratio)
    )
    resolved_height_ratio = (
        placement.max_subject_height_ratio
        if placement is not None
        else float(max_subject_height_ratio)
    )
    max_width = max(1, round(canvas_width * resolved_width_ratio))
    max_height = max(1, round(canvas_height * resolved_height_ratio))
    scale = min(max_width / crop_foreground.width, max_height / crop_foreground.height)
    subject_size = (
        max(1, round(crop_foreground.width * scale)),
        max(1, round(crop_foreground.height * scale)),
    )
    resized_foreground = crop_foreground.resize(subject_size, Image.Resampling.LANCZOS)
    resized_mask = crop_mask.resize(subject_size, Image.Resampling.LANCZOS)

    if placement is None:
        x = (canvas_width - subject_size[0]) // 2
        bottom_margin = round(canvas_height * float(bottom_margin_ratio))
        y = max(0, canvas_height - bottom_margin - subject_size[1])
    else:
        x = round((canvas_width * placement.center_x) - (subject_size[0] / 2))
        y = round((canvas_height * placement.center_y) - (subject_size[1] / 2))
        x = min(max(0, x), max(0, canvas_width - subject_size[0]))
        y = min(max(0, y), max(0, canvas_height - subject_size[1]))
    foreground_canvas = Image.new("RGBA", normalized_background.size, (0, 0, 0, 0))
    mask_canvas = Image.new("L", normalized_background.size, 0)
    mask_canvas.paste(resized_mask, (x, y))
    masked_foreground = resized_foreground.copy()
    masked_foreground.putalpha(resized_mask)
    foreground_canvas.alpha_composite(masked_foreground, (x, y))

    modification_mask = mask_canvas
    shadow_metadata: dict[str, Any] = {"enabled": False}
    output = normalized_background.copy().convert("RGBA")
    if placement is not None and placement.shadow_opacity > 0:
        blur_radius = max(
            1,
            round(min(canvas_width, canvas_height) * placement.shadow_blur_ratio),
        )
        offset_x = round(canvas_width * placement.shadow_offset_x_ratio)
        offset_y = round(canvas_height * placement.shadow_offset_y_ratio)
        blurred = mask_canvas.filter(ImageFilter.GaussianBlur(radius=blur_radius))
        shifted = Image.new("L", normalized_background.size, 0)
        shifted.paste(blurred, (offset_x, offset_y))
        shadow_alpha = shifted.point(
            lambda value: round(value * min(1.0, max(0.0, placement.shadow_opacity)))
        )
        shadow_layer = Image.new("RGBA", normalized_background.size, (0, 0, 0, 0))
        shadow_layer.putalpha(shadow_alpha)
        output.alpha_composite(shadow_layer)
        modification_mask = ImageChops.lighter(mask_canvas, shadow_alpha)
        shadow_metadata = {
            "enabled": True,
            "offsetX": offset_x,
            "offsetY": offset_y,
            "blurRadius": blur_radius,
            "opacity": placement.shadow_opacity,
        }
    output.alpha_composite(foreground_canvas)

    outside_preserved = outside_mask_pixels_equal(
        normalized_background,
        output,
        modification_mask,
    )
    if not outside_preserved:
        raise CompositionError("background_identity_mismatch", "pixels outside the foreground mask changed")

    return CompositionResult(
        image=output,
        foreground_mask=modification_mask,
        normalized_background=normalized_background,
        metadata={
            "backgroundIdentityVerified": True,
            "outsideMaskPixelsPreserved": True,
            "sourceMaskRatio": round(mask_ratio, 6),
            "subjectBounds": {
                "x": x,
                "y": y,
                "width": subject_size[0],
                "height": subject_size[1],
            },
            "subjectCenter": {
                "x": round((x + (subject_size[0] / 2)) / canvas_width, 6),
                "y": round((y + (subject_size[1] / 2)) / canvas_height, 6),
            },
            "placementContract": placement.payload() if placement is not None else None,
            "contactShadow": shadow_metadata,
            "canvas": {"width": canvas_width, "height": canvas_height},
        },
    )


def outside_mask_pixels_equal(background: Image.Image, output: Image.Image, mask: Image.Image) -> bool:
    if background.size != output.size or output.size != mask.size:
        return False
    difference = ImageChops.difference(background.convert("RGB"), output.convert("RGB"))
    outside = mask.convert("L").point(lambda value: 255 if value == 0 else 0)
    outside_rgb = Image.merge("RGB", (outside, outside, outside))
    return ImageChops.multiply(difference, outside_rgb).getbbox() is None
