from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import billing
import payment_service
import storage_db


PAYMENT_WEBHOOK_SECRET = "growth-api-payment-secret"


def test_growth_api_connects_invite_agent_and_payment_rewards(tmp_path: Path, monkeypatch) -> None:
    storage_db_path = tmp_path / "storage.sqlite3"
    billing_db_path = tmp_path / "billing.sqlite3"
    monkeypatch.setenv("STORAGE_DB_PATH", str(storage_db_path))
    monkeypatch.setenv("BILLING_DB_PATH", str(billing_db_path))
    monkeypatch.setenv("PAYMENT_WEBHOOK_SECRET", PAYMENT_WEBHOOK_SECRET)

    app_module = importlib.import_module("app")
    app_module = importlib.reload(app_module)
    client = app_module.app.test_client()

    agent_response = client.post("/api/growth/agents", json={"userId": "agent-user", "agentCode": "A001"})
    agent_payload = _json_for_status(agent_response, 200)
    agent = agent_payload["agent"]
    assert agent["userId"] == "agent-user"

    bind_response = client.post(
        "/api/growth/agent-customers",
        json={"agentId": agent["id"], "customerId": "customer-user", "source": "invite-code"},
    )
    relation = _json_for_status(bind_response, 200)["relation"]
    assert relation["agentId"] == agent["id"]
    assert relation["customerId"] == "customer-user"

    invite_response = client.post(
        "/api/growth/invites/accept",
        json={
            "inviterUserId": "inviter-user",
            "inviteeUserId": "customer-user",
            "phoneVerified": True,
            "humanVerified": True,
        },
    )
    invite_payload = _json_for_status(invite_response, 200)
    assert invite_payload["invite"]["rewardStatus"] == "granted"
    assert billing.get_account("inviter-user", db_path=billing_db_path)["balance"] == 50
    assert billing.get_account("customer-user", db_path=billing_db_path)["balance"] == 50

    order_response = client.post(
        "/api/payments/orders",
        json={"userId": "customer-user", "cash": 49, "idempotencyKey": "growth-pay-49"},
    )
    order = _json_for_status(order_response, 200)["order"]
    event_payload = {"eventId": "evt-growth-pay"}
    event_payload["signature"] = payment_service.fake_callback_signature(
        "fake",
        order["providerOrderId"],
        "pay_success",
        event_payload,
        PAYMENT_WEBHOOK_SECRET,
    )
    callback_response = client.post(
        "/api/payments/fake-callback",
        json={
            "providerOrderId": order["providerOrderId"],
            "eventType": "pay_success",
            "payload": event_payload,
        },
    )
    callback_payload = _json_for_status(callback_response, 200)

    growth = callback_payload["growth"]
    assert growth["agentCommission"]["agentId"] == agent["id"]
    assert growth["agentCommission"]["commissionAmount"] == 980
    assert growth["consumerReferralReward"]["inviterUserId"] == "inviter-user"
    assert growth["consumerReferralReward"]["inviterPoints"] == 49
    assert billing.get_account("inviter-user", db_path=billing_db_path)["balance"] == 99
    assert billing.get_account("customer-user", db_path=billing_db_path)["balance"] == 550

    duplicate_response = client.post(
        "/api/payments/fake-callback",
        json={
            "providerOrderId": order["providerOrderId"],
            "eventType": "pay_success",
            "payload": event_payload,
        },
    )
    duplicate_payload = _json_for_status(duplicate_response, 200)
    assert duplicate_payload["growth"]["agentCommission"]["idempotent"] is True
    assert duplicate_payload["growth"]["consumerReferralReward"]["inviterPoints"] == 0
    assert billing.get_account("inviter-user", db_path=billing_db_path)["balance"] == 99

    refund_payload = {"eventId": "evt-growth-refund"}
    refund_payload["signature"] = payment_service.fake_callback_signature(
        "fake",
        order["providerOrderId"],
        "refund_success",
        refund_payload,
        PAYMENT_WEBHOOK_SECRET,
    )
    refund_response = client.post(
        "/api/payments/fake-callback",
        json={
            "providerOrderId": order["providerOrderId"],
            "eventType": "refund_success",
            "payload": refund_payload,
        },
    )
    refund_callback = _json_for_status(refund_response, 200)
    refund_growth = refund_callback["growth"]
    assert refund_growth["agentCommissionRefund"]["status"] == "refunded"
    assert refund_growth["agentCommissionRefund"]["commissionAmount"] == 0
    assert refund_growth["consumerReferralRefund"]["inviterPointsToDebit"] == 49
    assert billing.get_account("inviter-user", db_path=billing_db_path)["balance"] == 50
    assert billing.get_account("customer-user", db_path=billing_db_path)["balance"] == 50

    conn = storage_db.get_conn(storage_db_path)
    try:
        row = conn.execute("SELECT status, commission_amount FROM commission_orders WHERE order_id = ?", (order["orderId"],)).fetchone()
        assert row["status"] == "refunded"
        assert row["commission_amount"] == 0
    finally:
        conn.close()


def _json_for_status(response: Any, expected_status: int) -> dict[str, Any]:
    data = response.get_json(silent=True)
    assert response.status_code == expected_status, response.get_data(as_text=True)
    assert isinstance(data, dict), response.get_data(as_text=True)
    return data
