from __future__ import annotations

import importlib
import sqlite3
from pathlib import Path
from typing import Any

import storage_db


OLD = "2026-06-18T00:00:00+00:00"
NOW = "2026-06-28T00:00:00+00:00"


def test_admin_commission_settlement_api_releases_batches_and_marks_paid(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "storage.sqlite3"
    monkeypatch.setenv("STORAGE_DB_PATH", str(db_path))
    app_module = importlib.import_module("app")
    app_module = importlib.reload(app_module)
    client = app_module.app.test_client()
    _seed_commission(db_path)

    release_response = client.post(
        "/api/admin/actions/commissions/release-eligible",
        json={"agentId": "agent_1", "minAgeDays": 7, "now": NOW},
    )
    release_payload = _json_for_status(release_response, 200)
    assert release_payload["released"] == 2
    assert release_payload["commissionAmount"] == 3000

    create_response = client.post(
        "/api/admin/actions/commission-settlements",
        json={"agentId": "agent_1", "periodStart": "2026-06-01", "periodEnd": "2026-06-30"},
    )
    settlement = _json_for_status(create_response, 200)["settlement"]
    assert settlement["status"] == "pending"
    assert settlement["totalCommissionAmount"] == 3000
    assert settlement["orderCount"] == 2

    list_response = client.get("/api/admin/actions/commission-settlements", query_string={"agentId": "agent_1"})
    settlements = _json_for_status(list_response, 200)["settlements"]
    assert [item["id"] for item in settlements] == [settlement["id"]]

    paid_response = client.post(
        f"/api/admin/actions/commission-settlements/{settlement['id']}/status",
        json={"status": "paid", "paidAt": "2026-06-29T00:00:00+00:00"},
    )
    paid = _json_for_status(paid_response, 200)["settlement"]
    assert paid["status"] == "paid"
    assert paid["paidAt"] == "2026-06-29T00:00:00+00:00"

    conn = storage_db.get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT id, status, settlement_id, settled_at FROM commission_orders ORDER BY id"
        ).fetchall()
        assert [(row["id"], row["status"], row["settlement_id"]) for row in rows] == [
            ("co_1", "settled", settlement["id"]),
            ("co_2", "settled", settlement["id"]),
        ]
        assert all(row["settled_at"] == "2026-06-29T00:00:00+00:00" for row in rows)
    finally:
        conn.close()


def _seed_commission(db_path: Path) -> None:
    conn = storage_db.init_db(db_path)
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO agent_profiles (id, user_id, agent_code, status, created_at, updated_at)
                VALUES ('agent_1', 'agent_user_1', 'A001', 'active', ?, ?)
                """,
                (OLD, OLD),
            )
            for index, amount in enumerate((2000, 1000), start=1):
                conn.execute(
                    """
                    INSERT INTO commission_orders (
                        id, order_id, agent_id, customer_id, relation_id, order_amount,
                        commission_amount, commission_rate_bps, status, created_at, updated_at
                    )
                    VALUES (?, ?, 'agent_1', ?, ?, ?, ?, 2000, 'pending', ?, ?)
                    """,
                    (
                        f"co_{index}",
                        f"order_{index}",
                        f"customer_{index}",
                        f"relation_{index}",
                        amount * 5,
                        amount,
                        OLD,
                        OLD,
                    ),
                )
    finally:
        conn.close()


def _json_for_status(response: Any, expected_status: int) -> dict[str, Any]:
    data = response.get_json(silent=True)
    assert response.status_code == expected_status, response.get_data(as_text=True)
    assert isinstance(data, dict), response.get_data(as_text=True)
    return data
