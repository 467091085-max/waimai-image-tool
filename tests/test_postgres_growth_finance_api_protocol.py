from __future__ import annotations

import importlib
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

import pytest

from shared import (
    product_finance_store,
    product_growth_outbox,
    product_payment_store,
)
from worker.growth_business_handler import GrowthBusinessHandler
from worker.growth_event_processor import process_growth_events_once


TEST_POSTGRES_DSN = str(os.environ.get("TEST_POSTGRES_DSN") or "").strip()
ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = (
    "001_product_generation_postgres.sql",
    "002_product_wallet_postgres.sql",
    "004_product_payments_postgres.sql",
    "005_product_auth_postgres.sql",
    "006_product_growth_outbox_postgres.sql",
    "007_product_growth_business_postgres.sql",
    "008_product_finance_postgres.sql",
    "011_product_admin_security_postgres.sql",
)


class RegistrationContextStore:
    def registration_session_context(self, **_: Any) -> dict[str, Any]:
        return {
            "phone_verified": True,
            "human_verified": True,
            "same_phone_registered": False,
            "same_device_recent_registrations": 0,
            "same_ip_recent_registrations": 0,
            "risk_blocked": False,
        }


@pytest.mark.skipif(
    not TEST_POSTGRES_DSN,
    reason="TEST_POSTGRES_DSN is required for the real growth HTTP protocol",
)
def test_live_growth_finance_http_protocol_uses_postgres_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if "test" not in TEST_POSTGRES_DSN.lower():
        pytest.skip("TEST_POSTGRES_DSN must target an explicitly named test DB")
    psycopg = pytest.importorskip("psycopg")
    sql = pytest.importorskip("psycopg.sql")
    schema = f"test_growth_http_{uuid4().hex}"
    sqlite_path = tmp_path / "must-not-exist.sqlite3"

    admin = psycopg.connect(TEST_POSTGRES_DSN, autocommit=True)
    try:
        with admin.cursor() as cursor:
            cursor.execute(
                sql.SQL("CREATE SCHEMA {}").format(
                    sql.Identifier(schema)
                )
            )

        def connect_schema() -> Any:
            connection = psycopg.connect(
                TEST_POSTGRES_DSN,
                autocommit=False,
            )
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("SET search_path TO {}").format(
                        sql.Identifier(schema)
                    )
                )
            connection.commit()
            return connection

        connection = connect_schema()
        try:
            with connection.cursor() as cursor:
                for migration in MIGRATIONS:
                    cursor.execute(
                        (ROOT / "migrations" / migration).read_text(
                            encoding="utf-8"
                        )
                    )
            connection.commit()
        finally:
            connection.close()

        @contextmanager
        def postgres_connection_for_test(
            *_: Any,
            **__: Any,
        ) -> Iterator[Any]:
            current = connect_schema()
            try:
                yield current
            except BaseException:
                current.rollback()
                raise
            finally:
                current.close()

        sessions = {
            "agent-token": {
                "id": "session-agent",
                "user_id": "agent-user",
                "user": {"id": "agent-user", "metadata": {}},
            },
            "inviter-token": {
                "id": "session-inviter",
                "user_id": "inviter-user",
                "user": {"id": "inviter-user", "metadata": {}},
            },
            "customer-token": {
                "id": "session-customer",
                "user_id": "customer-user",
                "user": {"id": "customer-user", "metadata": {}},
            },
        }
        monkeypatch.setenv("STORAGE_DB_PATH", str(sqlite_path))
        monkeypatch.setenv(
            "GROWTH_INVITE_CODE_SECRET",
            "growth-http-protocol-secret-" + ("x" * 32),
        )
        monkeypatch.setenv("PRODUCT_GROWTH_TENANT_ID", "waimai")

        app_module = importlib.import_module("app")
        app_module = importlib.reload(app_module)
        monkeypatch.setattr(
            app_module,
            "postgres_product_runtime_enabled",
            lambda: True,
        )
        monkeypatch.setattr(
            app_module,
            "postgres_connection",
            postgres_connection_for_test,
        )
        monkeypatch.setattr(
            app_module.product_auth_store,
            "product_auth_store_from_env",
            lambda _: RegistrationContextStore(),
        )

        def require_session():
            authorization = str(
                app_module.request.headers.get("Authorization") or ""
            )
            token = (
                authorization[7:].strip()
                if authorization.lower().startswith("bearer ")
                else ""
            )
            session = sessions.get(token)
            if session is None:
                return None, (
                    app_module.jsonify(
                        {"error": "auth required", "code": "auth_required"}
                    ),
                    401,
                )
            return {**session, "token": token}, None

        monkeypatch.setattr(
            app_module,
            "require_authenticated_session",
            require_session,
        )
        monkeypatch.setattr(
            app_module,
            "admin_write_authorized",
            lambda: app_module.request.headers.get("X-Test-Admin") == "1",
        )
        monkeypatch.setattr(
            app_module,
            "admin_actor_user_id",
            lambda: "service:test-admin",
        )
        monkeypatch.setattr(
            app_module,
            "admin_finance_action_authorized",
            lambda: app_module.request.headers.get("X-Test-Admin") == "1",
        )
        monkeypatch.setattr(
            app_module,
            "admin_withdrawal_status_authorized",
            lambda _: app_module.request.headers.get("X-Test-Admin") == "1",
        )
        monkeypatch.setattr(
            app_module,
            "admin_commission_settlement_status_authorized",
            lambda _: app_module.request.headers.get("X-Test-Admin") == "1",
        )

        app_module.app.config.update(TESTING=True)
        client = app_module.app.test_client()
        agent_headers = {
            "Authorization": "Bearer agent-token",
            "Idempotency-Key": "agent-create-1",
        }
        admin_headers = {
            "X-Test-Admin": "1",
            "Idempotency-Key": "admin-bind-1",
        }

        agent_response = client.post(
            "/api/growth/agents",
            json={"agentCode": "AGENT001"},
            headers=agent_headers,
        )
        assert agent_response.status_code == 200
        agent = agent_response.get_json()["agent"]

        binding_response = client.post(
            "/api/growth/agent-customers",
            json={
                "agentId": agent["id"],
                "customerId": "customer-user",
                "source": "admin-test",
            },
            headers=admin_headers,
        )
        assert binding_response.status_code == 200

        code_response = client.post(
            "/api/growth/invites/code",
            headers={
                "Authorization": "Bearer inviter-token",
                "Idempotency-Key": "invite-code-1",
            },
        )
        assert code_response.status_code == 200
        invite_code = code_response.get_json()["inviteCode"]
        replayed_code = client.post(
            "/api/growth/invites/code",
            headers={
                "Authorization": "Bearer inviter-token",
                "Idempotency-Key": "invite-code-1",
            },
        )
        assert replayed_code.status_code == 200
        assert replayed_code.get_json()["inviteCode"] == invite_code
        assert replayed_code.get_json()["idempotent"] is True

        accept_response = client.post(
            "/api/growth/invites/accept",
            json={"inviteCode": invite_code},
            headers={
                "Authorization": "Bearer customer-token",
                "Idempotency-Key": "invite-accept-1",
            },
        )
        assert accept_response.status_code == 200
        accepted = accept_response.get_json()
        assert accepted["invite"]["rewardStatus"] == "pending"
        assert accepted["invite"]["registrationRewards"] == {
            "inviterPoints": 100,
            "inviteePoints": 20,
        }

        connection = connect_schema()
        try:
            payment_store = product_payment_store.ProductPaymentStore(
                connection
            )
            order = payment_store.create_or_get_order(
                owner_user_id="customer-user",
                provider="alipay",
                package_id="starter-500",
                idempotency_key="payment-create-1",
                order_id="pay-growth-http-1",
                provider_order_id="provider-growth-http-1",
                provider_payload={"checkout": "protocol"},
            ).order
            payment_store.apply_verified_event(
                owner_user_id="customer-user",
                provider="alipay",
                provider_order_id=str(order["provider_order_id"]),
                provider_event_id="provider-event-growth-http-1",
                event_kind=product_payment_store.EVENT_PAYMENT_SUCCEEDED,
                event_type="TRADE_SUCCESS",
                payload={"tradeNo": "trade-growth-http-1"},
                amount_cents=int(order["amount_cents"]),
            )

            outbox = product_growth_outbox.ProductGrowthOutbox(connection)
            report = process_growth_events_once(
                outbox,
                connection,
                handler=GrowthBusinessHandler(tenant_id="waimai"),
                worker_id="growth-http-worker",
                limit=10,
            )
            assert report["succeeded"] == 2

            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT owner_user_id, balance_points
                    FROM product_point_accounts
                    WHERE owner_user_id IN ('customer-user', 'inviter-user')
                    ORDER BY owner_user_id
                    """
                )
                balances = dict(cursor.fetchall())
                cursor.execute(
                    """
                    SELECT id, status, commission_amount_cents
                    FROM product_commission_orders
                    WHERE source_order_id = %s
                    """,
                    (str(order["id"]),),
                )
                commission = cursor.fetchone()
            assert balances == {
                "customer-user": 520,
                "inviter-user": 149,
            }
            assert commission[1:] == ("pending", 980)

            finance = product_finance_store.ProductFinanceStore(connection)
            finance.record_commission_order(
                commission_order_id="commission-protocol-extra",
                source_order_id="payment-protocol-extra",
                agent_id=str(agent["id"]),
                customer_user_id="customer-user",
                order_amount_cents=100_000,
                commission_amount_cents=20_000,
                commission_rate_bps=2000,
                status="eligible",
                metadata={"source": "protocol-fixture"},
            )
        finally:
            connection.close()

        release_response = client.post(
            "/api/admin/actions/commissions/release-eligible",
            json={"agentId": agent["id"], "minAgeDays": 0},
            headers={
                "X-Test-Admin": "1",
                "Idempotency-Key": "release-growth-http-1",
            },
        )
        assert release_response.status_code == 200

        settlement_response = client.post(
            "/api/admin/actions/commission-settlements",
            json={
                "agentId": agent["id"],
                "commissionOrderIds": [
                    str(commission[0]),
                    "commission-protocol-extra",
                ],
                "settlementAccount": {
                    "type": "bank",
                    "accountName": "Protocol Agent",
                    "accountNoMasked": "6222****8888",
                },
            },
            headers={
                "X-Test-Admin": "1",
                "Idempotency-Key": "settlement-growth-http-1",
            },
        )
        assert settlement_response.status_code == 201, (
            settlement_response.get_data(as_text=True)
        )
        settlement = settlement_response.get_json()["settlement"]
        assert settlement["totalCommissionAmountCents"] == 20_980

        paid_response = client.post(
            f"/api/admin/actions/commission-settlements/{settlement['id']}/status",
            json={"status": "paid", "reason": "protocol transfer"},
            headers={
                "X-Test-Admin": "1",
                "Idempotency-Key": "settlement-paid-growth-http-1",
            },
        )
        assert paid_response.status_code == 200

        balance_response = client.get(
            "/api/growth/withdrawals/balance",
            headers={"Authorization": "Bearer agent-token"},
        )
        assert balance_response.status_code == 200
        assert (
            balance_response.get_json()["balance"]["availableCents"]
            == 20_980
        )

        withdrawal_response = client.post(
            "/api/growth/withdrawals",
            json={
                "amountCents": 10_000,
                "accountSnapshot": {
                    "type": "bank",
                    "accountName": "Protocol Agent",
                    "accountNoMasked": "6222****8888",
                },
            },
            headers={
                "Authorization": "Bearer agent-token",
                "Idempotency-Key": "withdraw-growth-http-1",
            },
        )
        assert withdrawal_response.status_code == 201
        withdrawal = withdrawal_response.get_json()["withdrawal"]
        assert withdrawal["status"] == "pending"

        approved_response = client.post(
            f"/api/admin/actions/withdrawals/{withdrawal['id']}/status",
            json={"status": "approved", "reason": "protocol review"},
            headers={
                "X-Test-Admin": "1",
                "Idempotency-Key": "withdraw-approve-growth-http-1",
            },
        )
        assert approved_response.status_code == 200
        paid_withdrawal_response = client.post(
            f"/api/admin/actions/withdrawals/{withdrawal['id']}/status",
            json={"status": "paid", "reason": "protocol payout"},
            headers={
                "X-Test-Admin": "1",
                "Idempotency-Key": "withdraw-paid-growth-http-1",
            },
        )
        assert paid_withdrawal_response.status_code == 200

        list_response = client.get(
            "/api/growth/withdrawals?status=paid",
            headers={"Authorization": "Bearer agent-token"},
        )
        assert list_response.status_code == 200
        assert [
            item["id"]
            for item in list_response.get_json()["withdrawals"]
        ] == [withdrawal["id"]]

        missing_key_response = client.post(
            "/api/admin/actions/points-adjustments",
            json={
                "userId": "customer-user",
                "direction": "credit",
                "points": 30,
                "reason": "protocol adjustment",
            },
            headers={"X-Test-Admin": "1"},
        )
        assert missing_key_response.status_code == 400
        assert (
            missing_key_response.get_json()["code"]
            == "idempotency_key_required"
        )

        original_record_finance_audit = (
            app_module.product_finance_store.record_finance_audit
        )

        def fail_finance_audit(*_: Any, **__: Any) -> Any:
            raise product_finance_store.ProductFinanceStoreError(
                "protocol audit failure"
            )

        monkeypatch.setattr(
            app_module.product_finance_store,
            "record_finance_audit",
            fail_finance_audit,
        )
        failed_adjustment_response = client.post(
            "/api/admin/actions/points-adjustments",
            json={
                "userId": "customer-user",
                "direction": "credit",
                "points": 30,
                "reason": "must roll back",
            },
            headers={
                "X-Test-Admin": "1",
                "Idempotency-Key": "points-adjustment-audit-failure",
            },
        )
        assert failed_adjustment_response.status_code == 500
        monkeypatch.setattr(
            app_module.product_finance_store,
            "record_finance_audit",
            original_record_finance_audit,
        )

        connection = connect_schema()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT balance_points
                    FROM product_point_accounts
                    WHERE owner_user_id = 'customer-user'
                    """
                )
                assert cursor.fetchone()[0] == 520
                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM product_point_orders
                    WHERE owner_user_id = 'customer-user'
                      AND metadata->'context'->>'reason' = 'must roll back'
                    """
                )
                assert cursor.fetchone()[0] == 0
        finally:
            connection.close()

        adjustment_request = {
            "userId": "customer-user",
            "direction": "credit",
            "points": 30,
            "reason": "protocol adjustment",
            "orderId": "client-order-must-not-be-authoritative",
            "metadata": {"ticketId": "protocol-ticket-1"},
        }
        adjustment_headers = {
            "X-Test-Admin": "1",
            "Idempotency-Key": "points-adjustment-growth-http-1",
        }
        adjustment_response = client.post(
            "/api/admin/actions/points-adjustments",
            json=adjustment_request,
            headers=adjustment_headers,
        )
        assert adjustment_response.status_code == 200, (
            adjustment_response.get_data(as_text=True)
        )
        adjustment = adjustment_response.get_json()
        assert adjustment["transaction"]["idempotent"] is False
        assert adjustment["account"]["balance"] == 550
        assert (
            adjustment["transaction"]["orderId"]
            != "client-order-must-not-be-authoritative"
        )
        assert adjustment["audit"]["actorUserId"] == "service:test-admin"
        assert adjustment["audit"]["action"] == "points.credit.adjusted"

        adjustment_replay_response = client.post(
            "/api/admin/actions/points-adjustments",
            json=adjustment_request,
            headers=adjustment_headers,
        )
        assert adjustment_replay_response.status_code == 200
        adjustment_replay = adjustment_replay_response.get_json()
        assert adjustment_replay["transaction"]["idempotent"] is True
        assert adjustment_replay["account"]["balance"] == 550

        connection = connect_schema()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM product_point_orders
                    WHERE owner_user_id = 'customer-user'
                      AND metadata->>'description' = 'admin-points-credit'
                    """
                )
                point_order_count = cursor.fetchone()[0]
                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM product_finance_audit_events
                    WHERE actor_user_id = 'service:test-admin'
                      AND action = 'points.credit.adjusted'
                    """
                )
                adjustment_audit_count = cursor.fetchone()[0]
            assert point_order_count == 1
            assert adjustment_audit_count == 1
        finally:
            connection.close()

        connection = connect_schema()
        try:
            reconcile_order = (
                product_payment_store.ProductPaymentStore(
                    connection
                ).create_or_get_order(
                    owner_user_id="reconcile-user",
                    provider="alipay",
                    package_id="starter-500",
                    idempotency_key="reconcile-order-create-1",
                    order_id="payment-reconcile-protocol-1",
                    provider_order_id=(
                        "provider-reconcile-protocol-1"
                    ),
                    provider_payload={"checkout": "manual-protocol"},
                ).order
            )
            failure_order = (
                product_payment_store.ProductPaymentStore(
                    connection
                ).create_or_get_order(
                    owner_user_id="reconcile-failure-user",
                    provider="alipay",
                    package_id="starter-500",
                    idempotency_key="reconcile-order-create-2",
                    order_id="payment-reconcile-protocol-2",
                    provider_order_id=(
                        "provider-reconcile-protocol-2"
                    ),
                    provider_payload={"checkout": "manual-protocol"},
                ).order
            )
        finally:
            connection.close()

        reconcile_body = {
            "provider": "alipay",
            "providerOrderId": reconcile_order["provider_order_id"],
            "status": "paid",
            "eventId": "client-event-is-evidence-only",
            "eventType": "manual_paid",
            "reason": "protocol reconciliation",
            "payload": {"total_amount": "49.00"},
            "metadata": {"ticketId": "reconcile-ticket-1"},
        }
        missing_reconcile_key = client.post(
            "/api/admin/actions/payments/reconcile",
            json=reconcile_body,
            headers={"X-Test-Admin": "1"},
        )
        assert missing_reconcile_key.status_code == 400
        assert (
            missing_reconcile_key.get_json()["code"]
            == "idempotency_key_required"
        )
        fake_reconcile = client.post(
            "/api/admin/actions/payments/reconcile",
            json={
                **reconcile_body,
                "provider": "fake",
            },
            headers={
                "X-Test-Admin": "1",
                "Idempotency-Key": "fake-reconcile-live",
            },
        )
        assert fake_reconcile.status_code == 409
        assert (
            fake_reconcile.get_json()["code"]
            == "durable_payment_provider_required"
        )

        reconcile_headers = {
            "X-Test-Admin": "1",
            "Idempotency-Key": "payment-reconcile-protocol-1",
        }
        reconcile_response = client.post(
            "/api/admin/actions/payments/reconcile",
            json=reconcile_body,
            headers=reconcile_headers,
        )
        assert reconcile_response.status_code == 200, (
            reconcile_response.get_data(as_text=True)
        )
        reconciled = reconcile_response.get_json()
        assert reconciled["callback"]["idempotent"] is False
        assert reconciled["account"]["balance"] == 500
        assert reconciled["audit"]["action"] == "payment_reconciled"
        assert reconciled["audit"]["actorUserId"] == "service:test-admin"
        assert (
            reconciled["audit"]["metadata"]["eventId"]
            != "client-event-is-evidence-only"
        )

        reconcile_replay_response = client.post(
            "/api/admin/actions/payments/reconcile",
            json=reconcile_body,
            headers=reconcile_headers,
        )
        assert reconcile_replay_response.status_code == 200
        reconcile_replay = reconcile_replay_response.get_json()
        assert reconcile_replay["callback"]["idempotent"] is True
        assert reconcile_replay["account"]["balance"] == 500

        original_record_finance_audit = (
            app_module.product_finance_store.record_finance_audit
        )

        def fail_reconciliation_audit(*_: Any, **__: Any) -> Any:
            raise product_finance_store.ProductFinanceStoreError(
                "protocol reconciliation audit failure"
            )

        monkeypatch.setattr(
            app_module.product_finance_store,
            "record_finance_audit",
            fail_reconciliation_audit,
        )
        failed_reconciliation = client.post(
            "/api/admin/actions/payments/reconcile",
            json={
                **reconcile_body,
                "providerOrderId": failure_order[
                    "provider_order_id"
                ],
                "eventId": "must-roll-back",
                "reason": "must roll back reconciliation",
            },
            headers={
                "X-Test-Admin": "1",
                "Idempotency-Key": "payment-reconcile-audit-failure",
            },
        )
        assert failed_reconciliation.status_code == 500
        monkeypatch.setattr(
            app_module.product_finance_store,
            "record_finance_audit",
            original_record_finance_audit,
        )

        connection = connect_schema()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT status
                    FROM product_payment_orders
                    WHERE id = %s
                    """,
                    (str(failure_order["id"]),),
                )
                assert cursor.fetchone() == ("pending",)
                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM product_payment_events
                    WHERE order_id = %s
                    """,
                    (str(failure_order["id"]),),
                )
                assert cursor.fetchone()[0] == 0
                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM product_payment_events
                    WHERE order_id = %s
                    """,
                    (str(reconcile_order["id"]),),
                )
                assert cursor.fetchone()[0] == 1
                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM product_finance_audit_events
                    WHERE target_id = %s
                      AND action = 'payment_reconciled'
                    """,
                    (str(reconcile_order["id"]),),
                )
                assert cursor.fetchone()[0] == 1
        finally:
            connection.close()
        assert sqlite_path.exists() is False
    finally:
        with admin.cursor() as cursor:
            cursor.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(schema)
                )
            )
        admin.close()
