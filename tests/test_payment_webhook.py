from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import billing
import payment_webhook


class PaymentWebhookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "app.db"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_real_provider_without_credentials_returns_clear_not_configured_status(self) -> None:
        result, status = payment_webhook.handle_payment_webhook(
            {
                "eventId": "evt-wechat-1",
                "paymentOrderId": "pay-wechat-1",
                "provider": "wechat",
                "status": "paid",
            },
            headers={"X-Payment-Signature": "anything"},
            db_path=self.db_path,
        )

        self.assertEqual(status, 501)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "payment_provider_not_configured")
        self.assertEqual(result["status"], "not_configured")

    def test_invalid_mock_signature_is_recorded_without_credit(self) -> None:
        billing.create_payment_order("u1", "pay-49", cash_amount=49, db_path=self.db_path)

        result, status = payment_webhook.handle_payment_webhook(
            {
                "eventId": "evt-bad-signature",
                "paymentOrderId": "pay-49",
                "provider": "mock",
                "status": "paid",
            },
            headers={"X-Mock-Signature": "bad"},
            db_path=self.db_path,
        )
        admin = billing.admin_billing_payload(self.db_path)

        self.assertEqual(status, 401)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "payment_signature_invalid")
        self.assertEqual(billing.get_account("u1", db_path=self.db_path)["balance"], 0)
        self.assertEqual(admin["summary"]["paymentWebhookEventCount"], 1)
        self.assertFalse(admin["paymentWebhooks"][0]["signatureValid"])


if __name__ == "__main__":
    unittest.main()
