from __future__ import annotations

import numpy as np
import pytest
from PIL import Image, ImageDraw

from chroma_foreground import ChromaExtractionError, extract_chroma_mask


CYAN = (0, 245, 245)


def chroma_subject(size: tuple[int, int] = (320, 240)) -> Image.Image:
    image = Image.new("RGB", size, CYAN)
    draw = ImageDraw.Draw(image)
    draw.ellipse((55, 35, 265, 220), fill=(215, 72, 38))
    draw.ellipse((115, 95, 205, 170), fill=CYAN)
    return image


def test_extracts_edge_connected_chroma_without_removing_interior_color() -> None:
    result = extract_chroma_mask(chroma_subject())

    assert result.metadata["action"] == "LocalChromaKeyMask"
    assert 0.2 < result.metadata["foregroundRatio"] < 0.7
    assert result.mask.getpixel((0, 0)) == 0
    assert result.mask.getpixel((160, 120)) > 240


def test_accepts_small_realistic_chroma_variation() -> None:
    array = np.zeros((240, 320, 3), dtype=np.uint8)
    array[:, :, 1] = np.linspace(238, 250, 320, dtype=np.uint8)
    array[:, :, 2] = np.linspace(240, 252, 320, dtype=np.uint8)
    image = Image.fromarray(array, mode="RGB")
    ImageDraw.Draw(image).rectangle((80, 50, 240, 215), fill=(220, 90, 45))

    result = extract_chroma_mask(image)

    assert result.metadata["borderP95Distance"] < 48
    assert result.mask.getbbox() is not None


def test_rejects_large_dark_chroma_shadow_connected_to_subject_edge() -> None:
    image = Image.new("RGB", (320, 240), CYAN)
    draw = ImageDraw.Draw(image)
    draw.ellipse((55, 145, 265, 225), fill=(55, 155, 158))
    draw.ellipse((70, 35, 250, 205), fill=(215, 72, 38))

    with pytest.raises(ChromaExtractionError) as exc_info:
        extract_chroma_mask(image)

    assert exc_info.value.code == "chroma_spill_too_large"


def test_rejects_medium_chroma_halo_below_one_percent_of_image() -> None:
    image = Image.new("RGB", (320, 240), CYAN)
    draw = ImageDraw.Draw(image)
    draw.ellipse((66, 55, 254, 220), fill=(215, 72, 38))
    draw.ellipse((58, 185, 262, 228), fill=(55, 155, 158))
    draw.ellipse((66, 55, 254, 215), fill=(215, 72, 38))

    with pytest.raises(ChromaExtractionError) as exc_info:
        extract_chroma_mask(image)

    assert exc_info.value.code == "chroma_spill_too_large"


def test_accepts_small_interior_dark_chroma_detail() -> None:
    image = chroma_subject()
    ImageDraw.Draw(image).ellipse((145, 105, 175, 135), fill=(55, 155, 158))

    result = extract_chroma_mask(image)

    assert result.metadata["residualChromaRatio"] == 0


@pytest.mark.parametrize(
    ("image", "code"),
    [
        (Image.new("RGB", (320, 240), (245, 245, 245)), "chroma_color_not_detected"),
        (Image.new("RGB", (64, 64), CYAN), "chroma_image_too_small"),
    ],
)
def test_rejects_missing_or_undersized_chroma(image: Image.Image, code: str) -> None:
    with pytest.raises(ChromaExtractionError) as exc_info:
        extract_chroma_mask(image)

    assert exc_info.value.code == code


def test_rejects_subject_touching_border() -> None:
    image = Image.new("RGB", (320, 240), CYAN)
    ImageDraw.Draw(image).rectangle((0, 40, 220, 210), fill=(210, 60, 35))

    with pytest.raises(ChromaExtractionError) as exc_info:
        extract_chroma_mask(image)

    assert exc_info.value.code in {
        "chroma_border_not_uniform",
        "chroma_border_subject_contact",
        "chroma_subject_touches_border",
    }


def test_rejects_nonuniform_border() -> None:
    image = chroma_subject()
    draw = ImageDraw.Draw(image)
    for x in range(image.width):
        draw.point((x, 0), fill=(x % 256, 245, 245))

    with pytest.raises(ChromaExtractionError) as exc_info:
        extract_chroma_mask(image)

    assert exc_info.value.code in {
        "chroma_border_not_uniform",
        "chroma_border_subject_contact",
    }
