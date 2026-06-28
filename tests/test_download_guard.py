from __future__ import annotations

import unittest

import asset_security
import download_guard


class DownloadGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.secret = "download-secret"
        self.now = 1_800_000_000
        self.asset = {
            "asset_id": "asset-1",
            "user_id": "user-1",
            "order_id": "order-1",
            "job_id": "job-1",
            "allowed_purposes": asset_security.ASSET_PURPOSES,
            "available_variants": asset_security.ASSET_VARIANTS,
        }
        self.user_context = {"user_id": "user-1"}
        self.order_context = {"order_id": "order-1"}
        self.job_context = {"job_id": "job-1"}

    def test_allows_original_download_when_token_and_context_match(self) -> None:
        token = self._token(asset_security.ORIGINAL, asset_security.ORIGINAL)

        decision = self._authorize(token, asset_security.ORIGINAL, asset_security.ORIGINAL)

        self.assertTrue(decision["allowed"])
        self.assertEqual(decision["reason"], download_guard.REASON_ALLOWED)
        self.assertEqual(decision["asset_id"], "asset-1")
        self.assertEqual(decision["action"], "download")
        self.assertEqual(decision["expires_at"], self.now + 60)
        self.assertEqual(decision["audit"]["token_nonce"], "nonce-1")

    def test_denies_expired_token(self) -> None:
        token = self._token(asset_security.ORIGINAL, asset_security.ORIGINAL, expires_at=self.now - 1)

        decision = self._authorize(token, asset_security.ORIGINAL, asset_security.ORIGINAL)

        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["reason"], download_guard.REASON_TOKEN_EXPIRED)
        self.assertEqual(decision["expires_at"], self.now - 1)
        self.assertEqual(decision["audit"]["deny_reason"], download_guard.REASON_TOKEN_EXPIRED)

    def test_denies_bad_signature(self) -> None:
        token = self._token(asset_security.ORIGINAL, asset_security.ORIGINAL)
        payload_part, signature_part = token.split(".")
        replacement = "A" if signature_part[0] != "A" else "B"
        tampered = f"{payload_part}.{replacement}{signature_part[1:]}"

        decision = self._authorize(tampered, asset_security.ORIGINAL, asset_security.ORIGINAL)

        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["reason"], download_guard.REASON_INVALID_SIGNATURE)
        self.assertEqual(decision["audit"]["token_asset_id"], "asset-1")

    def test_denies_user_context_mismatch(self) -> None:
        token = self._token(asset_security.ORIGINAL, asset_security.ORIGINAL)

        decision = download_guard.authorize_download(
            self.asset,
            {"user_id": "user-2"},
            self.order_context,
            self.job_context,
            asset_security.ORIGINAL,
            asset_security.ORIGINAL,
            token,
            self.secret,
            now=self.now,
        )

        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["reason"], download_guard.REASON_USER_MISMATCH)
        self.assertEqual(decision["audit"]["actor_user_id"], "user-2")

    def test_denies_purpose_mismatch(self) -> None:
        token = self._token(asset_security.ORIGINAL, asset_security.ORIGINAL)

        decision = self._authorize(token, asset_security.EXPORT, asset_security.ORIGINAL)

        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["reason"], download_guard.REASON_PURPOSE_MISMATCH)
        self.assertEqual(decision["audit"]["token_purpose"], asset_security.ORIGINAL)
        self.assertEqual(decision["audit"]["requested_purpose"], asset_security.EXPORT)

    def test_denies_variant_for_wrong_purpose_policy(self) -> None:
        token = self._token(asset_security.PREVIEW, asset_security.ORIGINAL)

        decision = self._authorize(token, asset_security.PREVIEW, asset_security.ORIGINAL)

        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["reason"], download_guard.REASON_PURPOSE_VARIANT_MISMATCH)

    def test_denies_replayed_one_time_download_token(self) -> None:
        token = self._token(asset_security.ORIGINAL, asset_security.ORIGINAL)
        claims = asset_security.verify_asset_url_token(token, self.secret, now=self.now)
        consumed_key = asset_security.asset_token_consumption_key(claims)

        decision = download_guard.authorize_download(
            self.asset,
            self.user_context,
            self.order_context,
            self.job_context,
            asset_security.ORIGINAL,
            asset_security.ORIGINAL,
            token,
            self.secret,
            now=self.now,
            consumed_token_keys={consumed_key},
        )

        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["reason"], download_guard.REASON_TOKEN_REPLAYED)
        self.assertEqual(decision["audit"]["token_consumption_key"], consumed_key)
        self.assertTrue(decision["audit"]["token_one_time_required"])

    def test_allows_preview_token_even_when_nonce_seen(self) -> None:
        token = self._token(asset_security.PREVIEW, asset_security.PREVIEW)
        claims = asset_security.verify_asset_url_token(token, self.secret, now=self.now)
        consumed_key = asset_security.asset_token_consumption_key(claims)

        decision = download_guard.authorize_download(
            self.asset,
            self.user_context,
            self.order_context,
            self.job_context,
            asset_security.PREVIEW,
            asset_security.PREVIEW,
            token,
            self.secret,
            now=self.now,
            consumed_token_keys={consumed_key},
        )

        self.assertTrue(decision["allowed"])
        self.assertEqual(decision["reason"], download_guard.REASON_ALLOWED)
        self.assertEqual(decision["audit"]["token_consumption_key"], consumed_key)
        self.assertFalse(decision["audit"]["token_one_time_required"])

    def test_allows_admin_review_for_admin_context(self) -> None:
        token = self._token(
            asset_security.ADMIN_REVIEW,
            asset_security.ORIGINAL,
            user_id="admin-1",
            can_admin_review=True,
        )

        decision = download_guard.authorize_download(
            self.asset,
            {"user_id": "admin-1", "role": "admin"},
            self.order_context,
            self.job_context,
            asset_security.ADMIN_REVIEW,
            asset_security.ORIGINAL,
            token,
            self.secret,
            now=self.now,
        )

        self.assertTrue(decision["allowed"])
        self.assertEqual(decision["reason"], download_guard.REASON_ALLOWED)
        self.assertEqual(decision["action"], "admin_review")
        self.assertEqual(decision["audit"]["asset_user_id"], "user-1")
        self.assertEqual(decision["audit"]["actor_user_id"], "admin-1")

    def _authorize(self, token: str, purpose: str, variant: str) -> dict[str, object]:
        return download_guard.authorize_download(
            self.asset,
            self.user_context,
            self.order_context,
            self.job_context,
            purpose,
            variant,
            token,
            self.secret,
            now=self.now,
        )

    def _token(
        self,
        purpose: str,
        variant: str,
        *,
        asset_id: str = "asset-1",
        user_id: str = "user-1",
        order_id: str = "order-1",
        job_id: str = "job-1",
        expires_at: int | None = None,
        can_admin_review: bool = False,
    ) -> str:
        payload = {
            "asset_id": asset_id,
            "user_id": user_id,
            "order_id": order_id,
            "job_id": job_id,
            "variant": variant,
            "purpose": purpose,
            "expires_at": expires_at if expires_at is not None else self.now + 60,
            "nonce": "nonce-1",
        }
        if can_admin_review:
            payload["can_admin_review"] = True
        return asset_security.sign_asset_url(payload, self.secret, now=self.now)


if __name__ == "__main__":
    unittest.main()
