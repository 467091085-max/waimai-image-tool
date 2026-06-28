from __future__ import annotations

import sqlite3
import unittest
from urllib.parse import parse_qs, urlparse

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import payment_service as payments


SECRET = "local-secret"


def _alipay_key_env() -> dict[str, str]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    return {
        "APP_ENV": "production",
        "PAYMENT_PROVIDER": "alipay",
        "ENABLE_LOCAL_DEMO_BILLING": "false",
        "ALIPAY_APP_ID": "2021000000000000",
        "ALIPAY_PRIVATE_KEY": private_pem,
        "ALIPAY_PUBLIC_KEY": public_pem,
        "PAYMENT_NOTIFY_URL": "https://example.test/api/payments/alipay/notify",
        "ALIPAY_GATEWAY_URL": "https://example.test/alipay",
    }


def _signed_alipay_payload(env: dict[str, str], provider_order_id: str, status: str = "TRADE_SUCCESS") -> dict[str, str]:
    payload = {
        "app_id": env["ALIPAY_APP_ID"],
        "out_trade_no": provider_order_id,
        "trade_no": "2026062922000000000001",
        "trade_status": status,
        "total_amount": "49.00",
        "sign_type": "RSA2",
    }
    payload["sign"] = payments._alipay_rsa2_sign(  # type: ignore[attr-defined]
        payments._alipay_signing_string(payload),  # type: ignore[attr-defined]
        payments._load_alipay_private_key(env),  # type: ignore[attr-defined]
    )
    return payload


class PaymentServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        payments.init_payment_schema(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_create_payment_order_returns_pending_fake_order(self) -> None:
        order = payments.create_payment_order(
            self.conn,
            user_id="u1",
            amount_cents=4900,
            points=500,
            order_id="order-1",
        )

        self.assertFalse(order["idempotent"])
        self.assertEqual(order["status"], "pending")
        self.assertEqual(order["provider"], "fake")
        self.assertEqual(order["provider_order_id"], "order-1")
        self.assertEqual(order["payment_url"], payments.fake_payment_url(order))
        self.assertEqual(payments.payment_instructions(order)["payment_url"], order["payment_url"])

        row = self.conn.execute(
            "SELECT user_id, amount_cents, points, status FROM payment_orders WHERE order_id = ?",
            ("order-1",),
        ).fetchone()
        self.assertEqual(row, ("u1", 4900, 500, "pending"))

    def test_fake_payment_provider_enabled_defaults_to_local_demo(self) -> None:
        self.assertTrue(payments.fake_payment_provider_enabled({}))
        self.assertTrue(payments.fake_payment_provider_enabled({"ENABLE_LOCAL_DEMO_BILLING": "true"}))

    def test_fake_payment_provider_disabled_in_live_runtime(self) -> None:
        self.assertFalse(
            payments.fake_payment_provider_enabled(
                {
                    "APP_ENV": "staging",
                    "ENABLE_LOCAL_DEMO_BILLING": "true",
                    "PAYMENT_PROVIDER": "fake",
                    "ALLOW_FAKE_PAYMENT_PROVIDER": "true",
                }
            )
        )
        self.assertFalse(
            payments.fake_payment_provider_enabled(
                {
                    "PUBLIC_BASE_URL": "https://waimai-image-tool-1.onrender.com",
                    "ENABLE_LOCAL_DEMO_BILLING": "true",
                }
            )
        )

    def test_fake_payment_provider_disabled_without_explicit_override(self) -> None:
        self.assertFalse(payments.fake_payment_provider_enabled({"ENABLE_LOCAL_DEMO_BILLING": "false"}))
        self.assertFalse(
            payments.fake_payment_provider_enabled(
                {
                    "ENABLE_LOCAL_DEMO_BILLING": "0",
                    "PAYMENT_PROVIDER": "wechat",
                    "ALLOW_FAKE_PAYMENT_PROVIDER": "false",
                }
            )
        )

    def test_fake_payment_provider_enabled_by_explicit_fake_config(self) -> None:
        self.assertTrue(
            payments.fake_payment_provider_enabled(
                {
                    "ENABLE_LOCAL_DEMO_BILLING": "false",
                    "PAYMENT_PROVIDER": "fake",
                }
            )
        )
        self.assertTrue(
            payments.fake_payment_provider_enabled(
                {
                    "ENABLE_LOCAL_DEMO_BILLING": "false",
                    "ALLOW_FAKE_PAYMENT_PROVIDER": "true",
                }
            )
        )

    def test_payment_readiness_allows_local_fake_with_development_warning(self) -> None:
        readiness = payments.assess_payment_provider_readiness({})

        self.assertTrue(readiness["ready"])
        self.assertEqual(readiness["provider"], "fake")
        self.assertEqual(readiness["mode"], "local_demo")
        self.assertEqual(readiness["errors"], [])
        self.assertEqual(readiness["blockingIssues"], [])
        self.assertEqual(readiness["requiredConfig"], [])
        self.assertIn("fake_payment_provider_is_for_development_only", readiness["warnings"])

    def test_payment_readiness_rejects_fake_in_production(self) -> None:
        readiness = payments.assess_payment_provider_readiness({"APP_ENV": "production"})

        self.assertFalse(readiness["ready"])
        self.assertEqual(readiness["provider"], "fake")
        self.assertEqual(readiness["mode"], "local_demo")
        self.assertEqual(readiness["appEnv"], "production")
        self.assertIn("real_payment_provider_required", readiness["errors"])
        self.assertIn("fake_payment_provider_forbidden_in_live_environment", readiness["errors"])
        self.assertIn("fake_payment_provider_forbidden_in_production", readiness["errors"])
        self.assertEqual(readiness["blockingIssues"], readiness["errors"])
        self.assertEqual(readiness["requiredConfig"][0]["key"], "payment_provider")

    def test_payment_readiness_rejects_fake_on_render_runtime(self) -> None:
        readiness = payments.assess_payment_provider_readiness(
            {
                "PUBLIC_BASE_URL": "https://waimai-image-tool-1.onrender.com",
                "ENABLE_LOCAL_DEMO_BILLING": "true",
            }
        )

        self.assertFalse(readiness["ready"])
        self.assertEqual(readiness["provider"], "fake")
        self.assertEqual(readiness["mode"], "local_demo")
        self.assertEqual(readiness["appEnv"], "render")
        self.assertIn("real_payment_provider_required", readiness["errors"])
        self.assertIn("fake_payment_provider_forbidden_in_live_environment", readiness["errors"])
        self.assertEqual(readiness["blockingIssues"], readiness["errors"])

    def test_payment_readiness_rejects_fake_when_local_demo_billing_is_disabled(self) -> None:
        readiness = payments.assess_payment_provider_readiness(
            {
                "ENABLE_LOCAL_DEMO_BILLING": "false",
                "PAYMENT_PROVIDER": "fake",
            }
        )

        self.assertFalse(readiness["ready"])
        self.assertEqual(readiness["provider"], "fake")
        self.assertIn("real_payment_provider_required", readiness["errors"])
        self.assertIn("local_demo_billing_disabled", readiness["errors"])

        no_provider_readiness = payments.assess_payment_provider_readiness(
            {"ENABLE_LOCAL_DEMO_BILLING": "false"}
        )
        self.assertFalse(no_provider_readiness["ready"])
        self.assertEqual(no_provider_readiness["provider"], "fake")
        self.assertIn("real_payment_provider_required", no_provider_readiness["errors"])

    def test_wechat_readiness_requires_credentials_and_real_adapter(self) -> None:
        readiness = payments.assess_payment_provider_readiness(
            {
                "APP_ENV": "production",
                "PAYMENT_PROVIDER": "wechat",
                "ENABLE_LOCAL_DEMO_BILLING": "false",
            }
        )

        self.assertFalse(readiness["ready"])
        self.assertEqual(readiness["provider"], "wechat")
        self.assertEqual(readiness["mode"], "real_provider")
        self.assertIn("payment_provider_credentials_required", readiness["errors"])
        self.assertIn("wechat_payment_adapter_not_implemented", readiness["errors"])
        self.assertIn("real_payment_callback_signature_verification_not_implemented", readiness["errors"])
        self.assertIn("wechat_app_id", readiness["missingConfig"])
        self.assertIn("wechat_merchant_id", readiness["missingConfig"])
        self.assertTrue(any(item["key"] == "wechat_api_v3_key" for item in readiness["requiredConfig"]))

    def test_alipay_readiness_accepts_complete_credentials_and_adapter(self) -> None:
        readiness = payments.assess_payment_provider_readiness(_alipay_key_env())

        self.assertTrue(readiness["ready"])
        self.assertEqual(readiness["provider"], "alipay")
        self.assertEqual(readiness["missingConfig"], [])
        self.assertNotIn("payment_provider_credentials_required", readiness["errors"])
        self.assertEqual(readiness["blockingIssues"], [])
        self.assertIn("alipay_page_pay_adapter_enabled", readiness["warnings"])

    def test_alipay_checkout_generates_signed_page_pay_url(self) -> None:
        env = _alipay_key_env()
        order = payments.create_payment_order(
            self.conn,
            user_id="u1",
            amount_cents=4900,
            points=490,
            provider="alipay",
            order_id="alipay-order-1",
        )

        checkout = payments.create_payment_checkout(order, env)
        parsed = urlparse(checkout["payment_url"])
        query = parse_qs(parsed.query)

        self.assertEqual(parsed.scheme + "://" + parsed.netloc + parsed.path, env["ALIPAY_GATEWAY_URL"])
        self.assertEqual(query["method"], ["alipay.trade.page.pay"])
        self.assertEqual(query["sign_type"], ["RSA2"])
        self.assertEqual(query["notify_url"], [env["PAYMENT_NOTIFY_URL"]])
        self.assertEqual(checkout["provider_order_id"], "alipay-order-1")

    def test_alipay_callback_signature_marks_order_paid(self) -> None:
        env = _alipay_key_env()
        order = payments.create_payment_order(
            self.conn,
            user_id="u1",
            amount_cents=4900,
            points=490,
            provider="alipay",
            order_id="alipay-order-1",
        )
        payload = _signed_alipay_payload(env, order["provider_order_id"])

        result = payments.handle_payment_callback(
            self.conn,
            "alipay",
            order["provider_order_id"],
            payments.alipay_callback_event_type(payload),
            payload,
            secret=env["ALIPAY_PUBLIC_KEY"],
        )

        self.assertEqual(result["status"], "paid")
        self.assertEqual(result["points_to_credit"], 490)

    def test_alipay_callback_rejects_invalid_signature(self) -> None:
        env = _alipay_key_env()
        order = payments.create_payment_order(
            self.conn,
            user_id="u1",
            amount_cents=4900,
            points=490,
            provider="alipay",
            order_id="alipay-order-1",
        )
        payload = _signed_alipay_payload(env, order["provider_order_id"])
        payload["total_amount"] = "1.00"

        with self.assertRaises(payments.PaymentSignatureError):
            payments.handle_payment_callback(
                self.conn,
                "alipay",
                order["provider_order_id"],
                payments.alipay_callback_event_type(payload),
                payload,
                secret=env["ALIPAY_PUBLIC_KEY"],
            )

    def test_real_provider_checkout_guard_fails_closed_until_adapter_exists(self) -> None:
        with self.assertRaises(payments.PaymentAdapterNotImplemented) as context:
            payments.ensure_payment_checkout_available(
                "wechatpay",
                {
                    "APP_ENV": "production",
                    "PAYMENT_PROVIDER": "wechat",
                    "ENABLE_LOCAL_DEMO_BILLING": "false",
                    "WECHAT_PAY_APP_ID": "wx-app-id",
                    "WECHAT_PAY_MCH_ID": "mch-id",
                    "WECHAT_PAY_API_V3_KEY": "api-v3-key",
                    "WECHAT_PAY_PRIVATE_KEY": "private-key",
                    "WECHAT_PAY_CERT_SERIAL_NO": "serial",
                    "WECHAT_PAY_PLATFORM_CERT": "platform-cert",
                    "PAYMENT_NOTIFY_URL": "https://example.test/payments/wechat/notify",
                },
            )

        self.assertEqual(context.exception.code, "payment_adapter_not_implemented")
        details = context.exception.to_dict()
        self.assertEqual(details["provider"], "wechat")
        self.assertEqual(details["missingConfig"], [])
        self.assertIn("wechat_payment_adapter_not_implemented", details["blockingIssues"])

    def test_real_provider_checkout_guard_reports_missing_credentials(self) -> None:
        with self.assertRaises(payments.PaymentProviderUnavailable) as context:
            payments.ensure_payment_checkout_available(
                "alipay",
                {
                    "APP_ENV": "production",
                    "PAYMENT_PROVIDER": "alipay",
                    "ENABLE_LOCAL_DEMO_BILLING": "false",
                },
            )

        details = context.exception.to_dict()
        self.assertEqual(details["code"], "payment_provider_unavailable")
        self.assertEqual(details["provider"], "alipay")
        self.assertIn("alipay_app_id", details["missingConfig"])
        self.assertIn("payment_provider_credentials_required", details["blockingIssues"])

    def test_create_payment_order_is_idempotent_by_key(self) -> None:
        first = payments.create_payment_order(
            self.conn,
            user_id="u1",
            amount_cents=9900,
            points=1040,
            order_id="order-1",
            idempotency_key="idem-1",
        )
        second = payments.create_payment_order(
            self.conn,
            user_id="u1",
            amount_cents=9900,
            points=1040,
            order_id="ignored-on-retry",
            idempotency_key="idem-1",
        )

        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])
        self.assertEqual(second["order_id"], first["order_id"])
        self.assertEqual(self._count("payment_orders"), 1)

    def test_success_callback_marks_order_paid_and_returns_points_to_credit(self) -> None:
        order = self._create_order()
        payload = self._signed_payload(
            order["provider_order_id"],
            "pay_success",
            {"event_id": "evt-paid-1"},
        )

        result = payments.handle_payment_callback(
            self.conn,
            "fake",
            order["provider_order_id"],
            "pay_success",
            payload,
            secret=SECRET,
        )

        self.assertFalse(result["idempotent"])
        self.assertEqual(result["previous_status"], "pending")
        self.assertEqual(result["status"], "paid")
        self.assertEqual(result["points_to_credit"], 500)
        self.assertEqual(result["points_to_refund"], 0)
        self.assertEqual(self._order_status(order["order_id"]), "paid")

    def test_duplicate_success_callback_does_not_return_points_again(self) -> None:
        order = self._create_order()
        payload = self._signed_payload(
            order["provider_order_id"],
            "pay_success",
            {"event_id": "evt-paid-1"},
        )

        first = payments.handle_payment_callback(
            self.conn,
            "fake",
            order["provider_order_id"],
            "pay_success",
            payload,
            secret=SECRET,
        )
        second = payments.handle_payment_callback(
            self.conn,
            "fake",
            order["provider_order_id"],
            "pay_success",
            payload,
            secret=SECRET,
        )

        self.assertEqual(first["points_to_credit"], 500)
        self.assertTrue(second["idempotent"])
        self.assertEqual(second["points_to_credit"], 0)
        self.assertEqual(self._count("payment_events"), 1)

    def test_callback_rejects_invalid_fake_signature_without_writing_event(self) -> None:
        order = self._create_order()

        with self.assertRaises(payments.PaymentSignatureError):
            payments.handle_payment_callback(
                self.conn,
                "fake",
                order["provider_order_id"],
                "pay_success",
                {"event_id": "evt-bad-signature", "signature": "not-valid"},
                secret=SECRET,
            )

        self.assertEqual(self._order_status(order["order_id"]), "pending")
        self.assertEqual(self._count("payment_events"), 0)

    def test_refund_callback_marks_paid_order_refunded_and_returns_points_to_refund(self) -> None:
        order = self._create_order()
        paid_payload = self._signed_payload(
            order["provider_order_id"],
            "pay_success",
            {"event_id": "evt-paid-1"},
        )
        payments.handle_payment_callback(
            self.conn,
            "fake",
            order["provider_order_id"],
            "pay_success",
            paid_payload,
            secret=SECRET,
        )
        refund_payload = self._signed_payload(
            order["provider_order_id"],
            "refund_success",
            {"event_id": "evt-refund-1"},
        )

        result = payments.handle_payment_callback(
            self.conn,
            "fake",
            order["provider_order_id"],
            "refund_success",
            refund_payload,
            secret=SECRET,
        )

        self.assertEqual(result["previous_status"], "paid")
        self.assertEqual(result["status"], "refunded")
        self.assertEqual(result["points_to_credit"], 0)
        self.assertEqual(result["points_to_refund"], 500)
        self.assertEqual(self._order_status(order["order_id"]), "refunded")

    def test_illegal_status_transition_rolls_back_event(self) -> None:
        order = self._create_order()
        failed_payload = self._signed_payload(
            order["provider_order_id"],
            "payment_failed",
            {"event_id": "evt-failed-1"},
        )
        payments.handle_payment_callback(
            self.conn,
            "fake",
            order["provider_order_id"],
            "payment_failed",
            failed_payload,
            secret=SECRET,
        )
        paid_payload = self._signed_payload(
            order["provider_order_id"],
            "pay_success",
            {"event_id": "evt-paid-after-failed"},
        )

        with self.assertRaises(payments.PaymentTransitionError):
            payments.handle_payment_callback(
                self.conn,
                "fake",
                order["provider_order_id"],
                "pay_success",
                paid_payload,
                secret=SECRET,
            )

        self.assertEqual(self._order_status(order["order_id"]), "failed")
        self.assertEqual(self._count("payment_events"), 1)

    def _create_order(self) -> dict[str, object]:
        return payments.create_payment_order(
            self.conn,
            user_id="u1",
            amount_cents=4900,
            points=500,
            order_id="order-1",
        )

    def _signed_payload(
        self,
        provider_order_id: str,
        event_type: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        signed = dict(payload)
        signed["signature"] = payments.fake_callback_signature(
            "fake",
            provider_order_id,
            event_type,
            signed,
            SECRET,
        )
        return signed

    def _order_status(self, order_id: str) -> str:
        row = self.conn.execute(
            "SELECT status FROM payment_orders WHERE order_id = ?",
            (order_id,),
        ).fetchone()
        return row[0]

    def _count(self, table: str) -> int:
        row = self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return int(row[0])


if __name__ == "__main__":
    unittest.main()
