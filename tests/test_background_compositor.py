from __future__ import annotations

import pytest
from PIL import Image, ImageDraw

from background_compositor import CompositionError, compose_selected_background, outside_mask_pixels_equal
from image_pipeline import fit_to_platform


def sample_inputs() -> tuple[Image.Image, Image.Image, Image.Image]:
    background = Image.new("RGB", (800, 600), (30, 80, 120))
    draw = ImageDraw.Draw(background)
    draw.rectangle((0, 300, 799, 599), fill=(80, 55, 35))

    foreground = Image.new("RGBA", (320, 240), (230, 70, 40, 255))
    mask = Image.new("L", foreground.size, 0)
    ImageDraw.Draw(mask).ellipse((20, 35, 300, 220), fill=255)
    return background, foreground, mask


def test_compositor_preserves_selected_background_outside_mask():
    background, foreground, mask = sample_inputs()

    result = compose_selected_background(background, foreground, mask)

    assert result.image.size == background.size
    assert result.metadata["backgroundIdentityVerified"] is True
    assert result.metadata["outsideMaskPixelsPreserved"] is True
    assert outside_mask_pixels_equal(result.normalized_background, result.image, result.foreground_mask)
    assert result.foreground_mask.getbbox() is not None


@pytest.mark.parametrize(
    ("mask", "code"),
    [
        (None, "foreground_mask_required"),
        (Image.new("L", (320, 240), 0), "foreground_mask_too_small"),
        (Image.new("L", (320, 240), 255), "foreground_mask_too_large"),
    ],
)
def test_compositor_rejects_untrustworthy_masks(mask, code):
    background, foreground, _valid_mask = sample_inputs()

    with pytest.raises(CompositionError) as exc_info:
        compose_selected_background(background, foreground, mask)

    assert exc_info.value.code == code


def test_compositor_rejects_mask_size_mismatch():
    background, foreground, _mask = sample_inputs()

    with pytest.raises(CompositionError) as exc_info:
        compose_selected_background(background, foreground, Image.new("L", (100, 100), 255))

    assert exc_info.value.code == "foreground_mask_size_mismatch"


def test_platform_fit_uses_full_frame_cover_crop():
    source = Image.new("RGB", (400, 800), (220, 30, 30))

    fitted = fit_to_platform(source, "meituan")

    assert fitted.size == (800, 600)
    assert fitted.getpixel((0, 0))[:3] == (220, 30, 30)
    assert fitted.getpixel((799, 599))[:3] == (220, 30, 30)
