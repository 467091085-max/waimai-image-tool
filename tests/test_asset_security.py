from __future__ import annotations

import unittest

import asset_security


class AssetSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.secret = "test-secret"
        self.now = 1_800_000_000
        self.payload = {
            "asset_id": "asset-1",
            "user_id": "user-1",
            "order_id": "order-1",
            "variant": asset_security.PREVIEW,
            "purpose": asset_security.PREVIEW,
            "expires_at": self.now + 60,
            "nonce": "nonce-1",
        }

    def test_successful_verification_returns_payload(self) -> None:
        token = asset_security.sign_asset_url(self.payload, self.secret, now=self.now)

        verified = asset_security.verify_asset_url_token(token, self.secret, now=self.now)

        self.assertEqual(verified, self.payload)

    def test_expired_token_fails(self) -> None:
        payload = dict(self.payload, expires_at=self.now - 1)
        token = asset_security.sign_asset_url(payload, self.secret, now=self.now)

        with self.assertRaises(asset_security.ExpiredAssetTokenError):
            asset_security.verify_asset_url_token(token, self.secret, now=self.now)

    def test_tampered_token_fails(self) -> None:
        token = asset_security.sign_asset_url(self.payload, self.secret, now=self.now)
        payload_part, signature_part = token.split(".")
        replacement = "A" if payload_part[-1] != "A" else "B"
        tampered = f"{payload_part[:-1]}{replacement}.{signature_part}"

        with self.assertRaises(asset_security.InvalidAssetTokenError):
            asset_security.verify_asset_url_token(tampered, self.secret, now=self.now)

    def test_missing_field_fails(self) -> None:
        payload = dict(self.payload)
        del payload["order_id"]
        token = asset_security.sign_asset_url(payload, self.secret, now=self.now)

        with self.assertRaises(asset_security.MissingAssetTokenFieldError):
            asset_security.verify_asset_url_token(token, self.secret, now=self.now)

    def test_invalid_variant_fails(self) -> None:
        payload = dict(self.payload, variant="private")
        token = asset_security.sign_asset_url(payload, self.secret, now=self.now)

        with self.assertRaises(asset_security.InvalidAssetTokenClaimError):
            asset_security.verify_asset_url_token(token, self.secret, now=self.now)

    def test_invalid_purpose_fails(self) -> None:
        payload = dict(self.payload, purpose="private")
        token = asset_security.sign_asset_url(payload, self.secret, now=self.now)

        with self.assertRaises(asset_security.InvalidAssetTokenClaimError):
            asset_security.verify_asset_url_token(token, self.secret, now=self.now)

    def test_policy_defaults(self) -> None:
        self.assertEqual(
            asset_security.preview_policy(),
            {
                "ttl_seconds": 300,
                "cache_control": "private, max-age=300",
                "one_time": False,
            },
        )
        self.assertEqual(
            asset_security.download_policy(),
            {
                "ttl_seconds": 600,
                "cache_control": "private, no-store, max-age=0",
                "one_time": True,
            },
        )

    def test_consumption_key_is_stable_and_hashed(self) -> None:
        key = asset_security.asset_token_consumption_key(self.payload)
        same_key = asset_security.asset_token_consumption_key(dict(reversed(list(self.payload.items()))))

        self.assertEqual(key, same_key)
        self.assertTrue(key.startswith("asset-token:"))
        self.assertNotIn("asset-1", key)
        self.assertNotIn("user-1", key)
        self.assertNotIn("nonce-1", key)

    def test_consumption_key_requires_nonce_claims(self) -> None:
        payload = dict(self.payload)
        del payload["nonce"]

        with self.assertRaises(asset_security.MissingAssetTokenFieldError):
            asset_security.asset_token_consumption_key(payload)

    def test_one_time_consumption_policy(self) -> None:
        self.assertFalse(asset_security.requires_one_time_consumption(asset_security.PREVIEW))
        self.assertTrue(asset_security.requires_one_time_consumption(asset_security.ORIGINAL))
        self.assertTrue(asset_security.requires_one_time_consumption(asset_security.EXPORT))
        self.assertFalse(asset_security.requires_one_time_consumption(asset_security.ADMIN_REVIEW))


if __name__ == "__main__":
    unittest.main()
