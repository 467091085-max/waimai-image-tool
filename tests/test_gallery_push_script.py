from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stderr
from unittest import mock

from scripts import push_gallery_via_app


class GalleryPushScriptTest(unittest.TestCase):
    def test_missing_local_token_fails_before_status_probe(self) -> None:
        stderr = io.StringIO()
        with (
            mock.patch.object(sys, "argv", ["push_gallery_via_app.py", "--base-url", "https://example.test"]),
            mock.patch.dict(push_gallery_via_app.os.environ, {"GALLERY_UPLOAD_TOKEN": ""}, clear=False),
            mock.patch.object(push_gallery_via_app, "request_json", side_effect=AssertionError("status should not be called")),
            redirect_stderr(stderr),
        ):
            code = push_gallery_via_app.main()

        self.assertEqual(code, 2)
        self.assertIn("missing --token or GALLERY_UPLOAD_TOKEN", stderr.getvalue())

    def test_remote_disabled_token_status_fails_clearly(self) -> None:
        stderr = io.StringIO()
        disabled_status = {
            "enabled": False,
            "cosReady": True,
            "bucket": "bucket-123",
            "region": "ap-guangzhou",
            "prefix": "waimai-gallery",
            "indexUrl": "https://bucket-123.cos.ap-guangzhou.myqcloud.com/waimai-gallery/index/library_index.jsonl",
        }
        with (
            mock.patch.object(
                sys,
                "argv",
                [
                    "push_gallery_via_app.py",
                    "--base-url",
                    "https://example.test",
                    "--token",
                    "local-token",
                    "--wait-ready",
                    "0",
                ],
            ),
            mock.patch.object(push_gallery_via_app, "request_json", return_value=disabled_status),
            mock.patch.object(push_gallery_via_app, "scan_library", side_effect=AssertionError("scan should not run")),
            redirect_stderr(stderr),
        ):
            code = push_gallery_via_app.main()

        self.assertEqual(code, 1)
        message = stderr.getvalue()
        self.assertIn("remote gallery upload is disabled", message)
        self.assertIn("GALLERY_UPLOAD_TOKEN", message)


if __name__ == "__main__":
    unittest.main()
