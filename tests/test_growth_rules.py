from __future__ import annotations

import unittest

import growth_rules as rules


class GrowthRulesTests(unittest.TestCase):
    def test_single_agent_commission_rate(self) -> None:
        self.assertEqual(rules.agent_commission("agent", 10000), 2000)
        self.assertEqual(rules.agent_commission("agent", 10000, is_first_order=False), 2000)
        self.assertEqual(rules.agent_commission("standard", 10000), 2000)

    def test_single_agent_instant_points(self) -> None:
        self.assertEqual(rules.agent_instant_points("agent"), 200)
        self.assertEqual(rules.agent_instant_points("standard"), 200)

    def test_consumer_registration_and_first_image_rewards(self) -> None:
        registration = rules.consumer_referral_rewards(rules.EVENT_INVITEE_REGISTERED)
        first_image = rules.consumer_referral_rewards(rules.EVENT_FIRST_IMAGE_COMPLETED)

        self.assertEqual(registration["inviter_points"], 50)
        self.assertEqual(registration["invitee_points"], 50)
        self.assertEqual(first_image["inviter_points"], 30)
        self.assertEqual(first_image["invitee_points"], 0)

    def test_zero_payment_has_no_invitation_payment_rewards(self) -> None:
        reward = rules.consumer_referral_rewards(rules.EVENT_FIRST_PAYMENT, paid_cents=0)

        self.assertEqual(reward["inviter_points"], 0)
        self.assertEqual(reward["invitee_points"], 0)
        self.assertEqual(reward["inviter_payment_points"], 0)
        self.assertEqual(reward["invitee_rebate_points"], 0)

    def test_payment_rewards_inviter_with_ten_percent_points(self) -> None:
        reward = rules.consumer_referral_rewards(rules.EVENT_FIRST_PAYMENT, paid_cents=20000)

        self.assertEqual(reward["inviter_payment_points"], 200)
        self.assertEqual(reward["inviter_bonus_points"], 0)
        self.assertEqual(reward["inviter_points"], 200)
        self.assertEqual(reward["invitee_rebate_points"], 0)
        self.assertEqual(reward["invitee_points"], 0)

    def test_first_recharge_helper_is_direct_only_and_ten_percent_points(self) -> None:
        reward = rules.consumer_first_recharge_rewards(4900)

        self.assertEqual(reward["inviter_payment_points"], 49)
        self.assertEqual(reward["inviter_points"], 49)
        self.assertEqual(reward["invitee_points"], 0)
        with self.assertRaises(ValueError):
            rules.consumer_first_recharge_rewards(4900, depth=2)

    def test_non_first_recharge_has_no_consumer_referral_rebate(self) -> None:
        reward = rules.consumer_first_recharge_rewards(4900, is_first_recharge=False)

        self.assertEqual(reward["inviter_payment_points"], 0)
        self.assertEqual(reward["inviter_points"], 0)

    def test_only_first_payment_gets_ten_percent_referral_points(self) -> None:
        reward = rules.consumer_referral_rewards(rules.EVENT_FIRST_PAYMENT, paid_cents=30000)
        alias_reward = rules.consumer_referral_rewards("recharge", paid_cents=30000)

        self.assertEqual(reward["inviter_payment_points"], 300)
        self.assertEqual(reward["inviter_points"], 300)
        self.assertEqual(reward["invitee_rebate_points"], 0)
        self.assertEqual(reward["invitee_points"], 0)
        self.assertEqual(alias_reward["inviter_payment_points"], 0)
        self.assertEqual(alias_reward["inviter_points"], 0)

    def test_gifted_points_do_not_qualify_for_agent_commission(self) -> None:
        self.assertFalse(rules.qualifies_for_commission(0, gifted_points=10000))
        self.assertTrue(rules.qualifies_for_commission(100, gifted_points=10000))
        self.assertEqual(rules.agent_commission("standard", 0), 0)

    def test_refunded_orders_do_not_qualify_for_agent_commission(self) -> None:
        self.assertFalse(rules.qualifies_for_commission(10000, is_refunded=True))

    def test_commission_and_referral_depth_are_locked_to_one_level(self) -> None:
        self.assertEqual(rules.validate_agent_commission_depth(1), 1)
        self.assertEqual(rules.validate_consumer_referral_depth(1), 1)
        with self.assertRaises(ValueError):
            rules.validate_agent_commission_depth(2)
        with self.assertRaises(ValueError):
            rules.validate_consumer_referral_depth(2)

    def test_registration_reward_requires_phone_and_human_verification(self) -> None:
        self.assertTrue(rules.registration_reward_allowed(phone_verified=True, human_verified=True))
        self.assertFalse(rules.registration_reward_allowed(phone_verified=False, human_verified=True))
        self.assertFalse(rules.registration_reward_allowed(phone_verified=True, human_verified=False))

    def test_invite_registration_reward_decision_returns_points_when_allowed(self) -> None:
        decision = rules.invite_registration_reward_decision(
            phone_verified=True,
            human_verified=True,
        )

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reasons, ())
        self.assertEqual(decision.inviter_points, 50)
        self.assertEqual(decision.invitee_points, 50)

    def test_registration_reward_blocks_obvious_abuse(self) -> None:
        self.assertFalse(rules.registration_reward_allowed(phone_verified=True, human_verified=True, same_phone_registered=True))
        self.assertFalse(rules.registration_reward_allowed(phone_verified=True, human_verified=True, risk_blocked=True))
        self.assertFalse(rules.registration_reward_allowed(phone_verified=True, human_verified=True, same_device_recent_registrations=2))
        self.assertFalse(rules.registration_reward_allowed(phone_verified=True, human_verified=True, same_ip_recent_registrations=5))
        self.assertFalse(rules.registration_reward_allowed(phone_verified=True, human_verified=True, same_device_recent_registrations=3))
        self.assertFalse(rules.registration_reward_allowed(phone_verified=True, human_verified=True, same_ip_recent_registrations=6))

    def test_invite_registration_reward_decision_reports_abuse_reasons(self) -> None:
        decision = rules.invite_registration_reward_decision(
            phone_verified=False,
            human_verified=False,
            same_phone_registered=True,
            same_device_recent_registrations=2,
            same_ip_recent_registrations=5,
            self_invite=True,
            reward_already_claimed=True,
            risk_blocked=True,
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(
            decision.reasons,
            (
                "phone_not_verified",
                "human_not_verified",
                "self_invite",
                "reward_already_claimed",
                "phone_already_registered",
                "risk_blocked",
                "device_registration_limit_reached",
                "ip_registration_limit_reached",
            ),
        )
        self.assertEqual(decision.inviter_points, 0)
        self.assertEqual(decision.invitee_points, 0)


if __name__ == "__main__":
    unittest.main()
