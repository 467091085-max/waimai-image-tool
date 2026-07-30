from __future__ import annotations

import io
import zipfile

import pytest
from PIL import Image

import app as app_module


def image_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (20, 12), color=(80, 140, 210)).save(
        output,
        format="PNG",
    )
    return output.getvalue()


def archive_with(name: str, payload: bytes) -> io.BytesIO:
    output = io.BytesIO()
    with zipfile.ZipFile(
        output,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        archive.writestr(name, payload)
    output.seek(0)
    return output


class Upload:
    def __init__(self, stream: io.BytesIO, filename: str = "library.zip"):
        self.stream = stream
        self.filename = filename


def test_library_zip_images_are_decoded_and_normalized() -> None:
    images = app_module.read_library_import_zip(
        Upload(archive_with("背景/炒饭.png", image_bytes()))
    )

    assert len(images) == 1
    assert images[0].member_name == "背景/炒饭.png"
    assert images[0].standard_name == "炒饭"
    assert images[0].suffix == ".jpg"
    assert images[0].width == 20
    assert images[0].height == 12
    assert images[0].size_bytes == len(images[0].data)


@pytest.mark.parametrize(
    ("name", "payload", "code"),
    (
        ("../escape.png", image_bytes(), "invalid_library_zip_member"),
        ("safe.png", b"not-an-image", "invalid_library_image"),
        ("notes.txt", b"plain text", "library_zip_has_no_images"),
    ),
)
def test_library_zip_rejects_unsafe_or_invalid_content(
    name: str,
    payload: bytes,
    code: str,
) -> None:
    with pytest.raises(
        app_module.LibraryImportRequestError
    ) as exc_info:
        app_module.read_library_import_zip(
            Upload(archive_with(name, payload))
        )

    assert exc_info.value.code == code


def test_library_zip_rejects_abnormal_compression_ratio() -> None:
    with pytest.raises(
        app_module.LibraryImportRequestError
    ) as exc_info:
        app_module.read_library_import_zip(
            Upload(archive_with("notes.txt", b"0" * 200_000))
        )

    assert exc_info.value.code == "library_zip_compression_ratio"
