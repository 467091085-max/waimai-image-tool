from __future__ import annotations

import unittest

import risk_rules as rules


class RiskRulesTests(unittest.TestCase):
    def test_low_risk_registration_reward_is_allowed(self) -> None:
        decision = rules.evaluate_registration_reward_risk(
            phone_verified=True,
            human_verified=True,
            same_device_registrations_24h=0,
            same_ip_registrations_24h=0,
        )

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.level, rules.LEVEL_INFO)
        self.assertEqual(decision.decision, rules.DECISION_ALLOW)
        self.assertEqual(decision.reasons, ())
        self.assertEqual(decision.cooldown_seconds, 0)

    def test_referral_rebate_review_for_shared_payment_account(self) -> None:
        decision = rules.evaluate_referral_rebate_risk(payment_account_users_30d=2)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.level, rules.LEVEL_MEDIUM)
        self.assertEqual(decision.decision, rules.DECISION_REVIEW)
        self.assertEqual(decision.reasons, ("shared_payment_account",))

    def test_registration_reward_freezes_for_reward_freeze_or_device_limit(self) -> None:
        decision = rules.evaluate_registration_reward_risk(
            registration_reward_frozen=True,
            same_device_registrations_24h=2,
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.level, rules.LEVEL_HIGH)
        self.assertEqual(decision.decision, rules.DECISION_FREEZE)
        self.assertEqual(
            decision.reasons,
            ("registration_reward_frozen", "device_registration_limit_reached"),
        )
        self.assertEqual(decision.cooldown_seconds, rules.FREEZE_COOLDOWN_SECONDS)

    def test_sms_otp_denies_rate_limited_phone_and_ip(self) -> None:
        decision = rules.evaluate_sms_otp_risk(
            phone_otp_requests_1h=5,
            ip_otp_requests_1h=20,
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.level, rules.LEVEL_HIGH)
        self.assertEqual(decision.decision, rules.DECISION_DENY)
        self.assertEqual(
            decision.reasons,
            ("phone_otp_rate_limited", "ip_otp_rate_limited"),
        )
        self.assertEqual(decision.cooldown_seconds, rules.RATE_LIMIT_COOLDOWN_SECONDS)

    def test_sms_otp_denies_after_too_many_verification_attempts(self) -> None:
        decision = rules.evaluate_sms_otp_risk(otp_attempts=5)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.level, rules.LEVEL_HIGH)
        self.assertEqual(decision.decision, rules.DECISION_DENY)
        self.assertEqual(decision.reasons, ("otp_attempt_limit_reached",))
        self.assertEqual(decision.cooldown_seconds, rules.SHORT_COOLDOWN_SECONDS)

    def test_invite_reward_denies_unverified_self_and_duplicate_claims(self) -> None:
        decision = rules.evaluate_invite_reward_risk(
            phone_verified=False,
            self_invite=True,
            reward_already_claimed=True,
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.level, rules.LEVEL_CRITICAL)
        self.assertEqual(decision.decision, rules.DECISION_DENY)
        self.assertEqual(
            decision.reasons,
            ("phone_not_verified", "self_invite", "reward_already_claimed"),
        )

    def test_invite_reward_blocks_short_window_registration_clusters(self) -> None:
        decision = rules.evaluate_invite_reward_risk(
            same_phone_registrations_24h=1,
            same_device_registrations_24h=2,
            same_ip_registrations_24h=5,
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.level, rules.LEVEL_CRITICAL)
        self.assertEqual(decision.decision, rules.DECISION_DENY)
        self.assertEqual(
            decision.reasons,
            (
                "phone_registration_limit_reached",
                "device_registration_limit_reached",
                "ip_registration_limit_reached",
            ),
        )

    def test_invitation_self_invite_denies_rebate(self) -> None:
        decision = rules.evaluate_referral_rebate_risk(self_invite=True)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.level, rules.LEVEL_CRITICAL)
        self.assertEqual(decision.decision, rules.DECISION_DENY)
        self.assertEqual(decision.reasons, ("self_invite",))

    def test_agent_commission_denies_critical_refund_rate(self) -> None:
        decision = rules.evaluate_agent_commission_risk(
            refund_rate_30d=0.6,
            refund_orders_30d=3,
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.level, rules.LEVEL_CRITICAL)
        self.assertEqual(decision.decision, rules.DECISION_DENY)
        self.assertEqual(decision.reasons, ("refund_rate_critical",))

    def test_download_frequency_has_review_freeze_and_deny_bands(self) -> None:
        review = rules.evaluate_download_risk(downloads_1h=20)
        freeze = rules.evaluate_download_risk(downloads_24h=200)
        deny = rules.evaluate_download_risk(downloads_1h=80)

        self.assertEqual(review.decision, rules.DECISION_REVIEW)
        self.assertEqual(review.reasons, ("download_rate_elevated",))
        self.assertEqual(freeze.decision, rules.DECISION_FREEZE)
        self.assertEqual(freeze.reasons, ("download_rate_high",))
        self.assertEqual(deny.decision, rules.DECISION_DENY)
        self.assertEqual(deny.reasons, ("download_rate_critical",))

    def test_multiple_reasons_merge_and_escalate_to_strongest_decision(self) -> None:
        decision = rules.evaluate_referral_rebate_risk(
            phone_verified=False,
            same_ip_registrations_24h=5,
            payment_account_users_30d=2,
            related_account_count=1,
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.level, rules.LEVEL_HIGH)
        self.assertEqual(decision.decision, rules.DECISION_FREEZE)
        self.assertEqual(
            decision.reasons,
            (
                "phone_not_verified",
                "ip_registration_limit_reached",
                "related_account",
                "shared_payment_account",
            ),
        )
        self.assertEqual(decision.cooldown_seconds, rules.FREEZE_COOLDOWN_SECONDS)

    def test_evaluate_risk_accepts_mapping_stats_and_aliases(self) -> None:
        decision = rules.evaluate_risk(
            "otp_request",
            {
                "phone_number_valid": False,
                "phone_otp_requests_1h": 5,
            },
        )

        self.assertEqual(decision.level, rules.LEVEL_CRITICAL)
        self.assertEqual(decision.decision, rules.DECISION_DENY)
        self.assertEqual(
            decision.reasons,
            ("invalid_phone_number", "phone_otp_rate_limited"),
        )

    def test_evaluate_all_risks_returns_all_supported_categories(self) -> None:
        decisions = rules.evaluate_all_risks()

        self.assertEqual(
            set(decisions),
            {
                rules.RISK_REGISTRATION_REWARD,
                rules.RISK_SMS_OTP,
                rules.RISK_INVITE_REWARD,
                rules.RISK_REFERRAL_REBATE,
                rules.RISK_AGENT_COMMISSION,
                rules.RISK_DOWNLOAD,
            },
        )
        self.assertTrue(all(decision.allowed for decision in decisions.values()))

    def test_invalid_stats_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            rules.evaluate_download_risk(downloads_1h=-1)

        with self.assertRaises(TypeError):
            rules.evaluate_sms_otp_risk(phone_otp_requests_1h=True)  # type: ignore[arg-type]

        with self.assertRaises(ValueError):
            rules.evaluate_agent_commission_risk(refund_rate_30d=1.1)


if __name__ == "__main__":
    unittest.main()
