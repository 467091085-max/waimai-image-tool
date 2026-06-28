from __future__ import annotations

import unittest

import auth_rules as rules


class AuthRulesTests(unittest.TestCase):
    def test_normalize_phone_accepts_mainland_mobile_formats(self) -> None:
        self.assertEqual(rules.normalize_phone("13800138000"), "+8613800138000")
        self.assertEqual(rules.normalize_phone("+8613800138000"), "+8613800138000")
        self.assertEqual(rules.normalize_phone(" +86 138-0013-8000 "), "+8613800138000")

    def test_normalize_phone_rejects_non_mainland_mobile_numbers(self) -> None:
        invalid_numbers = [
            "",
            "23800138000",
            "1380013800",
            "138001380000",
            "8613800138000",
            "+85213800138000",
            "+861380013800a",
        ]

        for phone in invalid_numbers:
            with self.subTest(phone=phone):
                with self.assertRaises(ValueError):
                    rules.normalize_phone(phone)

        with self.assertRaises(TypeError):
            rules.normalize_phone(None)  # type: ignore[arg-type]

    def test_otp_request_allowed_enforces_phone_and_ip_hourly_limits(self) -> None:
        self.assertTrue(rules.otp_request_allowed(phone_requests_1h=0, ip_requests_1h=0))
        self.assertTrue(rules.otp_request_allowed(phone_requests_1h=4, ip_requests_1h=19))
        self.assertFalse(rules.otp_request_allowed(phone_requests_1h=5, ip_requests_1h=0))
        self.assertFalse(rules.otp_request_allowed(phone_requests_1h=0, ip_requests_1h=20))

    def test_registration_otp_helper_returns_invalid_phone_result(self) -> None:
        result = rules.evaluate_phone_registration_otp("8613800138000")

        self.assertFalse(result.allowed)
        self.assertEqual(result.code, "invalid_phone")
        self.assertEqual(result.reasons, ("invalid_phone",))
        self.assertEqual(result.cooldown_seconds, 0)
        self.assertEqual(result.normalized_phone, "")

    def test_registration_otp_helper_enforces_send_attempt_and_rate_limits(self) -> None:
        result = rules.evaluate_phone_registration_otp(
            "13800138000",
            phone_requests_1h=5,
            ip_requests_1h=20,
            otp_attempts=5,
            seconds_since_last_otp=30,
        )

        self.assertFalse(result.allowed)
        self.assertEqual(result.code, "otp_attempt_limited")
        self.assertEqual(
            result.reasons,
            (
                "otp_send_cooldown",
                "phone_otp_rate_limited",
                "ip_otp_rate_limited",
                "otp_attempt_limit_reached",
            ),
        )
        self.assertEqual(result.cooldown_seconds, rules.DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS)
        self.assertEqual(result.normalized_phone, "+8613800138000")

    def test_registration_otp_helper_enforces_phone_device_and_ip_clusters(self) -> None:
        result = rules.evaluate_phone_registration_otp(
            "13800138000",
            same_phone_registrations_24h=1,
            device_registrations_24h=2,
            ip_registrations_24h=5,
        )

        self.assertFalse(result.allowed)
        self.assertEqual(result.code, "registration_rate_limited")
        self.assertEqual(
            result.reasons,
            (
                "phone_registration_limit_reached",
                "device_registration_limit_reached",
                "ip_registration_limit_reached",
            ),
        )
        self.assertEqual(
            result.cooldown_seconds,
            rules.DEFAULT_REGISTRATION_CLUSTER_COOLDOWN_SECONDS,
        )

    def test_otp_verification_attempt_limit(self) -> None:
        self.assertTrue(rules.otp_verification_allowed(4))
        self.assertFalse(rules.otp_verification_allowed(5))

    def test_registration_reward_requires_phone_and_human_verification(self) -> None:
        self.assertTrue(
            rules.can_issue_registration_reward(
                phone_verified=True,
                human_verified=True,
                same_phone_registered=False,
                device_registrations_24h=0,
                ip_registrations_24h=0,
                risk_blocked=False,
            )
        )
        self.assertFalse(
            rules.can_issue_registration_reward(
                phone_verified=False,
                human_verified=True,
                same_phone_registered=False,
                device_registrations_24h=0,
                ip_registrations_24h=0,
                risk_blocked=False,
            )
        )
        self.assertFalse(
            rules.can_issue_registration_reward(
                phone_verified=True,
                human_verified=False,
                same_phone_registered=False,
                device_registrations_24h=0,
                ip_registrations_24h=0,
                risk_blocked=False,
            )
        )

    def test_registration_reward_blocks_registered_phone_and_risk_block(self) -> None:
        base = {
            "phone_verified": True,
            "human_verified": True,
            "device_registrations_24h": 0,
            "ip_registrations_24h": 0,
        }

        self.assertFalse(
            rules.can_issue_registration_reward(
                **base,
                same_phone_registered=True,
                risk_blocked=False,
            )
        )
        self.assertFalse(
            rules.can_issue_registration_reward(
                **base,
                same_phone_registered=False,
                risk_blocked=True,
            )
        )

    def test_registration_reward_blocks_device_and_ip_thresholds(self) -> None:
        base = {
            "phone_verified": True,
            "human_verified": True,
            "same_phone_registered": False,
            "risk_blocked": False,
        }

        self.assertTrue(
            rules.can_issue_registration_reward(
                **base,
                device_registrations_24h=1,
                ip_registrations_24h=4,
            )
        )
        self.assertFalse(
            rules.can_issue_registration_reward(
                **base,
                device_registrations_24h=2,
                ip_registrations_24h=0,
            )
        )
        self.assertFalse(
            rules.can_issue_registration_reward(
                **base,
                device_registrations_24h=0,
                ip_registrations_24h=5,
            )
        )

    def test_negative_or_non_integer_counts_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            rules.otp_request_allowed(phone_requests_1h=-1, ip_requests_1h=0)

        with self.assertRaises(TypeError):
            rules.otp_request_allowed(phone_requests_1h=True, ip_requests_1h=0)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
