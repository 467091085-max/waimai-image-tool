from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from typing import Any

from shared.product_job_store import (
    CancellationRequested,
    FenceMismatch,
    InsufficientPointBalance,
    InvalidProductJobInput,
    JobNotFound,
    JobStateConflict,
    OutboxClaimLost,
    PointOrderConflict,
    ProductJobStore,
    RequestDigestConflict,
    RevisionJobCandidate,
    ResultWriteConflict,
    SettlementConflict,
    WalletIntegrityError,
)

ROOT = Path(__file__).resolve().parents[1]
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_C = "c" * 64


class ScriptedCursor:
    def __init__(self, connection: "ScriptedConnection") -> None:
        self.connection = connection
        self.description = None
        self.rowcount = -1
        self.rows: list[dict[str, Any]] = []
        self.closed = False

    def execute(self, operation: str, parameters: tuple[Any, ...] = ()) -> None:
        match = re.search(r"/\* product_job_store:([a-z_]+) \*/", operation)
        if match is None:
            raise AssertionError(f"SQL operation has no test marker: {operation}")
        name = match.group(1)
        placeholder_count = operation.count("%s")
        if placeholder_count != len(parameters):
            raise AssertionError(
                f"{name} expected {placeholder_count} SQL parameters, "
                f"received {len(parameters)}"
            )
        self.connection.calls.append((name, operation, tuple(parameters)))
        responses = self.connection.responses.get(name)
        if not responses:
            raise AssertionError(f"unexpected or exhausted SQL operation: {name}")
        response = responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        self.rows = list(response)
        self.rowcount = len(self.rows)

    def fetchone(self) -> dict[str, Any] | None:
        if not self.rows:
            return None
        return self.rows.pop(0)

    def fetchall(self) -> list[dict[str, Any]]:
        rows = list(self.rows)
        self.rows.clear()
        return rows

    def close(self) -> None:
        self.closed = True


class ScriptedConnection:
    def __init__(self, **responses: list[Any]) -> None:
        self.responses = {name: list(items) for name, items in responses.items()}
        self.calls: list[tuple[str, str, tuple[Any, ...]]] = []
        self.commits = 0
        self.rollbacks = 0
        self.cursors: list[ScriptedCursor] = []

    def cursor(self) -> ScriptedCursor:
        cursor = ScriptedCursor(self)
        self.cursors.append(cursor)
        return cursor

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class AutocommitConnection(ScriptedConnection):
    autocommit = True


def job_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": "job-1",
        "owner_user_id": "user-1",
        "idempotency_key": "idem-1",
        "request_sha256": DIGEST_A,
        "status": "queued",
        "version": 0,
        "fence": 0,
        "cancel_requested": False,
        "requested_count": 3,
        "completed_count": 0,
        "failed_count": 0,
        "manifest_ref": None,
        "manifest_sha256": None,
        "debit_order_id": "debit-job-1",
        "debit_points": 30,
        "refund_order_id": "refund-job-1",
        "refund_target_points": 0,
        "error_message": "",
        "created_at": "2026-07-30T00:00:00Z",
        "updated_at": "2026-07-30T00:00:00Z",
        "started_at": None,
        "completed_at": None,
    }
    row.update(overrides)
    return row


def settlement_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "job_id": "job-1",
        "owner_user_id": "user-1",
        "status": "pending",
        "version": 0,
        "debit_order_id": "debit-job-1",
        "debit_points": 30,
        "refund_order_id": "refund-job-1",
        "refund_target_points": 0,
        "refund_applied_points": 0,
        "claim_token": None,
        "claimed_by": None,
        "claimed_at": None,
        "applied_at": None,
        "finalized_at": None,
        "provider_reference": "",
        "error_message": "",
        "created_at": "2026-07-30T00:00:00Z",
        "updated_at": "2026-07-30T00:00:00Z",
    }
    row.update(overrides)
    return row


def account_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "owner_user_id": "user-1",
        "balance_points": 100,
        "lifetime_credited_points": 100,
        "lifetime_debited_points": 0,
        "lifetime_refunded_points": 0,
        "version": 1,
        "metadata": {"source": "registration"},
        "created_at": "2026-07-30T00:00:00Z",
        "updated_at": "2026-07-30T00:00:00Z",
    }
    row.update(overrides)
    return row


def point_order_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": "credit-1",
        "owner_user_id": "user-1",
        "order_kind": "credit",
        "points": 20,
        "source_order_id": None,
        "source_order_kind": None,
        "job_id": None,
        "request_sha256": DIGEST_A,
        "metadata": {"paymentId": "payment-1"},
        "applied_at": "2026-07-30T00:01:00Z",
        "created_at": "2026-07-30T00:01:00Z",
        "updated_at": "2026-07-30T00:01:00Z",
    }
    row.update(overrides)
    return row


def atomic_integrity_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "job_id": "job-1",
        "owner_user_id": "user-1",
        "request_sha256": DIGEST_A,
        "debit_order_id": "debit-job-1",
        "debit_points": 30,
        "wallet_debit_order_id": "debit-job-1",
        "wallet_owner_user_id": "user-1",
        "wallet_order_kind": "debit",
        "wallet_order_points": 30,
        "wallet_request_sha256": DIGEST_A,
        "wallet_job_id": "job-1",
        "settlement_job_id": "job-1",
        "outbox_id": "outbox-1",
        "outbox_event_type": "product_generation.requested",
    }
    row.update(overrides)
    return row


def result_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": 1,
        "job_id": "job-1",
        "job_fence": 1,
        "menu_row": 7,
        "platform": "meituan",
        "variant": "default",
        "status": "generated",
        "object_ref": "private/results/job-1/7-meituan.png",
        "object_sha256": DIGEST_B,
        "prompt_sha256": DIGEST_C,
        "provider_request_id": "provider-1",
        "metadata": {"width": 800, "height": 600},
        "error_message": "",
        "created_at": "2026-07-30T00:03:00Z",
        "updated_at": "2026-07-30T00:03:00Z",
    }
    row.update(overrides)
    return row


def result_context(
    *,
    current_fence: int = 1,
    job_status: str = "running",
    cancel_requested: bool = False,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    context = {
        "current_job_id": "job-1",
        "current_job_status": job_status,
        "current_job_fence": current_fence,
        "current_cancel_requested": cancel_requested,
        "id": None,
    }
    if result is not None:
        context.update(result)
    return context


def create_job(store: ProductJobStore, *, digest: str = DIGEST_A):
    return store.create_or_get_job(
        owner_user_id="user-1",
        idempotency_key="idem-1",
        request_sha256=digest,
        request_payload={"quality": "standard", "platforms": ["meituan"]},
        menu_upload_id="menu-1",
        menu_object_ref="private/menus/menu-1.xlsx",
        menu_object_sha256=DIGEST_B,
        selected_background_ref="private/backgrounds/bg-1.png",
        selected_background_sha256=DIGEST_C,
        requested_count=3,
        debit_order_id="debit-job-1",
        debit_points=30,
        refund_order_id="refund-job-1",
        job_id="job-1",
        outbox_id="outbox-1",
    )


def create_atomic_job(
    store: ProductJobStore,
    *,
    digest: str = DIGEST_A,
    debit_points: int = 30,
):
    return store.create_or_get_job_with_debit(
        owner_user_id="user-1",
        idempotency_key="idem-1",
        request_sha256=digest,
        request_payload={
            "quality": "standard",
            "platforms": ["meituan"],
            "browserPoints": 999999,
        },
        menu_upload_id="menu-1",
        menu_object_ref="private/menus/menu-1.xlsx",
        menu_object_sha256=DIGEST_B,
        selected_background_ref="private/backgrounds/bg-1.png",
        selected_background_sha256=DIGEST_C,
        requested_count=3,
        debit_order_id="debit-job-1",
        debit_points=debit_points,
        refund_order_id="refund-job-1",
        job_id="job-1",
        outbox_id="outbox-1",
        debit_metadata={"priceRule": "standard-v1"},
    )


def parent_generation_job(**overrides: Any) -> dict[str, Any]:
    values = {
        "id": "parent-job-1",
        "owner_user_id": "user-1",
        "idempotency_key": "parent-idem-1",
        "request_sha256": DIGEST_A,
        "request_payload": {
            "jobType": "menu_batch_generation",
            "jobId": "parent-job-1",
            "userId": "user-1",
        },
        "menu_upload_id": "menu-1",
        "menu_object_ref": "private/menus/menu-1.xlsx",
        "menu_object_sha256": DIGEST_B,
        "selected_background_ref": "private/backgrounds/bg-1.png",
        "selected_background_sha256": DIGEST_C,
        "status": "succeeded",
    }
    values.update(overrides)
    return job_row(**values)


def revision_candidate(
    *,
    free: bool,
    digest: str,
    job_id: str = "revision-job-1",
) -> RevisionJobCandidate:
    points = 0 if free else 10
    request_payload = {
        "schemaVersion": 1,
        "jobType": "delivery_asset_revision_batch",
        "jobId": job_id,
        "parentGenerationJobId": "parent-job-1",
        "userId": "user-1",
        "mode": "rework",
        "selectedBackground": {
            "objectKey": "private/backgrounds/bg-1.png",
            "sha256": DIGEST_C,
        },
        "billing": {
            "totalPoints": points,
            "freeReworkQuotaVerified": free,
            "debitOrderId": f"revision:{job_id}:debit",
            "refundOrderId": f"revision:{job_id}:refund",
        },
        "idempotency": {
            "key": "revision-idem-1",
            "requestSha256": digest,
        },
    }
    return RevisionJobCandidate(
        request_sha256=digest,
        request_payload=request_payload,
        menu_upload_id="menu-1",
        menu_object_ref="private/menus/menu-1.xlsx",
        menu_object_sha256=DIGEST_B,
        selected_background_ref="private/backgrounds/bg-1.png",
        selected_background_sha256=DIGEST_C,
        requested_count=1,
        debit_order_id=f"revision:{job_id}:debit",
        debit_points=points,
        refund_order_id=f"revision:{job_id}:refund",
        job_id=job_id,
        outbox_id=f"outbox:{job_id}",
        account_metadata={"source": "customer_revision"},
        debit_metadata={"parentGenerationJobId": "parent-job-1"},
    )


def persisted_revision_job(
    *,
    free: bool,
    digest: str,
) -> dict[str, Any]:
    candidate = revision_candidate(free=free, digest=digest)
    return job_row(
        id=candidate.job_id,
        owner_user_id="user-1",
        idempotency_key="revision-idem-1",
        request_sha256=digest,
        request_payload=dict(candidate.request_payload),
        menu_upload_id=candidate.menu_upload_id,
        menu_object_ref=candidate.menu_object_ref,
        menu_object_sha256=candidate.menu_object_sha256,
        selected_background_ref=candidate.selected_background_ref,
        selected_background_sha256=candidate.selected_background_sha256,
        requested_count=1,
        debit_order_id=candidate.debit_order_id,
        debit_points=candidate.debit_points,
        refund_order_id=candidate.refund_order_id,
    )


def revision_integrity_row(
    *,
    free: bool,
    digest: str,
) -> dict[str, Any]:
    candidate = revision_candidate(free=free, digest=digest)
    return atomic_integrity_row(
        job_id=candidate.job_id,
        owner_user_id="user-1",
        request_sha256=digest,
        debit_order_id=candidate.debit_order_id,
        debit_points=candidate.debit_points,
        wallet_debit_order_id=(
            None if free else candidate.debit_order_id
        ),
        wallet_owner_user_id=None if free else "user-1",
        wallet_order_kind=None if free else "debit",
        wallet_order_points=None if free else candidate.debit_points,
        wallet_request_sha256=None if free else digest,
        wallet_job_id=None if free else candidate.job_id,
        settlement_job_id=candidate.job_id,
        outbox_id=candidate.outbox_id,
    )


def create_revision_with_quota(
    store: ProductJobStore,
    *,
    free_limit: int = 1,
):
    return store.create_or_get_revision_job_with_quota(
        owner_user_id="user-1",
        parent_generation_job_id="parent-job-1",
        revision_job_type="delivery_asset_revision_batch",
        idempotency_key="revision-idem-1",
        free_rework_limit=free_limit,
        free_candidate=revision_candidate(free=True, digest=DIGEST_A),
        paid_candidate=revision_candidate(free=False, digest=DIGEST_B),
    )


def write_result(
    store: ProductJobStore,
    *,
    fence: int = 1,
    object_sha256: str = DIGEST_B,
):
    return store.upsert_result(
        job_id="job-1",
        fence=fence,
        menu_row=7,
        platform="Meituan",
        variant="Default",
        status="generated",
        object_ref="private/results/job-1/7-meituan.png",
        object_sha256=object_sha256,
        prompt_sha256=DIGEST_C,
        provider_request_id="provider-1",
        metadata={"width": 800, "height": 600},
    )


class ProductJobStoreTests(unittest.TestCase):
    def test_store_rejects_autocommit_connection(self) -> None:
        with self.assertRaisesRegex(InvalidProductJobInput, "autocommit-disabled"):
            ProductJobStore(AutocommitConnection())

    def test_account_get_or_create_and_get_have_explicit_semantics(self) -> None:
        created_connection = ScriptedConnection(
            create_point_account=[[account_row(balance_points=0)]],
        )
        created = ProductJobStore(created_connection).get_or_create_account(
            owner_user_id="user-1",
            metadata={"source": "registration"},
        )
        self.assertTrue(created.created)
        self.assertEqual(created.account["balance_points"], 0)

        existing_connection = ScriptedConnection(
            create_point_account=[[]],
            select_point_account=[[account_row()]],
        )
        existing = ProductJobStore(existing_connection).get_or_create_account(
            owner_user_id="user-1",
            metadata={"source": "ignored-on-replay"},
        )
        self.assertFalse(existing.created)
        self.assertEqual(existing.account["metadata"], {"source": "registration"})

        missing_connection = ScriptedConnection(select_point_account=[[]])
        self.assertIsNone(
            ProductJobStore(missing_connection).get_account(
                owner_user_id="missing-user",
            )
        )
        self.assertEqual(
            [call[0] for call in missing_connection.calls],
            ["select_point_account"],
        )

    def test_credit_points_is_atomic_and_same_content_replay_is_idempotent(
        self,
    ) -> None:
        metadata = {"paymentId": "payment-1"}
        inserted_order = point_order_row(metadata=metadata)
        connection = ScriptedConnection(
            create_point_account=[[]],
            select_point_account_for_update=[[account_row()]],
            select_point_order=[[]],
            create_point_order=[[inserted_order]],
            credit_point_account=[
                [
                    account_row(
                        balance_points=120,
                        lifetime_credited_points=120,
                        version=2,
                    )
                ]
            ],
            create_point_ledger=[[]],
        )

        credited = ProductJobStore(connection).credit_points(
            owner_user_id="user-1",
            order_id="credit-1",
            points=20,
            request_sha256=DIGEST_A,
            metadata=metadata,
        )

        self.assertFalse(credited.idempotent)
        self.assertEqual(credited.account["balance_points"], 120)
        self.assertEqual(connection.commits, 1)
        self.assertEqual(
            [call[0] for call in connection.calls],
            [
                "create_point_account",
                "select_point_account_for_update",
                "select_point_order",
                "create_point_order",
                "credit_point_account",
                "create_point_ledger",
            ],
        )
        self.assertIn("FOR UPDATE", connection.calls[1][1])

        replay_connection = ScriptedConnection(
            create_point_account=[[]],
            select_point_account_for_update=[
                [account_row(balance_points=120)]
            ],
            select_point_order=[[inserted_order]],
        )
        replay = ProductJobStore(replay_connection).credit_points(
            owner_user_id="user-1",
            order_id="credit-1",
            points=20,
            request_sha256=DIGEST_A,
            metadata=metadata,
        )
        self.assertTrue(replay.idempotent)
        self.assertEqual(
            [call[0] for call in replay_connection.calls],
            [
                "create_point_account",
                "select_point_account_for_update",
                "select_point_order",
            ],
        )

    def test_credit_order_content_conflict_and_sensitive_metadata_fail_closed(
        self,
    ) -> None:
        connection = ScriptedConnection(
            create_point_account=[[]],
            select_point_account_for_update=[[account_row()]],
            select_point_order=[[point_order_row(points=20)]],
        )
        with self.assertRaises(PointOrderConflict):
            ProductJobStore(connection).credit_points(
                owner_user_id="user-1",
                order_id="credit-1",
                points=21,
                request_sha256=DIGEST_A,
                metadata={"paymentId": "payment-1"},
            )
        self.assertEqual(connection.rollbacks, 1)

        with self.assertRaisesRegex(
            InvalidProductJobInput,
            "credentials or tokens",
        ):
            ProductJobStore(ScriptedConnection()).credit_points(
                owner_user_id="user-1",
                order_id="credit-2",
                points=10,
                request_sha256=DIGEST_A,
                metadata={"apiKey": "must-not-be-stored"},
            )

    def test_point_transaction_effect_is_atomic_and_runs_on_replay(
        self,
    ) -> None:
        metadata = {"source": "admin-adjustment"}
        inserted_order = point_order_row(metadata=metadata)
        connection = ScriptedConnection(
            create_point_account=[[]],
            select_point_account_for_update=[[account_row()]],
            select_point_order=[[]],
            create_point_order=[[inserted_order]],
            credit_point_account=[
                [
                    account_row(
                        balance_points=120,
                        lifetime_credited_points=120,
                        version=2,
                    )
                ]
            ],
            create_point_ledger=[[]],
        )
        effects: list[tuple[int, bool]] = []

        ProductJobStore(connection).credit_points(
            owner_user_id="user-1",
            order_id="credit-1",
            points=20,
            request_sha256=DIGEST_A,
            metadata=metadata,
            transaction_effect=lambda _, result: effects.append(
                (
                    int(result.account["balance_points"]),
                    result.idempotent,
                )
            ),
        )

        self.assertEqual(effects, [(120, False)])
        self.assertEqual(connection.commits, 1)

        replay_connection = ScriptedConnection(
            create_point_account=[[]],
            select_point_account_for_update=[
                [account_row(balance_points=120)]
            ],
            select_point_order=[[inserted_order]],
        )
        ProductJobStore(replay_connection).credit_points(
            owner_user_id="user-1",
            order_id="credit-1",
            points=20,
            request_sha256=DIGEST_A,
            metadata=metadata,
            transaction_effect=lambda _, result: effects.append(
                (
                    int(result.account["balance_points"]),
                    result.idempotent,
                )
            ),
        )
        self.assertEqual(effects, [(120, False), (120, True)])
        self.assertEqual(replay_connection.commits, 1)

        failed_connection = ScriptedConnection(
            create_point_account=[[]],
            select_point_account_for_update=[[account_row()]],
            select_point_order=[[]],
            create_point_order=[[inserted_order]],
            credit_point_account=[
                [
                    account_row(
                        balance_points=120,
                        lifetime_credited_points=120,
                        version=2,
                    )
                ]
            ],
            create_point_ledger=[[]],
        )

        def fail_effect(*_: Any) -> None:
            raise RuntimeError("audit unavailable")

        with self.assertRaisesRegex(RuntimeError, "audit unavailable"):
            ProductJobStore(failed_connection).credit_points(
                owner_user_id="user-1",
                order_id="credit-1",
                points=20,
                request_sha256=DIGEST_A,
                metadata=metadata,
                transaction_effect=fail_effect,
            )
        self.assertEqual(failed_connection.commits, 0)
        self.assertEqual(failed_connection.rollbacks, 1)

    def test_non_job_debit_is_atomic_idempotent_and_balance_bounded(
        self,
    ) -> None:
        metadata = {"paymentId": "payment-1", "purpose": "payment-refund"}
        debit_order = point_order_row(
            id="payment-refund-1",
            order_kind="debit",
            points=25,
            job_id=None,
            metadata=metadata,
        )
        connection = ScriptedConnection(
            create_point_account=[[]],
            select_point_account_for_update=[[account_row()]],
            select_point_order=[[]],
            create_point_order=[[debit_order]],
            debit_point_account=[
                [
                    account_row(
                        balance_points=75,
                        lifetime_debited_points=25,
                        version=2,
                    )
                ]
            ],
            create_point_ledger=[[]],
        )

        result = ProductJobStore(connection).debit_points(
            owner_user_id="user-1",
            order_id="payment-refund-1",
            points=25,
            request_sha256=DIGEST_A,
            metadata=metadata,
        )

        self.assertFalse(result.idempotent)
        self.assertEqual(result.account["balance_points"], 75)
        self.assertIsNone(result.order["job_id"])
        self.assertEqual(connection.commits, 1)

        replay_connection = ScriptedConnection(
            create_point_account=[[]],
            select_point_account_for_update=[
                [account_row(balance_points=75)]
            ],
            select_point_order=[[debit_order]],
        )
        replay = ProductJobStore(replay_connection).debit_points(
            owner_user_id="user-1",
            order_id="payment-refund-1",
            points=25,
            request_sha256=DIGEST_A,
            metadata=metadata,
        )
        self.assertTrue(replay.idempotent)
        self.assertEqual(replay.account["balance_points"], 75)

        insufficient = ScriptedConnection(
            create_point_account=[[]],
            select_point_account_for_update=[
                [account_row(balance_points=10)]
            ],
            select_point_order=[[]],
        )
        with self.assertRaises(InsufficientPointBalance):
            ProductJobStore(insufficient).debit_points(
                owner_user_id="user-1",
                order_id="payment-refund-2",
                points=25,
                request_sha256=DIGEST_B,
                metadata=metadata,
            )
        self.assertEqual(insufficient.rollbacks, 1)

    def test_atomic_job_debit_settlement_and_outbox_share_one_transaction(
        self,
    ) -> None:
        connection = ScriptedConnection(
            create_point_account=[[]],
            select_point_account_for_update=[[account_row()]],
            select_job_by_request_for_update=[[]],
            create_job=[[job_row()]],
            create_point_order=[
                [
                    point_order_row(
                        id="debit-job-1",
                        order_kind="debit",
                        points=30,
                        job_id="job-1",
                        metadata={
                            "jobId": "job-1",
                            "purpose": "product_generation",
                            "context": {"priceRule": "standard-v1"},
                        },
                    )
                ]
            ],
            debit_point_account=[
                [
                    account_row(
                        balance_points=70,
                        lifetime_debited_points=30,
                        version=2,
                    )
                ]
            ],
            create_point_ledger=[[]],
            create_settlement=[[]],
            create_outbox=[[]],
        )

        result = create_atomic_job(ProductJobStore(connection))

        self.assertTrue(result.created)
        self.assertEqual(connection.commits, 1)
        self.assertEqual(connection.rollbacks, 0)
        self.assertEqual(
            [call[0] for call in connection.calls],
            [
                "create_point_account",
                "select_point_account_for_update",
                "select_job_by_request_for_update",
                "create_job",
                "create_point_order",
                "debit_point_account",
                "create_point_ledger",
                "create_settlement",
                "create_outbox",
            ],
        )
        self.assertIn("FOR UPDATE", connection.calls[1][1])
        outbox_payload = json.loads(connection.calls[-1][2][2])
        self.assertEqual(
            outbox_payload,
            {
                "schemaVersion": 1,
                "jobId": "job-1",
                "ownerUserId": "user-1",
                "requestSha256": DIGEST_A,
            },
        )
        self.assertNotIn("browserPoints", outbox_payload)
        self.assertNotIn("request", outbox_payload)

    def test_atomic_job_replay_does_not_debit_again(self) -> None:
        connection = ScriptedConnection(
            create_point_account=[[]],
            select_point_account_for_update=[
                [account_row(balance_points=70)]
            ],
            select_job_by_request_for_update=[[job_row()]],
            select_atomic_job_integrity=[[atomic_integrity_row()]],
        )

        replay = create_atomic_job(ProductJobStore(connection))

        self.assertFalse(replay.created)
        self.assertEqual(replay.job["id"], "job-1")
        self.assertEqual(
            [call[0] for call in connection.calls],
            [
                "create_point_account",
                "select_point_account_for_update",
                "select_job_by_request_for_update",
                "select_atomic_job_integrity",
            ],
        )
        self.assertNotIn(
            "debit_point_account",
            [call[0] for call in connection.calls],
        )

    def test_atomic_job_digest_conflict_and_insufficient_balance_roll_back(
        self,
    ) -> None:
        digest_conflict = ScriptedConnection(
            create_point_account=[[]],
            select_point_account_for_update=[[account_row()]],
            select_job_by_request_for_update=[
                [job_row(request_sha256=DIGEST_B)]
            ],
        )
        with self.assertRaises(RequestDigestConflict):
            create_atomic_job(ProductJobStore(digest_conflict))
        self.assertEqual(digest_conflict.rollbacks, 1)

        insufficient = ScriptedConnection(
            create_point_account=[[]],
            select_point_account_for_update=[
                [account_row(balance_points=5)]
            ],
            select_job_by_request_for_update=[[]],
        )
        with self.assertRaises(InsufficientPointBalance) as raised:
            create_atomic_job(ProductJobStore(insufficient))
        self.assertEqual(raised.exception.available_points, 5)
        self.assertEqual(raised.exception.required_points, 30)
        self.assertEqual(insufficient.commits, 0)
        self.assertEqual(insufficient.rollbacks, 1)
        self.assertNotIn(
            "create_job",
            [call[0] for call in insufficient.calls],
        )

    def test_atomic_job_order_or_outbox_failure_rolls_back_everything(
        self,
    ) -> None:
        order_conflict = ScriptedConnection(
            create_point_account=[[]],
            select_point_account_for_update=[[account_row()]],
            select_job_by_request_for_update=[[]],
            create_job=[[job_row()]],
            create_point_order=[[]],
        )
        with self.assertRaises(PointOrderConflict):
            create_atomic_job(ProductJobStore(order_conflict))
        self.assertEqual(order_conflict.commits, 0)
        self.assertEqual(order_conflict.rollbacks, 1)

        outbox_failure = ScriptedConnection(
            create_point_account=[[]],
            select_point_account_for_update=[[account_row()]],
            select_job_by_request_for_update=[[]],
            create_job=[[job_row()]],
            create_point_order=[
                [
                    point_order_row(
                        id="debit-job-1",
                        order_kind="debit",
                        points=30,
                        job_id="job-1",
                    )
                ]
            ],
            debit_point_account=[[account_row(balance_points=70)]],
            create_point_ledger=[[]],
            create_settlement=[[]],
            create_outbox=[RuntimeError("outbox unavailable")],
        )
        with self.assertRaisesRegex(RuntimeError, "outbox unavailable"):
            create_atomic_job(ProductJobStore(outbox_failure))
        self.assertEqual(outbox_failure.commits, 0)
        self.assertEqual(outbox_failure.rollbacks, 1)
        self.assertIn(
            "debit_point_account",
            [call[0] for call in outbox_failure.calls],
        )

    def test_atomic_job_replay_detects_missing_wallet_or_outbox_state(self) -> None:
        connection = ScriptedConnection(
            create_point_account=[[]],
            select_point_account_for_update=[[account_row()]],
            select_job_by_request_for_update=[[job_row()]],
            select_atomic_job_integrity=[
                [atomic_integrity_row(outbox_id=None)]
            ],
        )
        with self.assertRaises(WalletIntegrityError):
            create_atomic_job(ProductJobStore(connection))
        self.assertEqual(connection.rollbacks, 1)

    def test_revision_quota_free_candidate_is_created_under_parent_lock(
        self,
    ) -> None:
        connection = ScriptedConnection(
            select_owned_parent_job_for_update=[[parent_generation_job()]],
            select_private_job_by_request_for_update=[[]],
            count_active_free_reworks=[
                [{"active_free_rework_count": 0}]
            ],
            create_job=[
                [persisted_revision_job(free=True, digest=DIGEST_A)]
            ],
            create_settlement=[[]],
            create_outbox=[[]],
        )

        result = create_revision_with_quota(ProductJobStore(connection))

        self.assertTrue(result.created)
        self.assertEqual(result.selected_candidate, "free")
        self.assertEqual(
            result.request_payload["billing"]["totalPoints"],
            0,
        )
        self.assertEqual(connection.commits, 1)
        self.assertEqual(connection.rollbacks, 0)
        names = [call[0] for call in connection.calls]
        self.assertEqual(
            names,
            [
                "select_owned_parent_job_for_update",
                "select_private_job_by_request_for_update",
                "count_active_free_reworks",
                "create_job",
                "create_settlement",
                "create_outbox",
            ],
        )
        parent_call = connection.calls[0]
        self.assertIn("FOR UPDATE", parent_call[1])
        self.assertEqual(parent_call[2], ("parent-job-1", "user-1"))
        self.assertNotIn("create_point_order", names)
        self.assertNotIn("debit_point_account", names)

    def test_revision_quota_paid_candidate_debits_in_same_transaction(
        self,
    ) -> None:
        paid_job = persisted_revision_job(free=False, digest=DIGEST_B)
        connection = ScriptedConnection(
            select_owned_parent_job_for_update=[[parent_generation_job()]],
            select_private_job_by_request_for_update=[[]],
            count_active_free_reworks=[
                [{"active_free_rework_count": 1}]
            ],
            create_point_account=[[]],
            select_point_account_for_update=[[account_row()]],
            create_job=[[paid_job]],
            create_point_order=[
                [
                    point_order_row(
                        id="revision:revision-job-1:debit",
                        order_kind="debit",
                        points=10,
                        job_id="revision-job-1",
                        request_sha256=DIGEST_B,
                    )
                ]
            ],
            debit_point_account=[
                [
                    account_row(
                        balance_points=90,
                        lifetime_debited_points=10,
                        version=2,
                    )
                ]
            ],
            create_point_ledger=[[]],
            create_settlement=[[]],
            create_outbox=[[]],
        )

        result = create_revision_with_quota(ProductJobStore(connection))

        self.assertTrue(result.created)
        self.assertEqual(result.selected_candidate, "paid")
        self.assertEqual(
            result.request_payload["billing"]["totalPoints"],
            10,
        )
        self.assertEqual(connection.commits, 1)
        names = [call[0] for call in connection.calls]
        self.assertEqual(
            names,
            [
                "select_owned_parent_job_for_update",
                "select_private_job_by_request_for_update",
                "count_active_free_reworks",
                "create_point_account",
                "select_point_account_for_update",
                "create_job",
                "create_point_order",
                "debit_point_account",
                "create_point_ledger",
                "create_settlement",
                "create_outbox",
            ],
        )
        self.assertIn("FOR UPDATE", connection.calls[4][1])

    def test_revision_paid_outbox_failure_rolls_back_job_and_wallet_debit(
        self,
    ) -> None:
        connection = ScriptedConnection(
            select_owned_parent_job_for_update=[[parent_generation_job()]],
            select_private_job_by_request_for_update=[[]],
            count_active_free_reworks=[
                [{"active_free_rework_count": 1}]
            ],
            create_point_account=[[]],
            select_point_account_for_update=[[account_row()]],
            create_job=[
                [persisted_revision_job(free=False, digest=DIGEST_B)]
            ],
            create_point_order=[
                [
                    point_order_row(
                        id="revision:revision-job-1:debit",
                        order_kind="debit",
                        points=10,
                        job_id="revision-job-1",
                        request_sha256=DIGEST_B,
                    )
                ]
            ],
            debit_point_account=[[account_row(balance_points=90)]],
            create_point_ledger=[[]],
            create_settlement=[[]],
            create_outbox=[RuntimeError("outbox unavailable")],
        )

        with self.assertRaisesRegex(RuntimeError, "outbox unavailable"):
            create_revision_with_quota(ProductJobStore(connection))

        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)
        names = [call[0] for call in connection.calls]
        self.assertIn("create_job", names)
        self.assertIn("debit_point_account", names)
        self.assertIn("create_settlement", names)

    def test_revision_replay_returns_original_free_candidate_before_quota_check(
        self,
    ) -> None:
        connection = ScriptedConnection(
            select_owned_parent_job_for_update=[[parent_generation_job()]],
            select_private_job_by_request_for_update=[
                [persisted_revision_job(free=True, digest=DIGEST_A)]
            ],
            select_atomic_job_integrity=[
                [revision_integrity_row(free=True, digest=DIGEST_A)]
            ],
        )

        result = create_revision_with_quota(
            ProductJobStore(connection),
            free_limit=0,
        )

        self.assertFalse(result.created)
        self.assertEqual(result.selected_candidate, "free")
        self.assertTrue(
            result.request_payload["billing"]["freeReworkQuotaVerified"]
        )
        names = [call[0] for call in connection.calls]
        self.assertEqual(
            names,
            [
                "select_owned_parent_job_for_update",
                "select_private_job_by_request_for_update",
                "select_atomic_job_integrity",
            ],
        )
        self.assertNotIn("count_active_free_reworks", names)
        self.assertNotIn("debit_point_account", names)

    def test_revision_replay_returns_original_paid_candidate_when_slot_is_free(
        self,
    ) -> None:
        connection = ScriptedConnection(
            select_owned_parent_job_for_update=[[parent_generation_job()]],
            select_private_job_by_request_for_update=[
                [persisted_revision_job(free=False, digest=DIGEST_B)]
            ],
            select_atomic_job_integrity=[
                [revision_integrity_row(free=False, digest=DIGEST_B)]
            ],
        )

        result = create_revision_with_quota(ProductJobStore(connection))

        self.assertFalse(result.created)
        self.assertEqual(result.selected_candidate, "paid")
        self.assertFalse(
            result.request_payload["billing"]["freeReworkQuotaVerified"]
        )
        self.assertNotIn(
            "count_active_free_reworks",
            [call[0] for call in connection.calls],
        )

    def test_revision_replay_rejects_digest_outside_both_candidates(
        self,
    ) -> None:
        connection = ScriptedConnection(
            select_owned_parent_job_for_update=[[parent_generation_job()]],
            select_private_job_by_request_for_update=[
                [persisted_revision_job(free=True, digest=DIGEST_C)]
            ],
        )

        with self.assertRaises(RequestDigestConflict):
            create_revision_with_quota(ProductJobStore(connection))

        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)
        self.assertNotIn(
            "count_active_free_reworks",
            [call[0] for call in connection.calls],
        )

    def test_revision_parent_must_be_owned_succeeded_product_batch(
        self,
    ) -> None:
        missing = ScriptedConnection(
            select_owned_parent_job_for_update=[[]],
        )
        with self.assertRaises(JobNotFound):
            create_revision_with_quota(ProductJobStore(missing))
        self.assertEqual(
            missing.calls[0][2],
            ("parent-job-1", "user-1"),
        )

        wrong_type = ScriptedConnection(
            select_owned_parent_job_for_update=[
                [
                    parent_generation_job(
                        request_payload={"jobType": "other"},
                    )
                ]
            ],
        )
        with self.assertRaisesRegex(JobStateConflict, "product_batch"):
            create_revision_with_quota(ProductJobStore(wrong_type))

        wrong_status = ScriptedConnection(
            select_owned_parent_job_for_update=[
                [parent_generation_job(status="running")]
            ],
        )
        with self.assertRaisesRegex(JobStateConflict, "succeeded"):
            create_revision_with_quota(ProductJobStore(wrong_status))

        for rejected in (missing, wrong_type, wrong_status):
            self.assertEqual(rejected.commits, 0)
            self.assertEqual(rejected.rollbacks, 1)
            self.assertNotIn(
                "count_active_free_reworks",
                [call[0] for call in rejected.calls],
            )

    def test_revision_insert_conflict_reloads_original_candidate_without_debit(
        self,
    ) -> None:
        connection = ScriptedConnection(
            select_owned_parent_job_for_update=[[parent_generation_job()]],
            select_private_job_by_request_for_update=[[]],
            count_active_free_reworks=[
                [{"active_free_rework_count": 0}]
            ],
            create_job=[[]],
            select_private_job_by_request=[
                [persisted_revision_job(free=True, digest=DIGEST_A)]
            ],
            select_atomic_job_integrity=[
                [revision_integrity_row(free=True, digest=DIGEST_A)]
            ],
        )

        result = create_revision_with_quota(ProductJobStore(connection))

        self.assertFalse(result.created)
        self.assertEqual(result.selected_candidate, "free")
        self.assertEqual(connection.commits, 1)
        self.assertNotIn(
            "debit_point_account",
            [call[0] for call in connection.calls],
        )

    def test_revision_quota_sql_contract_serializes_parent_before_count(
        self,
    ) -> None:
        """This proves SQL/call order, not real multi-connection lock blocking."""

        connection = ScriptedConnection(
            select_owned_parent_job_for_update=[[parent_generation_job()]],
            select_private_job_by_request_for_update=[[]],
            count_active_free_reworks=[
                [{"active_free_rework_count": 0}]
            ],
            create_job=[
                [persisted_revision_job(free=True, digest=DIGEST_A)]
            ],
            create_settlement=[[]],
            create_outbox=[[]],
        )

        create_revision_with_quota(ProductJobStore(connection))

        parent_index = next(
            index
            for index, call in enumerate(connection.calls)
            if call[0] == "select_owned_parent_job_for_update"
        )
        count_index = next(
            index
            for index, call in enumerate(connection.calls)
            if call[0] == "count_active_free_reworks"
        )
        self.assertLess(parent_index, count_index)
        self.assertIn("FOR UPDATE", connection.calls[parent_index][1])
        self.assertEqual(connection.commits, 1)

    def test_create_job_and_outbox_are_one_committed_transaction(self) -> None:
        connection = ScriptedConnection(
            create_job=[[job_row()]],
            create_settlement=[[]],
            create_outbox=[[]],
        )

        result = create_job(ProductJobStore(connection))

        self.assertTrue(result.created)
        self.assertEqual(result.job["id"], "job-1")
        self.assertEqual(
            [call[0] for call in connection.calls],
            ["create_job", "create_settlement", "create_outbox"],
        )
        self.assertEqual(connection.commits, 1)
        self.assertEqual(connection.rollbacks, 0)
        self.assertTrue(all("CREATE TABLE" not in call[1] for call in connection.calls))
        outbox_payload = connection.calls[-1][2][2]
        self.assertIn('"requestSha256":"' + DIGEST_A + '"', outbox_payload)

    def test_same_digest_returns_existing_job_without_duplicate_side_effects(self) -> None:
        connection = ScriptedConnection(
            create_job=[[]],
            select_job_by_request=[[job_row()]],
        )

        result = create_job(ProductJobStore(connection))

        self.assertFalse(result.created)
        self.assertEqual(result.job["id"], "job-1")
        self.assertEqual(
            [call[0] for call in connection.calls],
            ["create_job", "select_job_by_request"],
        )
        self.assertEqual(connection.commits, 1)

    def test_outbox_insert_failure_rolls_back_job_and_settlement(self) -> None:
        connection = ScriptedConnection(
            create_job=[[job_row()]],
            create_settlement=[[]],
            create_outbox=[RuntimeError("database write failed")],
        )

        with self.assertRaisesRegex(RuntimeError, "database write failed"):
            create_job(ProductJobStore(connection))

        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)
        self.assertEqual(
            [call[0] for call in connection.calls],
            ["create_job", "create_settlement", "create_outbox"],
        )

    def test_same_idempotency_key_with_different_digest_conflicts(self) -> None:
        connection = ScriptedConnection(
            create_job=[[]],
            select_job_by_request=[[job_row(request_sha256=DIGEST_B)]],
        )

        with self.assertRaises(RequestDigestConflict):
            create_job(ProductJobStore(connection))

        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)

    def test_claim_outbox_uses_skip_locked_and_returns_fence(self) -> None:
        connection = ScriptedConnection(
            claim_outbox=[
                [
                    {
                        "outbox_id": "outbox-1",
                        "job_id": "job-1",
                        "event_type": "product_generation.requested",
                        "payload": '{"jobId":"job-1"}',
                        "attempt_count": 1,
                        "claim_token": "claim-1",
                        "claimed_until": "later",
                        "owner_user_id": "user-1",
                        "request_sha256": DIGEST_A,
                        "fence": 1,
                        "version": 1,
                    }
                ]
            ]
        )

        claims = ProductJobStore(connection).claim_outbox(
            worker_id="dispatcher-1",
            limit=4,
            lease_seconds=90,
            claim_token="claim-1",
        )

        self.assertEqual(claims[0]["payload"], {"jobId": "job-1"})
        self.assertEqual(claims[0]["fence"], 1)
        _, sql, params = connection.calls[0]
        self.assertIn("FOR UPDATE OF o, j SKIP LOCKED", sql)
        self.assertIn("WHEN j.status = 'queued' THEN j.fence + 1", sql)
        self.assertIn("ELSE j.fence", sql)
        self.assertIn(
            "jsonb_build_object('request', f.request_payload)",
            sql,
        )
        self.assertEqual(params, (4, "dispatcher-1", "claim-1", 90))
        self.assertEqual(connection.commits, 1)

    def test_outbox_reclaim_keeps_dispatch_fence_stable_after_enqueue_timeout(
        self,
    ) -> None:
        first_connection = ScriptedConnection(
            claim_outbox=[
                [
                    {
                        "outbox_id": "outbox-1",
                        "job_id": "job-1",
                        "event_type": "product_generation.requested",
                        "payload": '{"jobId":"job-1"}',
                        "attempt_count": 1,
                        "claim_token": "claim-1",
                        "claimed_until": "later",
                        "owner_user_id": "user-1",
                        "request_sha256": DIGEST_A,
                        "fence": 1,
                        "version": 1,
                    }
                ]
            ]
        )
        reclaimed_connection = ScriptedConnection(
            claim_outbox=[
                [
                    {
                        "outbox_id": "outbox-1",
                        "job_id": "job-1",
                        "event_type": "product_generation.requested",
                        "payload": '{"jobId":"job-1"}',
                        "attempt_count": 2,
                        "claim_token": "claim-2",
                        "claimed_until": "later-again",
                        "owner_user_id": "user-1",
                        "request_sha256": DIGEST_A,
                        "fence": 1,
                        "version": 2,
                    }
                ]
            ]
        )

        first = ProductJobStore(first_connection).claim_outbox(
            worker_id="dispatcher-1",
            claim_token="claim-1",
        )
        reclaimed = ProductJobStore(reclaimed_connection).claim_outbox(
            worker_id="dispatcher-2",
            claim_token="claim-2",
        )

        self.assertEqual(first[0]["fence"], 1)
        self.assertEqual(reclaimed[0]["fence"], 1)
        self.assertGreater(
            reclaimed[0]["version"],
            first[0]["version"],
        )
        reclaim_sql = reclaimed_connection.calls[0][1]
        self.assertIn(
            "WHEN j.status = 'queued' THEN j.fence + 1",
            reclaim_sql,
        )
        self.assertIn("ELSE j.fence", reclaim_sql)
        self.assertNotIn("SET status = 'running',\n        fence = j.fence + 1", reclaim_sql)

    def test_outbox_publish_requires_current_claim_token(self) -> None:
        connection = ScriptedConnection(
            mark_outbox_published=[[]],
            select_outbox=[
                [
                    {
                        "id": "outbox-1",
                        "job_id": "job-1",
                        "status": "claimed",
                        "claim_token": "new-token",
                        "published_at": None,
                    }
                ]
            ],
        )

        with self.assertRaises(OutboxClaimLost):
            ProductJobStore(connection).mark_outbox_published(
                outbox_id="outbox-1",
                claim_token="old-token",
            )

        self.assertEqual(connection.rollbacks, 1)

    def test_result_first_writer_locks_job_and_creates_unique_slot(self) -> None:
        connection = ScriptedConnection(upsert_result=[[result_row()]])

        written = write_result(ProductJobStore(connection))

        self.assertTrue(written.created)
        self.assertEqual(written.result["job_fence"], 1)
        _, sql, params = connection.calls[0]
        self.assertIn("FOR UPDATE", sql)
        self.assertIn(
            "ON CONFLICT (job_id, menu_row, platform, variant) DO NOTHING",
            sql,
        )
        self.assertEqual(params[0:6], ("job-1", 1, 7, "meituan", "default", "generated"))
        self.assertEqual(connection.commits, 1)

    def test_result_same_content_retry_is_idempotent_even_after_reclaim(self) -> None:
        existing = result_row(job_fence=1)
        connection = ScriptedConnection(
            upsert_result=[[]],
            select_result_context=[
                [
                    result_context(
                        current_fence=2,
                        result=existing,
                    )
                ]
            ],
        )

        retried = write_result(ProductJobStore(connection), fence=2)

        self.assertFalse(retried.created)
        self.assertEqual(retried.result["job_fence"], 1)
        self.assertEqual(connection.commits, 1)

    def test_result_first_writer_rejects_different_content(self) -> None:
        connection = ScriptedConnection(
            upsert_result=[[]],
            select_result_context=[[result_context(result=result_row())]],
        )

        with self.assertRaises(ResultWriteConflict):
            write_result(ProductJobStore(connection), object_sha256=DIGEST_A)

        self.assertEqual(connection.rollbacks, 1)

    def test_result_write_rejects_old_fence_before_idempotency(self) -> None:
        connection = ScriptedConnection(
            upsert_result=[[]],
            select_result_context=[
                [
                    result_context(
                        current_fence=2,
                        result=result_row(),
                    )
                ]
            ],
        )

        with self.assertRaises(FenceMismatch):
            write_result(ProductJobStore(connection), fence=1)

        self.assertEqual(connection.rollbacks, 1)

    def test_result_write_rejects_cancel_requested_and_terminal_jobs(self) -> None:
        canceled_connection = ScriptedConnection(
            upsert_result=[[]],
            select_result_context=[
                [result_context(cancel_requested=True, result=result_row())]
            ],
        )
        with self.assertRaises(CancellationRequested):
            write_result(ProductJobStore(canceled_connection))

        terminal_connection = ScriptedConnection(
            upsert_result=[[]],
            select_result_context=[
                [
                    result_context(
                        job_status="succeeded",
                        result=result_row(),
                    )
                ]
            ],
        )
        with self.assertRaises(JobStateConflict):
            write_result(ProductJobStore(terminal_connection))

    def test_queued_cancel_is_terminal_and_sets_full_refund_target(self) -> None:
        canceled = job_row(
            status="canceled",
            version=1,
            cancel_requested=True,
            refund_target_points=30,
        )
        connection = ScriptedConnection(
            request_cancel=[[canceled]],
            cancel_outbox=[[]],
            set_canceled_settlement_target=[[{"job_id": "job-1"}]],
        )

        result = ProductJobStore(connection).request_cancel(
            job_id="job-1",
            owner_user_id="user-1",
        )

        self.assertFalse(result.idempotent)
        self.assertEqual(result.job["status"], "canceled")
        self.assertEqual(result.job["refund_target_points"], 30)
        self.assertEqual(
            [call[0] for call in connection.calls],
            [
                "request_cancel",
                "cancel_outbox",
                "set_canceled_settlement_target",
            ],
        )

    def test_running_cancel_is_only_a_request_until_fenced_worker_finishes(self) -> None:
        cancel_requested = job_row(
            status="running",
            version=2,
            fence=1,
            cancel_requested=True,
            started_at="2026-07-30T00:01:00Z",
        )
        connection = ScriptedConnection(request_cancel=[[cancel_requested]])

        result = ProductJobStore(connection).request_cancel(
            job_id="job-1",
            owner_user_id="user-1",
        )

        self.assertFalse(result.idempotent)
        self.assertEqual(result.job["status"], "running")
        self.assertTrue(result.job["cancel_requested"])
        self.assertEqual([call[0] for call in connection.calls], ["request_cancel"])

    def test_completion_requires_current_fence_and_updates_settlement_target(self) -> None:
        succeeded = job_row(
            status="succeeded",
            version=2,
            fence=1,
            completed_count=3,
            manifest_ref="private/manifests/job-1.json",
            manifest_sha256=DIGEST_B,
        )
        connection = ScriptedConnection(
            complete_with_fence=[[succeeded]],
            set_settlement_target=[[{"job_id": "job-1"}]],
        )

        result = ProductJobStore(connection).complete_with_fence(
            job_id="job-1",
            fence=1,
            terminal_status="succeeded",
            completed_count=3,
            failed_count=0,
            refund_target_points=0,
            manifest_ref="private/manifests/job-1.json",
            manifest_sha256=DIGEST_B,
        )

        self.assertFalse(result.idempotent)
        self.assertEqual(result.job["status"], "succeeded")
        self.assertEqual(
            [call[0] for call in connection.calls],
            ["complete_with_fence", "set_settlement_target"],
        )

    def test_old_fence_cannot_complete_after_reclaim(self) -> None:
        connection = ScriptedConnection(
            complete_with_fence=[[]],
            select_job=[[job_row(status="running", fence=2, version=2)]],
        )

        with self.assertRaises(FenceMismatch) as raised:
            ProductJobStore(connection).complete_with_fence(
                job_id="job-1",
                fence=1,
                terminal_status="failed",
                completed_count=0,
                failed_count=3,
                refund_target_points=30,
                error_message="provider timeout",
            )

        self.assertEqual(raised.exception.actual_fence, 2)
        self.assertEqual(connection.rollbacks, 1)

    def test_success_cannot_win_after_cancel_was_requested(self) -> None:
        connection = ScriptedConnection(
            complete_with_fence=[[]],
            select_job=[
                [
                    job_row(
                        status="running",
                        fence=1,
                        version=2,
                        cancel_requested=True,
                    )
                ]
            ],
        )

        with self.assertRaises(CancellationRequested):
            ProductJobStore(connection).complete_with_fence(
                job_id="job-1",
                fence=1,
                terminal_status="succeeded",
                completed_count=3,
                failed_count=0,
                refund_target_points=0,
                manifest_ref="private/manifests/job-1.json",
                manifest_sha256=DIGEST_B,
            )

    def test_settlement_claim_is_single_cas_and_same_token_is_idempotent(self) -> None:
        claimed = settlement_row(
            status="claimed",
            version=1,
            claim_token="settle-1",
            claimed_by="billing-1",
            claimed_at="2026-07-30T00:02:00Z",
        )
        first_connection = ScriptedConnection(claim_settlement=[[claimed]])
        first = ProductJobStore(first_connection).claim_settlement_once(
            job_id="job-1",
            expected_version=0,
            claimed_by="billing-1",
            claim_token="settle-1",
        )
        self.assertFalse(first.idempotent)

        retry_connection = ScriptedConnection(
            claim_settlement=[[]],
            select_settlement=[[claimed]],
        )
        retry = ProductJobStore(retry_connection).claim_settlement_once(
            job_id="job-1",
            expected_version=0,
            claimed_by="billing-1",
            claim_token="settle-1",
        )
        self.assertTrue(retry.idempotent)

        conflict_connection = ScriptedConnection(
            claim_settlement=[[]],
            select_settlement=[[claimed]],
        )
        with self.assertRaises(SettlementConflict):
            ProductJobStore(conflict_connection).claim_settlement_once(
                job_id="job-1",
                expected_version=0,
                claimed_by="billing-2",
                claim_token="settle-2",
            )

        claim_sql = first_connection.calls[0][1]
        self.assertIn("s.status = 'claimed'", claim_sql)
        self.assertIn("s.claimed_at", claim_sql)
        self.assertEqual(first_connection.calls[0][2][-2:], (300, 0))

    def test_reconciliation_candidates_are_bounded_and_explicit(self) -> None:
        candidate = {
            "job_id": "job-1",
            "owner_user_id": "user-1",
            "request_sha256": DIGEST_A,
            "job_type": "menu_batch_generation",
            "job_status": "running",
            "fence": 1,
            "cancel_requested": False,
            "settlement_status": "pending",
            "settlement_version": 0,
            "outbox_status": "published",
        }
        connection = ScriptedConnection(
            list_reconciliation_candidates=[[candidate]]
        )

        rows = ProductJobStore(connection).list_reconciliation_candidates(
            limit=25
        )

        self.assertEqual(rows, [candidate])
        self.assertEqual(connection.calls[0][2], (25,))
        sql = connection.calls[0][1]
        self.assertIn("j.status IN ('queued', 'running')", sql)
        self.assertIn("s.status IN ('pending', 'claimed')", sql)
        self.assertIn("LIMIT %s", sql)
        with self.assertRaises(InvalidProductJobInput):
            ProductJobStore(ScriptedConnection()).list_reconciliation_candidates(
                limit=1001
            )

    def test_missing_redis_task_requeues_only_published_matching_fence(self) -> None:
        requeued = {
            "id": "outbox-1",
            "job_id": "job-1",
            "status": "pending",
            "available_at": "2026-07-30T00:10:00Z",
            "last_error": "redis_task_missing",
        }
        connection = ScriptedConnection(
            requeue_published_outbox=[[requeued]]
        )

        result = ProductJobStore(
            connection
        ).requeue_published_outbox_if_task_missing(
            job_id="job-1",
            expected_fence=3,
        )

        self.assertEqual(result, requeued)
        self.assertEqual(connection.calls[0][2], ("job-1", 3))
        sql = connection.calls[0][1]
        self.assertIn("o.status = 'published'", sql)
        self.assertIn("j.status = 'running'", sql)
        self.assertIn("j.fence = %s", sql)
        self.assertIn("j.cancel_requested = FALSE", sql)

    def test_mark_settlement_applied_is_token_bound_and_idempotent(self) -> None:
        applied = settlement_row(
            status="applied",
            version=2,
            refund_target_points=10,
            refund_applied_points=10,
            claim_token="settle-1",
            claimed_by="billing-1",
            claimed_at="2026-07-30T00:02:00Z",
            applied_at="2026-07-30T00:04:00Z",
            finalized_at="2026-07-30T00:04:00Z",
            provider_reference="refund-provider-1",
        )
        first_connection = ScriptedConnection(
            mark_settlement_applied=[[applied]]
        )
        first = ProductJobStore(first_connection).mark_settlement_applied(
            job_id="job-1",
            claim_token="settle-1",
            refund_applied_points=10,
            provider_reference="refund-provider-1",
        )
        self.assertFalse(first.idempotent)

        retry_connection = ScriptedConnection(
            mark_settlement_applied=[[]],
            select_settlement=[[applied]],
        )
        retry = ProductJobStore(retry_connection).mark_settlement_applied(
            job_id="job-1",
            claim_token="settle-1",
            refund_applied_points=10,
            provider_reference="refund-provider-1",
        )
        self.assertTrue(retry.idempotent)

    def test_mark_settlement_applied_rejects_different_token_or_amount(self) -> None:
        applied = settlement_row(
            status="applied",
            version=2,
            refund_target_points=10,
            refund_applied_points=10,
            claim_token="settle-1",
            claimed_by="billing-1",
            claimed_at="2026-07-30T00:02:00Z",
            applied_at="2026-07-30T00:04:00Z",
            finalized_at="2026-07-30T00:04:00Z",
            provider_reference="refund-provider-1",
        )
        token_connection = ScriptedConnection(
            mark_settlement_applied=[[]],
            select_settlement=[[applied]],
        )
        with self.assertRaises(SettlementConflict):
            ProductJobStore(token_connection).mark_settlement_applied(
                job_id="job-1",
                claim_token="settle-2",
                refund_applied_points=10,
                provider_reference="refund-provider-1",
            )

        amount_connection = ScriptedConnection(
            mark_settlement_applied=[[]],
            select_settlement=[[applied]],
        )
        with self.assertRaises(SettlementConflict):
            ProductJobStore(amount_connection).mark_settlement_applied(
                job_id="job-1",
                claim_token="settle-1",
                refund_applied_points=9,
                provider_reference="refund-provider-1",
            )

    def test_mark_failed_and_manual_review_are_terminal_and_idempotent(self) -> None:
        failed = settlement_row(
            status="failed",
            version=2,
            refund_target_points=10,
            claim_token="settle-1",
            claimed_by="billing-1",
            claimed_at="2026-07-30T00:02:00Z",
            finalized_at="2026-07-30T00:04:00Z",
            error_message="refund provider unavailable",
        )
        failed_connection = ScriptedConnection(
            mark_settlement_problem=[[failed]]
        )
        failed_result = ProductJobStore(
            failed_connection
        ).mark_settlement_failed(
            job_id="job-1",
            claim_token="settle-1",
            error_message="refund provider unavailable",
        )
        self.assertFalse(failed_result.idempotent)
        self.assertEqual(failed_result.settlement["status"], "failed")

        manual = settlement_row(
            status="manual_review",
            version=2,
            refund_target_points=10,
            refund_applied_points=4,
            claim_token="settle-2",
            claimed_by="billing-2",
            claimed_at="2026-07-30T00:02:00Z",
            applied_at="2026-07-30T00:04:00Z",
            finalized_at="2026-07-30T00:04:00Z",
            provider_reference="provider-uncertain-1",
            error_message="provider response was ambiguous",
        )
        manual_connection = ScriptedConnection(
            mark_settlement_problem=[[]],
            select_settlement=[[manual]],
        )
        manual_result = ProductJobStore(
            manual_connection
        ).mark_settlement_manual_review(
            job_id="job-1",
            claim_token="settle-2",
            refund_applied_points=4,
            provider_reference="provider-uncertain-1",
            error_message="provider response was ambiguous",
        )
        self.assertTrue(manual_result.idempotent)

    def test_mark_failed_rejects_different_amount_after_finalization(self) -> None:
        failed = settlement_row(
            status="failed",
            version=2,
            refund_target_points=10,
            refund_applied_points=4,
            claim_token="settle-1",
            claimed_by="billing-1",
            claimed_at="2026-07-30T00:02:00Z",
            applied_at="2026-07-30T00:04:00Z",
            finalized_at="2026-07-30T00:04:00Z",
            provider_reference="provider-partial-1",
            error_message="partial refund",
        )
        connection = ScriptedConnection(
            mark_settlement_problem=[[]],
            select_settlement=[[failed]],
        )

        with self.assertRaises(SettlementConflict):
            ProductJobStore(connection).mark_settlement_failed(
                job_id="job-1",
                claim_token="settle-1",
                refund_applied_points=3,
                provider_reference="provider-partial-1",
                error_message="partial refund",
            )

    def test_apply_settlement_refund_uses_locked_server_target_atomically(
        self,
    ) -> None:
        claimed = settlement_row(
            status="claimed",
            version=1,
            refund_target_points=10,
            claim_token="settle-1",
            claimed_by="billing-1",
            claimed_at="2026-07-30T00:02:00Z",
            request_sha256=DIGEST_A,
            job_status="failed",
        )
        refund_order = point_order_row(
            id="refund-job-1",
            order_kind="refund",
            points=10,
            source_order_id="debit-job-1",
            source_order_kind="debit",
            job_id="job-1",
            metadata={
                "jobId": "job-1",
                "providerReference": "wallet-postgres",
                "purpose": "product_generation_refund",
            },
        )
        applied = settlement_row(
            status="applied",
            version=2,
            refund_target_points=10,
            refund_applied_points=10,
            claim_token="settle-1",
            claimed_by="billing-1",
            claimed_at="2026-07-30T00:02:00Z",
            applied_at="2026-07-30T00:04:00Z",
            finalized_at="2026-07-30T00:04:00Z",
            provider_reference="wallet-postgres",
        )
        connection = ScriptedConnection(
            select_settlement_refund_for_update=[[claimed]],
            select_point_account_for_update=[
                [account_row(balance_points=70)]
            ],
            create_point_order=[[refund_order]],
            refund_point_account=[
                [
                    account_row(
                        balance_points=80,
                        lifetime_refunded_points=10,
                        version=3,
                    )
                ]
            ],
            create_point_ledger=[[]],
            apply_settlement_refund=[[applied]],
        )

        result = ProductJobStore(connection).apply_settlement_refund(
            job_id="job-1",
            claim_token="settle-1",
            provider_reference="wallet-postgres",
        )

        self.assertFalse(result.idempotent)
        self.assertEqual(result.account["balance_points"], 80)
        self.assertEqual(result.settlement["refund_applied_points"], 10)
        self.assertEqual(
            [call[0] for call in connection.calls],
            [
                "select_settlement_refund_for_update",
                "select_point_account_for_update",
                "create_point_order",
                "refund_point_account",
                "create_point_ledger",
                "apply_settlement_refund",
            ],
        )
        self.assertIn("FOR UPDATE OF s, j", connection.calls[0][1])
        refund_order_params = connection.calls[2][2]
        self.assertEqual(refund_order_params[3], 10)
        self.assertEqual(refund_order_params[4], "debit-job-1")
        self.assertEqual(
            connection.calls[-1][2],
            ("wallet-postgres", "job-1", "settle-1"),
        )
        self.assertIn(
            "refund_applied_points = refund_target_points",
            connection.calls[-1][1],
        )
        self.assertEqual(connection.commits, 1)

    def test_apply_settlement_refund_retry_does_not_credit_twice(self) -> None:
        applied = settlement_row(
            status="applied",
            version=2,
            refund_target_points=10,
            refund_applied_points=10,
            claim_token="settle-1",
            claimed_by="billing-1",
            claimed_at="2026-07-30T00:02:00Z",
            applied_at="2026-07-30T00:04:00Z",
            finalized_at="2026-07-30T00:04:00Z",
            provider_reference="wallet-postgres",
            request_sha256=DIGEST_A,
            job_status="failed",
        )
        refund_order = point_order_row(
            id="refund-job-1",
            order_kind="refund",
            points=10,
            source_order_id="debit-job-1",
            source_order_kind="debit",
            job_id="job-1",
            metadata={
                "jobId": "job-1",
                "providerReference": "wallet-postgres",
                "purpose": "product_generation_refund",
            },
        )
        connection = ScriptedConnection(
            select_settlement_refund_for_update=[[applied]],
            select_point_account_for_update=[
                [account_row(balance_points=80)]
            ],
            select_point_order=[[refund_order]],
        )

        retry = ProductJobStore(connection).apply_settlement_refund(
            job_id="job-1",
            claim_token="settle-1",
            provider_reference="wallet-postgres",
        )

        self.assertTrue(retry.idempotent)
        self.assertEqual(
            [call[0] for call in connection.calls],
            [
                "select_settlement_refund_for_update",
                "select_point_account_for_update",
                "select_point_order",
            ],
        )
        self.assertNotIn(
            "refund_point_account",
            [call[0] for call in connection.calls],
        )

    def test_zero_refund_settlement_creates_no_point_order_or_ledger(self) -> None:
        claimed = settlement_row(
            status="claimed",
            claim_token="claim-zero",
            claimed_by="reconciler-1",
            claimed_at="2026-07-30T00:10:00Z",
            debit_points=0,
            refund_target_points=0,
            request_sha256=DIGEST_A,
            job_status="succeeded",
        )
        applied = settlement_row(
            status="applied",
            claim_token="claim-zero",
            claimed_by="reconciler-1",
            claimed_at="2026-07-30T00:10:00Z",
            applied_at="2026-07-30T00:11:00Z",
            finalized_at="2026-07-30T00:11:00Z",
            provider_reference="internal:job-1:refund:0",
            debit_points=0,
            refund_target_points=0,
            refund_applied_points=0,
        )
        connection = ScriptedConnection(
            select_settlement_refund_for_update=[[claimed]],
            select_point_account_for_update=[[account_row()]],
            apply_settlement_refund=[[applied]],
        )

        result = ProductJobStore(connection).apply_settlement_refund(
            job_id="job-1",
            claim_token="claim-zero",
            provider_reference="internal:job-1:refund:0",
        )

        self.assertFalse(result.idempotent)
        self.assertIsNone(result.order)
        self.assertEqual(result.account["balance_points"], 100)
        self.assertEqual(
            [name for name, _sql, _params in connection.calls],
            [
                "select_settlement_refund_for_update",
                "select_point_account_for_update",
                "apply_settlement_refund",
            ],
        )

    def test_apply_settlement_refund_rejects_target_above_debit(self) -> None:
        corrupt = settlement_row(
            status="claimed",
            debit_points=30,
            refund_target_points=31,
            claim_token="settle-1",
            claimed_by="billing-1",
            claimed_at="2026-07-30T00:02:00Z",
            request_sha256=DIGEST_A,
            job_status="failed",
        )
        connection = ScriptedConnection(
            select_settlement_refund_for_update=[[corrupt]],
        )
        with self.assertRaises(WalletIntegrityError):
            ProductJobStore(connection).apply_settlement_refund(
                job_id="job-1",
                claim_token="settle-1",
                provider_reference="wallet-postgres",
            )
        self.assertEqual(connection.rollbacks, 1)
        self.assertEqual(
            [call[0] for call in connection.calls],
            ["select_settlement_refund_for_update"],
        )

    def test_count_active_free_reworks_is_owner_and_parent_scoped(self) -> None:
        connection = ScriptedConnection(
            count_active_free_reworks=[
                [{"active_free_rework_count": 2}]
            ],
        )

        count = ProductJobStore(connection).count_active_free_reworks(
            owner_user_id="user-1",
            parent_generation_job_id="parent-job-1",
            revision_job_type="delivery_asset_revision_batch",
        )

        self.assertEqual(count, 2)
        name, _sql, parameters = connection.calls[0]
        self.assertEqual(name, "count_active_free_reworks")
        self.assertEqual(
            parameters,
            (
                "user-1",
                "delivery_asset_revision_batch",
                "parent-job-1",
            ),
        )

    def test_owned_job_detail_is_owner_scoped_and_explicitly_private(self) -> None:
        private_job = job_row(
            request_payload='{"quality":"standard"}',
            menu_upload_id="menu-1",
            menu_object_ref="private/menus/menu-1.xlsx",
            menu_object_sha256=DIGEST_B,
            selected_background_ref="private/backgrounds/bg-1.png",
            selected_background_sha256=DIGEST_C,
            manifest_ref="private/manifests/job-1.json",
            manifest_sha256=DIGEST_A,
        )
        settlement_summary = {
            "job_id": "job-1",
            "owner_user_id": "user-1",
            "status": "pending",
            "version": 0,
            "debit_order_id": "debit-job-1",
            "debit_points": 30,
            "refund_order_id": "refund-job-1",
            "refund_target_points": 0,
            "refund_applied_points": 0,
            "provider_reference": "",
        }
        outbox_summary = {
            "id": "outbox-1",
            "job_id": "job-1",
            "event_type": "product_generation.requested",
            "status": "pending",
            "attempt_count": 0,
        }
        connection = ScriptedConnection(
            select_owned_private_job=[[private_job]],
            select_owned_settlement_summary=[[settlement_summary]],
            select_owned_outbox_summary=[[outbox_summary]],
            select_owned_results=[[result_row()]],
        )

        detail = ProductJobStore(connection).get_owned_job_detail(
            job_id="job-1",
            owner_user_id="user-1",
        )

        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(detail.job["request_payload"], {"quality": "standard"})
        self.assertEqual(
            detail.job["manifest_ref"],
            "private/manifests/job-1.json",
        )
        self.assertEqual(detail.job["status"], "queued")
        self.assertNotIn("claim_token", detail.settlement or {})
        self.assertNotIn("payload", detail.outbox or {})
        self.assertEqual(len(detail.results), 1)
        for call in connection.calls:
            self.assertEqual(call[2][-1], "user-1")

        hidden_connection = ScriptedConnection(
            select_owned_private_job=[[]],
        )
        hidden = ProductJobStore(hidden_connection).get_owned_job_detail(
            job_id="job-1",
            owner_user_id="user-2",
        )
        self.assertIsNone(hidden)
        self.assertEqual(
            [call[0] for call in hidden_connection.calls],
            ["select_owned_private_job"],
        )

    def test_migration_contains_required_shared_state_contracts(self) -> None:
        sql = (
            ROOT / "migrations" / "001_product_generation_postgres.sql"
        ).read_text(encoding="utf-8")

        for table in (
            "product_generation_jobs",
            "product_generation_outbox",
            "product_generation_settlements",
            "product_generation_results",
        ):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table}", sql)
        self.assertIn("UNIQUE (owner_user_id, idempotency_key)", sql)
        self.assertIn("request_sha256", sql)
        self.assertIn("cancel_requested", sql)
        self.assertIn("manifest_sha256", sql)
        self.assertIn("debit_order_id", sql)
        self.assertIn("refund_target_points", sql)
        self.assertIn("UNIQUE (job_id, menu_row, platform, variant)", sql)
        self.assertIn("job_fence BIGINT NOT NULL", sql)
        self.assertIn("finalized_at TIMESTAMPTZ", sql)
        self.assertIn("refund_applied_points = refund_target_points", sql)
        self.assertIn("WHERE status = 'pending'", sql)

    def test_wallet_migration_has_strict_postgres_ledger_contracts(self) -> None:
        sql = (
            ROOT / "migrations" / "002_product_wallet_postgres.sql"
        ).read_text(encoding="utf-8")
        lowered = sql.lower()

        for table in (
            "product_point_accounts",
            "product_point_orders",
            "product_point_ledger",
        ):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table}", sql)
        self.assertIn("CHECK (balance_points >= 0)", sql)
        self.assertIn("metadata JSONB NOT NULL", sql)
        self.assertIn("jsonb_typeof(metadata) = 'object'", sql)
        self.assertIn("UNIQUE (id, owner_user_id, order_kind)", sql)
        self.assertIn("source_order_kind = 'debit'", sql)
        self.assertIn("REFERENCES product_point_orders", sql)
        self.assertIn("UNIQUE (order_id)", sql)
        self.assertIn("delta_points = -points", sql)
        self.assertIn("delta_points = points", sql)
        self.assertIn("fk_product_point_order_job_owner", sql)
        self.assertIn("fk_product_point_order_refund_source", sql)
        self.assertIn("fk_product_point_ledger_order", sql)
        self.assertIn("request_sha256", sql)
        self.assertNotIn("api_key", lowered)
        self.assertNotIn("private_key", lowered)
        self.assertNotIn("access_token", lowered)


if __name__ == "__main__":
    unittest.main()
