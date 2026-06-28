from __future__ import annotations

import unittest

import payment_rules as rules


class PaymentRulesTests(unittest.TestCase):
    def test_payment_statuses_are_defined(self) -> None:
        self.assertEqual(
            rules.PAYMENT_STATUSES,
            ("pending", "paid", "failed", "refunded", "closed"),
        )

    def test_transition_allows_valid_paths_and_idempotent_replays(self) -> None:
        self.assertEqual(rules.transition_payment_status("pending", "paid"), "paid")
        self.assertEqual(rules.transition_payment_status("pending", "failed"), "failed")
        self.assertEqual(rules.transition_payment_status("pending", "closed"), "closed")
        self.assertEqual(rules.transition_payment_status("paid", "refunded"), "refunded")
        self.assertEqual(rules.transition_payment_status("paid", "paid"), "paid")
        self.assertEqual(rules.transition_payment_status("refunded", "refunded"), "refunded")

    def test_transition_rejects_illegal_paths(self) -> None:
        with self.assertRaises(ValueError):
            rules.transition_payment_status("paid", "pending")
        with self.assertRaises(ValueError):
            rules.transition_payment_status("failed", "paid")
        with self.assertRaises(ValueError):
            rules.transition_payment_status("closed", "paid")
        with self.assertRaises(ValueError):
            rules.transition_payment_status("pending", "unknown")

    def test_idempotency_key_is_stable_and_event_specific(self) -> None:
        first = rules.idempotency_key(" WeChat ", "ORDER-123", "PAY_SUCCESS")
        second = rules.idempotency_key("wechat", "ORDER-123", "pay_success")
        different_event = rules.idempotency_key("wechat", "ORDER-123", "refund_success")

        self.assertEqual(first, second)
        self.assertNotEqual(first, different_event)
        self.assertTrue(first.startswith("payment:"))

    def test_payment_points_floor_to_whole_points(self) -> None:
        self.assertEqual(rules.payment_points(100), 10)
        self.assertEqual(rules.payment_points(199), 19)
        self.assertEqual(rules.payment_points(99), 9)
        self.assertEqual(rules.payment_points(250, point_rate=5), 12)

    def test_refund_points_are_proportional_and_capped(self) -> None:
        self.assertEqual(rules.refund_points(2500, 10000, 1000), 250)
        self.assertEqual(rules.refund_points(3333, 10000, 1000), 333)
        self.assertEqual(rules.refund_points(12000, 10000, 1000), 1000)
        self.assertEqual(rules.refund_points(0, 10000, 1000), 0)


if __name__ == "__main__":
    unittest.main()
