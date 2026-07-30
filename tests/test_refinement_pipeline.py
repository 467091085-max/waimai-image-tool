from __future__ import annotations

from PIL import Image, ImageDraw
import pytest

from background_compositor import CompositionError, outside_mask_pixels_equal
from refinement_pipeline import compose_locked_refinement


def scene() -> tuple[Image.Image, Image.Image, Image.Image]:
    background = Image.new("RGB", (160, 120), color=(228, 218, 196))
    source = background.copy()
    edited = background.copy()
    ImageDraw.Draw(source).ellipse(
        (42, 30, 118, 104),
        fill=(190, 45, 35),
    )
    ImageDraw.Draw(edited).ellipse(
        (42, 30, 118, 104),
        fill=(52, 145, 72),
    )
    return background, source, edited


def test_locked_refinement_preserves_every_outside_mask_pixel() -> None:
    background, source, edited = scene()

    result = compose_locked_refinement(source, background, edited)

    assert result.image.getpixel((80, 65))[:3] == (52, 145, 72)
    assert outside_mask_pixels_equal(
        result.normalized_background,
        result.image,
        result.mask,
    )
    assert result.metadata["backgroundIdentityVerified"] is True
    assert result.metadata["outsideMaskPixelsPreserved"] is True
    assert result.metadata["canonicalFormat"] == "PNG"
    assert result.png_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_locked_refinement_rejects_nearly_full_frame_change() -> None:
    background = Image.new("RGB", (100, 80), "white")
    source = Image.new("RGB", (100, 80), "red")
    edited = Image.new("RGB", (100, 80), "green")

    with pytest.raises(CompositionError) as raised:
        compose_locked_refinement(source, background, edited)

    assert raised.value.code == "refinement_foreground_too_large"


def test_locked_refinement_rejects_empty_or_tiny_foreground() -> None:
    background = Image.new("RGB", (160, 120), "white")
    source = background.copy()
    edited = background.copy()
    source.putpixel((80, 60), (0, 0, 0))

    with pytest.raises(CompositionError) as raised:
        compose_locked_refinement(source, background, edited)

    assert raised.value.code == "refinement_foreground_too_small"


def test_locked_refinement_rejects_provider_size_drift() -> None:
    background, source, _edited = scene()

    with pytest.raises(CompositionError) as raised:
        compose_locked_refinement(
            source,
            background,
            Image.new("RGB", (120, 120), "green"),
        )

    assert raised.value.code == "refinement_output_size_mismatch"
