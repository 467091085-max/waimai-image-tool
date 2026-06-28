from __future__ import annotations

import sqlite3
import unittest

import payment_service as payments


SECRET = "local-secret"


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

    def test_alipay_readiness_with_credentials_still_requires_real_adapter(self) -> None:
        readiness = payments.assess_payment_provider_readiness(
            {
                "APP_ENV": "production",
                "PAYMENT_PROVIDER": "alipay",
                "ENABLE_LOCAL_DEMO_BILLING": "false",
                "ALIPAY_APP_ID": "app-id",
                "ALIPAY_PRIVATE_KEY": "private-key",
                "ALIPAY_PUBLIC_KEY": "public-key",
                "PAYMENT_NOTIFY_URL": "https://example.test/payments/alipay/notify",
            }
        )

        self.assertFalse(readiness["ready"])
        self.assertEqual(readiness["provider"], "alipay")
        self.assertEqual(readiness["missingConfig"], [])
        self.assertNotIn("payment_provider_credentials_required", readiness["errors"])
        self.assertIn("alipay_payment_adapter_not_implemented", readiness["errors"])
        self.assertIn("real_payment_callback_signature_verification_not_implemented", readiness["errors"])

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
