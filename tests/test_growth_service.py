from __future__ import annotations

import sqlite3
import unittest

import growth_service
import payment_service
import storage_db


SECRET = "growth-payment-secret"


class GrowthServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        storage_db.init_db(self.conn)
        payment_service.init_payment_schema(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_agent_profile_and_customer_binding_are_idempotent(self) -> None:
        agent = growth_service.create_agent_profile(self.conn, "agent-user", agent_code="A001")
        repeat_agent = growth_service.create_agent_profile(self.conn, "agent-user", agent_code="A001")

        self.assertFalse(agent["idempotent"])
        self.assertTrue(repeat_agent["idempotent"])
        self.assertEqual(repeat_agent["id"], agent["id"])

        relation = growth_service.bind_agent_customer(
            self.conn,
            agent_id=agent["id"],
            customer_id="customer-1",
            source="invite-code",
        )
        repeat_relation = growth_service.bind_agent_customer(
            self.conn,
            agent_id=agent["id"],
            customer_id="customer-1",
            source="invite-code",
        )

        self.assertFalse(relation["idempotent"])
        self.assertTrue(repeat_relation["idempotent"])
        self.assertEqual(repeat_relation["id"], relation["id"])

    def test_customer_cannot_belong_to_two_agents(self) -> None:
        first = growth_service.create_agent_profile(self.conn, "agent-user-1")
        second = growth_service.create_agent_profile(self.conn, "agent-user-2")
        growth_service.bind_agent_customer(self.conn, agent_id=first["id"], customer_id="customer-1")

        with self.assertRaises(growth_service.GrowthConflict):
            growth_service.bind_agent_customer(self.conn, agent_id=second["id"], customer_id="customer-1")

    def test_consumer_invite_registration_rewards_are_pending_until_marked(self) -> None:
        invite = growth_service.accept_consumer_invite(
            self.conn,
            inviter_user_id="inviter-1",
            invitee_user_id="invitee-1",
            phone_verified=True,
            human_verified=True,
        )

        self.assertEqual(invite["status"], "accepted")
        self.assertEqual(invite["rewardStatus"], "pending")
        self.assertEqual(invite["registrationRewards"], {"inviterPoints": 100, "inviteePoints": 20})

        granted = growth_service.mark_invite_reward_granted(self.conn, invite["id"])
        self.assertEqual(granted["status"], "rewarded")
        self.assertEqual(granted["rewardStatus"], "granted")

        repeat = growth_service.accept_consumer_invite(
            self.conn,
            inviter_user_id="inviter-1",
            invitee_user_id="invitee-1",
            phone_verified=True,
            human_verified=True,
        )
        self.assertTrue(repeat["idempotent"])
        self.assertEqual(repeat["registrationRewards"], {"inviterPoints": 0, "inviteePoints": 0})

    def test_payment_growth_creates_direct_agent_commission_and_first_payment_reward(self) -> None:
        agent = growth_service.create_agent_profile(self.conn, "agent-user")
        relation = growth_service.bind_agent_customer(self.conn, agent_id=agent["id"], customer_id="customer-1")
        invite = growth_service.accept_consumer_invite(
            self.conn,
            inviter_user_id="inviter-1",
            invitee_user_id="customer-1",
            phone_verified=True,
            human_verified=True,
        )
        growth_service.mark_invite_reward_granted(self.conn, invite["id"])
        order = self._paid_order("order-1", "customer-1", amount_cents=10000, points=1000)

        result = growth_service.record_payment_growth(
            self.conn,
            order_id=order["order_id"],
            customer_id="customer-1",
            paid_cents=10000,
            request_id="evt-1",
        )

        commission = result["agentCommission"]
        reward = result["consumerReferralReward"]
        self.assertIsNotNone(commission)
        self.assertEqual(commission["relationId"], relation["id"])
        self.assertEqual(commission["commissionAmount"], 2000)
        self.assertEqual(commission["commissionRateBps"], 2000)
        self.assertEqual(commission["status"], "pending")
        self.assertEqual(reward["inviterUserId"], "inviter-1")
        self.assertEqual(reward["inviterPoints"], 100)

        repeat = growth_service.record_payment_growth(
            self.conn,
            order_id=order["order_id"],
            customer_id="customer-1",
            paid_cents=10000,
            request_id="evt-1",
        )
        self.assertTrue(repeat["agentCommission"]["idempotent"])
        self.assertEqual(repeat["consumerReferralReward"]["inviterPoints"], 0)
        self.assertEqual(self._table_count("commission_orders"), 1)
        self.assertEqual(self._table_count("promotion_event_logs"), 1)

    def test_second_payment_still_creates_agent_commission_but_no_consumer_first_pay_reward(self) -> None:
        agent = growth_service.create_agent_profile(self.conn, "agent-user")
        growth_service.bind_agent_customer(self.conn, agent_id=agent["id"], customer_id="customer-1")
        growth_service.accept_consumer_invite(
            self.conn,
            inviter_user_id="inviter-1",
            invitee_user_id="customer-1",
            phone_verified=True,
            human_verified=True,
        )
        self._paid_order("order-1", "customer-1", amount_cents=4900, points=500)
        second = self._paid_order("order-2", "customer-1", amount_cents=9900, points=1040)

        result = growth_service.record_payment_growth(
            self.conn,
            order_id=second["order_id"],
            customer_id="customer-1",
            paid_cents=9900,
            request_id="evt-2",
        )

        self.assertEqual(result["agentCommission"]["commissionAmount"], 990)
        self.assertEqual(result["agentCommission"]["commissionRateBps"], 1000)
        self.assertIsNone(result["consumerReferralReward"])

    def test_full_refund_cancels_unsettled_commission_and_first_payment_reward(self) -> None:
        agent = growth_service.create_agent_profile(self.conn, "agent-user")
        growth_service.bind_agent_customer(self.conn, agent_id=agent["id"], customer_id="customer-1")
        invite = growth_service.accept_consumer_invite(
            self.conn,
            inviter_user_id="inviter-1",
            invitee_user_id="customer-1",
            phone_verified=True,
            human_verified=True,
        )
        growth_service.mark_invite_reward_granted(self.conn, invite["id"])
        order = self._paid_order("order-1", "customer-1", amount_cents=4900, points=500)
        growth_service.record_payment_growth(
            self.conn,
            order_id=order["order_id"],
            customer_id="customer-1",
            paid_cents=4900,
            request_id="evt-paid-1",
        )

        result = growth_service.record_payment_refund(
            self.conn,
            order_id=order["order_id"],
            customer_id="customer-1",
            paid_cents=4900,
            refund_cents=4900,
            request_id="evt-refund-1",
        )

        commission = result["agentCommissionRefund"]
        referral = result["consumerReferralRefund"]
        self.assertEqual(commission["status"], "refunded")
        self.assertEqual(commission["orderAmount"], 0)
        self.assertEqual(commission["commissionAmount"], 0)
        self.assertEqual(referral["inviterUserId"], "inviter-1")
        self.assertEqual(referral["inviterPointsToDebit"], 49)

        repeat = growth_service.record_payment_refund(
            self.conn,
            order_id=order["order_id"],
            customer_id="customer-1",
            paid_cents=4900,
            refund_cents=4900,
            request_id="evt-refund-1",
        )
        self.assertTrue(repeat["agentCommissionRefund"]["idempotent"])
        self.assertEqual(repeat["consumerReferralRefund"]["inviterPointsToDebit"], 0)

    def test_partial_then_full_refund_uses_cumulative_amount_and_request_idempotency(self) -> None:
        agent = growth_service.create_agent_profile(self.conn, "agent-user")
        growth_service.bind_agent_customer(self.conn, agent_id=agent["id"], customer_id="customer-1")
        invite = growth_service.accept_consumer_invite(
            self.conn,
            inviter_user_id="inviter-1",
            invitee_user_id="customer-1",
            phone_verified=True,
            human_verified=True,
        )
        growth_service.mark_invite_reward_granted(self.conn, invite["id"])
        order = self._paid_order("order-1", "customer-1", amount_cents=10000, points=1000)
        growth_service.record_payment_growth(
            self.conn,
            order_id=order["order_id"],
            customer_id="customer-1",
            paid_cents=10000,
            request_id="evt-paid-1",
        )

        result = growth_service.record_payment_refund(
            self.conn,
            order_id=order["order_id"],
            customer_id="customer-1",
            paid_cents=10000,
            refund_cents=5000,
            request_id="evt-refund-1",
        )

        self.assertEqual(result["agentCommissionRefund"]["status"], "pending")
        self.assertEqual(result["agentCommissionRefund"]["orderAmount"], 5000)
        self.assertEqual(result["agentCommissionRefund"]["commissionAmount"], 1000)
        self.assertEqual(result["consumerReferralRefund"]["inviterPointsToDebit"], 50)

        repeat_partial = growth_service.record_payment_refund(
            self.conn,
            order_id=order["order_id"],
            customer_id="customer-1",
            paid_cents=10000,
            refund_cents=5000,
            request_id="evt-refund-1",
        )
        self.assertTrue(repeat_partial["agentCommissionRefund"]["idempotent"])
        self.assertTrue(repeat_partial["consumerReferralRefund"]["idempotent"])
        self.assertEqual(repeat_partial["consumerReferralRefund"]["inviterPointsToDebit"], 0)

        full = growth_service.record_payment_refund(
            self.conn,
            order_id=order["order_id"],
            customer_id="customer-1",
            paid_cents=10000,
            refund_cents=10000,
            request_id="evt-refund-2",
        )
        self.assertEqual(full["agentCommissionRefund"]["status"], "refunded")
        self.assertEqual(full["agentCommissionRefund"]["orderAmount"], 0)
        self.assertEqual(full["agentCommissionRefund"]["commissionAmount"], 0)
        self.assertEqual(full["consumerReferralRefund"]["inviterPointsToDebit"], 50)
        self.assertEqual(full["consumerReferralRefund"]["cumulativeInviterPointsDebited"], 100)

        repeat_full = growth_service.record_payment_refund(
            self.conn,
            order_id=order["order_id"],
            customer_id="customer-1",
            paid_cents=10000,
            refund_cents=10000,
            request_id="evt-refund-2",
        )
        self.assertTrue(repeat_full["agentCommissionRefund"]["idempotent"])
        self.assertEqual(repeat_full["consumerReferralRefund"]["inviterPointsToDebit"], 0)
        self.assertEqual(
            self.conn.execute(
                """
                SELECT COUNT(*) FROM promotion_event_logs
                WHERE event_type = 'consumer_first_payment_refund'
                """
            ).fetchone()[0],
            2,
        )

    def test_refund_cumulative_watermark_ignores_out_of_order_and_caps_over_refund(self) -> None:
        agent = growth_service.create_agent_profile(self.conn, "agent-user")
        growth_service.bind_agent_customer(self.conn, agent_id=agent["id"], customer_id="customer-1")
        invite = growth_service.accept_consumer_invite(
            self.conn,
            inviter_user_id="inviter-1",
            invitee_user_id="customer-1",
            phone_verified=True,
            human_verified=True,
        )
        growth_service.mark_invite_reward_granted(self.conn, invite["id"])
        order = self._paid_order("order-1", "customer-1", amount_cents=10000, points=1000)
        growth_service.record_payment_growth(
            self.conn,
            order_id=order["order_id"],
            customer_id="customer-1",
            paid_cents=10000,
            request_id="evt-paid-1",
        )

        first = growth_service.record_payment_refund(
            self.conn,
            order_id=order["order_id"],
            customer_id="customer-1",
            paid_cents=10000,
            refund_cents=7500,
            cumulative_refunded_cents=7500,
            request_id="evt-refund-75",
        )
        self.assertEqual(first["agentCommissionRefund"]["orderAmount"], 2500)
        self.assertEqual(first["agentCommissionRefund"]["commissionAmount"], 500)
        self.assertEqual(first["consumerReferralRefund"]["inviterPointsToDebit"], 75)

        out_of_order = growth_service.record_payment_refund(
            self.conn,
            order_id=order["order_id"],
            customer_id="customer-1",
            paid_cents=10000,
            refund_cents=5000,
            cumulative_refunded_cents=5000,
            request_id="evt-refund-50-late",
        )
        self.assertEqual(out_of_order["agentCommissionRefund"]["orderAmount"], 2500)
        self.assertEqual(out_of_order["agentCommissionRefund"]["commissionAmount"], 500)
        self.assertEqual(out_of_order["consumerReferralRefund"]["inviterPointsToDebit"], 0)
        self.assertEqual(out_of_order["consumerReferralRefund"]["cumulativeRefundedCents"], 7500)

        over_refund = growth_service.record_payment_refund(
            self.conn,
            order_id=order["order_id"],
            customer_id="customer-1",
            paid_cents=20000,
            refund_cents=15000,
            cumulative_refunded_cents=15000,
            request_id="evt-refund-over",
        )
        self.assertEqual(over_refund["agentCommissionRefund"]["orderAmount"], 0)
        self.assertEqual(over_refund["agentCommissionRefund"]["commissionAmount"], 0)
        self.assertEqual(over_refund["consumerReferralRefund"]["inviterPointsToDebit"], 25)
        self.assertEqual(over_refund["consumerReferralRefund"]["cumulativeRefundedCents"], 10000)
        self.assertEqual(over_refund["consumerReferralRefund"]["cumulativeInviterPointsDebited"], 100)

        replay = growth_service.record_payment_refund(
            self.conn,
            order_id=order["order_id"],
            customer_id="customer-1",
            paid_cents=20000,
            refund_cents=15000,
            cumulative_refunded_cents=15000,
            request_id="evt-refund-over",
        )
        self.assertEqual(replay["consumerReferralRefund"]["inviterPointsToDebit"], 0)
        self.assertEqual(
            replay["agentCommissionRefund"]["metadata"]["cumulativeRefundedCents"],
            10000,
        )

    def test_separate_refund_deltas_accumulate_without_cumulative_hint(self) -> None:
        agent = growth_service.create_agent_profile(self.conn, "agent-user")
        growth_service.bind_agent_customer(
            self.conn,
            agent_id=agent["id"],
            customer_id="customer-1",
        )
        invite = growth_service.accept_consumer_invite(
            self.conn,
            inviter_user_id="inviter-1",
            invitee_user_id="customer-1",
            phone_verified=True,
            human_verified=True,
        )
        growth_service.mark_invite_reward_granted(self.conn, invite["id"])
        order = self._paid_order(
            "order-1",
            "customer-1",
            amount_cents=10000,
            points=1000,
        )
        growth_service.record_payment_growth(
            self.conn,
            order_id=order["order_id"],
            customer_id="customer-1",
            paid_cents=10000,
            request_id="evt-paid-1",
        )

        first = growth_service.record_payment_refund(
            self.conn,
            order_id=order["order_id"],
            customer_id="customer-1",
            paid_cents=10000,
            refund_cents=2500,
            request_id="evt-refund-delta-1",
        )
        second = growth_service.record_payment_refund(
            self.conn,
            order_id=order["order_id"],
            customer_id="customer-1",
            paid_cents=10000,
            refund_cents=2500,
            request_id="evt-refund-delta-2",
        )

        self.assertEqual(first["agentCommissionRefund"]["orderAmount"], 7500)
        self.assertEqual(second["agentCommissionRefund"]["orderAmount"], 5000)
        self.assertEqual(
            second["agentCommissionRefund"]["metadata"][
                "cumulativeRefundedCents"
            ],
            5000,
        )
        self.assertEqual(
            first["consumerReferralRefund"]["inviterPointsToDebit"],
            25,
        )
        self.assertEqual(
            second["consumerReferralRefund"]["inviterPointsToDebit"],
            25,
        )

    def _paid_order(self, order_id: str, user_id: str, *, amount_cents: int, points: int) -> dict[str, object]:
        order = payment_service.create_payment_order(
            self.conn,
            user_id=user_id,
            amount_cents=amount_cents,
            points=points,
            order_id=order_id,
        )
        payload = {"event_id": f"evt-{order_id}"}
        payload["signature"] = payment_service.fake_callback_signature(
            "fake",
            order["provider_order_id"],
            "pay_success",
            payload,
            SECRET,
        )
        payment_service.handle_payment_callback(
            self.conn,
            "fake",
            order["provider_order_id"],
            "pay_success",
            payload,
            secret=SECRET,
        )
        return order

    def _table_count(self, table: str) -> int:
        return int(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
