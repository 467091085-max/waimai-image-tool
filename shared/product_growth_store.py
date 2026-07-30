from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator, Protocol

import growth_rules


EVENT_REGISTRATION_REWARD = "consumer.registration_reward"
EVENT_FIRST_RECHARGE_REWARD = "consumer.first_recharge_reward"
EVENT_FIRST_RECHARGE_REFUND = "consumer.first_recharge_refund"
SUPPORTED_EVENT_TYPES = frozenset(
    {
        EVENT_REGISTRATION_REWARD,
        EVENT_FIRST_RECHARGE_REWARD,
        EVENT_FIRST_RECHARGE_REFUND,
    }
)

IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$")
TENANT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$")
AGENT_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,63}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

MAX_LIST_LIMIT = 1000
MAX_SOURCE_PAYLOAD_BYTES = 64 * 1024
DEFAULT_EVENT_ACTOR = "service:growth-worker"


class CursorLike(Protocol):
    description: Sequence[Any] | None
    rowcount: int

    def execute(self, operation: str, parameters: Sequence[Any] = ()) -> Any: ...

    def fetchone(self) -> Any: ...

    def fetchall(self) -> Sequence[Any]: ...

    def close(self) -> Any: ...


class ConnectionLike(Protocol):
    autocommit: bool

    def cursor(self) -> CursorLike: ...

    def commit(self) -> Any: ...

    def rollback(self) -> Any: ...


class ProductGrowthStoreError(RuntimeError):
    pass


class InvalidProductGrowthInput(ProductGrowthStoreError, ValueError):
    pass


class ProductGrowthNotFound(ProductGrowthStoreError, LookupError):
    pass


class ProductGrowthConflict(ProductGrowthStoreError):
    pass


class ProductGrowthStateConflict(ProductGrowthConflict):
    pass


class ProductGrowthIntegrityError(ProductGrowthStoreError):
    pass


class ProductGrowthEventDeferred(ProductGrowthStateConflict):
    """The durable prerequisite is expected but has not committed yet."""


@dataclass(frozen=True)
class GrowthCreateResult:
    record: dict[str, Any]
    created: bool


@dataclass(frozen=True)
class InviteCodeIssueResult:
    record: dict[str, Any]
    created: bool
    plaintext_code: str | None


@dataclass(frozen=True)
class GrowthEventApplyResult:
    event: dict[str, Any]
    grants: tuple[dict[str, Any], ...]
    wallet_accounts: tuple[dict[str, Any], ...]
    applied: bool
    idempotent: bool
    debt_recoveries: tuple[dict[str, Any], ...] = ()
    gross_reward_points: int = 0
    debt_offset_points: int = 0
    net_wallet_points: int = 0


@dataclass(frozen=True)
class AgentPaymentClassification:
    tenant_id: str
    customer_user_id: str
    agent_id: str
    agent_owner_user_id: str
    binding_id: str
    source_order_id: str
    canonical_first_order_id: str
    paid_cents: int
    is_first_order: bool
    commission_rate_bps: int
    commission_amount_cents: int
    rule_version: str


@dataclass(frozen=True)
class GrowthRefundApplyResult:
    event: dict[str, Any]
    reversals: tuple[dict[str, Any], ...]
    wallet_account: dict[str, Any] | None
    cumulative_refunded_cents: int
    cumulative_reversed_points: int
    points_assessed: int
    points_recovered: int
    outstanding_points_added: int
    outstanding_points_after: int
    applied: bool
    idempotent: bool


_AGENT_COLUMNS = """
    id, tenant_id, owner_user_id, idempotency_key, agent_code,
    status, rule_version, commission_depth,
    first_order_commission_bps, repeat_order_commission_bps,
    request_sha256, metadata, created_at, updated_at
""".strip()

_BINDING_COLUMNS = """
    id, tenant_id, owner_user_id, idempotency_key,
    agent_id, agent_owner_user_id, customer_user_id,
    relation_depth, source, rule_version,
    first_order_commission_bps, repeat_order_commission_bps,
    request_sha256, metadata, created_at
""".strip()

_INVITE_CODE_COLUMNS = """
    id, tenant_id, owner_user_id, idempotency_key,
    code_sha256, rule_version, request_sha256,
    metadata, created_at
""".strip()

_INVITE_COLUMNS = """
    id, tenant_id, owner_user_id, idempotency_key,
    inviter_user_id, invitee_user_id, invite_code_id,
    invite_code_sha256,
    relation_depth, rule_version, registration_inviter_points,
    registration_invitee_points, first_recharge_rebate_percent,
    cash_points_per_yuan, cents_per_yuan, request_sha256,
    risk_snapshot, metadata, created_at
""".strip()

_REVERSAL_STATE_COLUMNS = """
    id, tenant_id, owner_user_id, invite_relation_id,
    source_order_id, original_business_event_id,
    original_reward_grant_id, reward_owner_user_id,
    original_paid_cents, original_reward_points,
    max_cumulative_refunded_cents, reversed_points,
    recovered_points, outstanding_points,
    version, created_at, updated_at
""".strip()

_DEBT_COLUMNS = """
    tenant_id, owner_user_id, outstanding_points,
    lifetime_assessed_points, lifetime_recovered_points,
    version, created_at, updated_at
""".strip()

_REVERSAL_COLUMNS = """
    id, tenant_id, owner_user_id, business_event_id,
    event_owner_user_id, reversal_state_id,
    original_reward_grant_id, reversal_kind,
    assessed_points, recovered_points,
    outstanding_points_added, outstanding_points_after,
    wallet_order_id, cumulative_refunded_cents,
    cumulative_reversed_points, rule_version,
    content_sha256, metadata, created_at
""".strip()

_EVENT_COLUMNS = """
    id, tenant_id, owner_user_id, idempotency_key,
    source_event_id, business_key, event_type,
    invite_relation_id, source_order_id, rule_version,
    payload, payload_sha256, outcome, outcome_reason,
    result_payload, result_sha256, occurred_at, created_at
""".strip()

_GRANT_COLUMNS = """
    id, tenant_id, owner_user_id, business_event_id,
    event_owner_user_id, invite_relation_id, reward_kind,
    points, debt_offset_points, net_wallet_points,
    wallet_order_id, rule_version, content_sha256, metadata, created_at
""".strip()

_DEBT_RECOVERY_COLUMNS = """
    id, tenant_id, owner_user_id, business_event_id,
    event_owner_user_id, reward_grant_id, reward_kind,
    gross_reward_points, recovered_points, net_wallet_points,
    debt_outstanding_before, debt_outstanding_after,
    rule_version, content_sha256, metadata, created_at
""".strip()

_AUDIT_COLUMNS = """
    action_id, tenant_id, owner_user_id, actor_user_id,
    action, target_type, target_id, status, metadata,
    content_sha256, created_at
""".strip()

_ACCOUNT_COLUMNS = """
    owner_user_id, balance_points, lifetime_credited_points,
    lifetime_debited_points, lifetime_refunded_points,
    version, metadata, created_at, updated_at
""".strip()

_POINT_ORDER_COLUMNS = """
    id, owner_user_id, order_kind, points,
    source_order_id, source_order_kind, job_id,
    request_sha256, metadata, applied_at, created_at, updated_at
""".strip()

_POINT_LEDGER_COLUMNS = """
    id, owner_user_id, order_id, order_kind, points,
    delta_points, balance_after_points, request_sha256,
    metadata, created_at
""".strip()

_PAYMENT_ORDER_COLUMNS = """
    id, owner_user_id, amount_cents, status,
    refunded_amount_cents, refunded_points,
    paid_at, refunded_at, created_at
""".strip()

_INSERT_SUBJECT_LOCK_SQL = """
/* product_growth_store:insert_subject_lock */
INSERT INTO product_growth_subject_locks (tenant_id, owner_user_id)
VALUES (%s, %s)
ON CONFLICT (tenant_id, owner_user_id) DO NOTHING
"""

_SELECT_SUBJECT_LOCK_FOR_UPDATE_SQL = """
/* product_growth_store:select_subject_lock_for_update */
SELECT tenant_id, owner_user_id, created_at
FROM product_growth_subject_locks
WHERE tenant_id = %s AND owner_user_id = %s
FOR UPDATE
"""

_INSERT_AGENT_SQL = f"""
/* product_growth_store:insert_agent */
INSERT INTO product_growth_agents (
    id, tenant_id, owner_user_id, idempotency_key, agent_code,
    rule_version, commission_depth,
    first_order_commission_bps, repeat_order_commission_bps,
    request_sha256, metadata
) VALUES (
    %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s, %s::jsonb
)
ON CONFLICT DO NOTHING
RETURNING {_AGENT_COLUMNS}
"""

_SELECT_AGENT_BY_USER_SQL = f"""
/* product_growth_store:select_agent_by_user */
SELECT {_AGENT_COLUMNS}
FROM product_growth_agents
WHERE tenant_id = %s AND owner_user_id = %s
"""

_SELECT_AGENT_BY_USER_FOR_UPDATE_SQL = f"""
/* product_growth_store:select_agent_by_user_for_update */
SELECT {_AGENT_COLUMNS}
FROM product_growth_agents
WHERE tenant_id = %s AND owner_user_id = %s
FOR UPDATE
"""

_SELECT_AGENT_BY_ID_FOR_UPDATE_SQL = f"""
/* product_growth_store:select_agent_by_id_for_update */
SELECT {_AGENT_COLUMNS}
FROM product_growth_agents
WHERE id = %s AND tenant_id = %s
FOR UPDATE
"""

_SELECT_AGENT_BY_IDEMPOTENCY_FOR_UPDATE_SQL = f"""
/* product_growth_store:select_agent_by_idempotency_for_update */
SELECT {_AGENT_COLUMNS}
FROM product_growth_agents
WHERE tenant_id = %s AND idempotency_key = %s
FOR UPDATE
"""

_SELECT_AGENT_BY_CODE_FOR_UPDATE_SQL = f"""
/* product_growth_store:select_agent_by_code_for_update */
SELECT {_AGENT_COLUMNS}
FROM product_growth_agents
WHERE tenant_id = %s AND agent_code = %s
FOR UPDATE
"""

_INSERT_BINDING_SQL = f"""
/* product_growth_store:insert_agent_binding */
INSERT INTO product_growth_agent_bindings (
    id, tenant_id, owner_user_id, idempotency_key,
    agent_id, agent_owner_user_id, customer_user_id,
    relation_depth, source, rule_version,
    first_order_commission_bps, repeat_order_commission_bps,
    request_sha256, metadata
) VALUES (
    %s, %s, %s, %s,
    %s, %s, %s,
    %s, %s, %s,
    %s, %s, %s, %s::jsonb
)
ON CONFLICT DO NOTHING
RETURNING {_BINDING_COLUMNS}
"""

_SELECT_BINDING_BY_OWNER_SQL = f"""
/* product_growth_store:select_agent_binding_by_owner */
SELECT {_BINDING_COLUMNS}
FROM product_growth_agent_bindings
WHERE tenant_id = %s AND owner_user_id = %s
"""

_SELECT_BINDING_BY_OWNER_FOR_UPDATE_SQL = f"""
/* product_growth_store:select_agent_binding_by_owner_for_update */
SELECT {_BINDING_COLUMNS}
FROM product_growth_agent_bindings
WHERE tenant_id = %s AND owner_user_id = %s
FOR UPDATE
"""

_SELECT_BINDING_BY_IDEMPOTENCY_FOR_UPDATE_SQL = f"""
/* product_growth_store:select_agent_binding_by_idempotency_for_update */
SELECT {_BINDING_COLUMNS}
FROM product_growth_agent_bindings
WHERE tenant_id = %s AND idempotency_key = %s
FOR UPDATE
"""

_INSERT_INVITE_CODE_SQL = f"""
/* product_growth_store:insert_invite_code */
INSERT INTO product_growth_invite_codes (
    id, tenant_id, owner_user_id, idempotency_key,
    code_sha256, rule_version, request_sha256, metadata
) VALUES (
    %s, %s, %s, %s,
    %s, %s, %s, %s::jsonb
)
ON CONFLICT DO NOTHING
RETURNING {_INVITE_CODE_COLUMNS}
"""

_SELECT_INVITE_CODE_BY_IDEMPOTENCY_FOR_UPDATE_SQL = f"""
/* product_growth_store:select_invite_code_by_idempotency_for_update */
SELECT {_INVITE_CODE_COLUMNS}
FROM product_growth_invite_codes
WHERE tenant_id = %s AND idempotency_key = %s
FOR UPDATE
"""

_SELECT_INVITE_CODE_BY_OWNER_FOR_UPDATE_SQL = f"""
/* product_growth_store:select_invite_code_by_owner_for_update */
SELECT {_INVITE_CODE_COLUMNS}
FROM product_growth_invite_codes
WHERE tenant_id = %s AND owner_user_id = %s
FOR UPDATE
"""

_SELECT_INVITE_CODE_BY_DIGEST_SQL = f"""
/* product_growth_store:select_invite_code_by_digest */
SELECT {_INVITE_CODE_COLUMNS}
FROM product_growth_invite_codes
WHERE tenant_id = %s AND code_sha256 = %s
"""

_SELECT_INVITE_CODE_BY_DIGEST_FOR_UPDATE_SQL = f"""
/* product_growth_store:select_invite_code_by_digest_for_update */
SELECT {_INVITE_CODE_COLUMNS}
FROM product_growth_invite_codes
WHERE tenant_id = %s AND code_sha256 = %s
FOR UPDATE
"""

_INSERT_INVITE_SQL = f"""
/* product_growth_store:insert_invite_relation */
INSERT INTO product_growth_invite_relations (
    id, tenant_id, owner_user_id, idempotency_key,
    inviter_user_id, invitee_user_id, invite_code_id,
    invite_code_sha256,
    relation_depth, rule_version, registration_inviter_points,
    registration_invitee_points, first_recharge_rebate_percent,
    cash_points_per_yuan, cents_per_yuan, request_sha256,
    risk_snapshot, metadata
) VALUES (
    %s, %s, %s, %s,
    %s, %s, %s, %s,
    %s, %s, %s,
    %s, %s,
    %s, %s, %s,
    %s::jsonb, %s::jsonb
)
ON CONFLICT DO NOTHING
RETURNING {_INVITE_COLUMNS}
"""

_SELECT_INVITE_BY_OWNER_SQL = f"""
/* product_growth_store:select_invite_by_owner */
SELECT {_INVITE_COLUMNS}
FROM product_growth_invite_relations
WHERE tenant_id = %s AND owner_user_id = %s
"""

_SELECT_INVITE_BY_OWNER_FOR_UPDATE_SQL = f"""
/* product_growth_store:select_invite_by_owner_for_update */
SELECT {_INVITE_COLUMNS}
FROM product_growth_invite_relations
WHERE tenant_id = %s AND owner_user_id = %s
FOR UPDATE
"""

_SELECT_INVITE_BY_ID_FOR_UPDATE_SQL = f"""
/* product_growth_store:select_invite_by_id_for_update */
SELECT {_INVITE_COLUMNS}
FROM product_growth_invite_relations
WHERE id = %s AND tenant_id = %s AND owner_user_id = %s
FOR UPDATE
"""

_SELECT_INVITE_BY_IDEMPOTENCY_FOR_UPDATE_SQL = f"""
/* product_growth_store:select_invite_by_idempotency_for_update */
SELECT {_INVITE_COLUMNS}
FROM product_growth_invite_relations
WHERE tenant_id = %s AND idempotency_key = %s
FOR UPDATE
"""

_INSERT_EVENT_SQL = f"""
/* product_growth_store:insert_business_event */
INSERT INTO product_growth_business_events (
    id, tenant_id, owner_user_id, idempotency_key,
    source_event_id, business_key, event_type,
    invite_relation_id, source_order_id, rule_version,
    payload, payload_sha256, outcome, outcome_reason,
    result_payload, result_sha256, occurred_at
) VALUES (
    %s, %s, %s, %s,
    %s, %s, %s,
    %s, %s, %s,
    %s::jsonb, %s, %s, %s,
    %s::jsonb, %s, %s
)
ON CONFLICT DO NOTHING
RETURNING {_EVENT_COLUMNS}
"""

_SELECT_EVENT_BY_IDEMPOTENCY_FOR_UPDATE_SQL = f"""
/* product_growth_store:select_event_by_idempotency_for_update */
SELECT {_EVENT_COLUMNS}
FROM product_growth_business_events
WHERE tenant_id = %s
  AND owner_user_id = %s
  AND idempotency_key = %s
FOR UPDATE
"""

_SELECT_EVENT_BY_SOURCE_FOR_UPDATE_SQL = f"""
/* product_growth_store:select_event_by_source_for_update */
SELECT {_EVENT_COLUMNS}
FROM product_growth_business_events
WHERE tenant_id = %s AND source_event_id = %s
FOR UPDATE
"""

_SELECT_EVENT_BY_BUSINESS_FOR_UPDATE_SQL = f"""
/* product_growth_store:select_event_by_business_for_update */
SELECT {_EVENT_COLUMNS}
FROM product_growth_business_events
WHERE tenant_id = %s AND event_type = %s AND business_key = %s
FOR UPDATE
"""

_SELECT_APPLIED_EVENT_FOR_UPDATE_SQL = f"""
/* product_growth_store:select_applied_event_for_update */
SELECT {_EVENT_COLUMNS}
FROM product_growth_business_events
WHERE tenant_id = %s
  AND invite_relation_id = %s
  AND event_type = %s
  AND outcome = 'applied'
FOR UPDATE
"""

_SELECT_EVENT_BY_ID_SQL = f"""
/* product_growth_store:select_event_by_id */
SELECT {_EVENT_COLUMNS}
FROM product_growth_business_events
WHERE id = %s AND tenant_id = %s AND owner_user_id = %s
"""

_INSERT_GRANT_SQL = f"""
/* product_growth_store:insert_reward_grant */
INSERT INTO product_growth_reward_grants (
    id, tenant_id, owner_user_id, business_event_id,
    event_owner_user_id, invite_relation_id, reward_kind,
    points, debt_offset_points, net_wallet_points,
    wallet_order_id, rule_version, content_sha256, metadata
) VALUES (
    %s, %s, %s, %s,
    %s, %s, %s,
    %s, %s, %s,
    %s, %s, %s, %s::jsonb
)
ON CONFLICT DO NOTHING
RETURNING {_GRANT_COLUMNS}
"""

_SELECT_GRANT_BY_ID_FOR_UPDATE_SQL = f"""
/* product_growth_store:select_reward_grant_by_id_for_update */
SELECT {_GRANT_COLUMNS}
FROM product_growth_reward_grants
WHERE id = %s
FOR UPDATE
"""

_SELECT_GRANTS_BY_EVENT_SQL = f"""
/* product_growth_store:select_reward_grants_by_event */
SELECT {_GRANT_COLUMNS}
FROM product_growth_reward_grants
WHERE business_event_id = %s
  AND tenant_id = %s
  AND event_owner_user_id = %s
ORDER BY reward_kind, owner_user_id, id
"""

_LIST_GRANTS_BY_OWNER_SQL = f"""
/* product_growth_store:list_reward_grants_by_owner */
SELECT {_GRANT_COLUMNS}
FROM product_growth_reward_grants
WHERE tenant_id = %s AND owner_user_id = %s
ORDER BY created_at DESC, id DESC
LIMIT %s
"""

_SELECT_GRANT_BY_EVENT_KIND_FOR_UPDATE_SQL = f"""
/* product_growth_store:select_grant_by_event_kind_for_update */
SELECT {_GRANT_COLUMNS}
FROM product_growth_reward_grants
WHERE business_event_id = %s
  AND tenant_id = %s
  AND reward_kind = %s
FOR UPDATE
"""

_INSERT_DEBT_RECOVERY_SQL = f"""
/* product_growth_store:insert_reward_debt_recovery */
INSERT INTO product_growth_reward_debt_recoveries (
    id, tenant_id, owner_user_id, business_event_id,
    event_owner_user_id, reward_grant_id, reward_kind,
    gross_reward_points, recovered_points, net_wallet_points,
    debt_outstanding_before, debt_outstanding_after,
    rule_version, content_sha256, metadata
) VALUES (
    %s, %s, %s, %s,
    %s, %s, %s,
    %s, %s, %s,
    %s, %s,
    %s, %s, %s::jsonb
)
ON CONFLICT DO NOTHING
RETURNING {_DEBT_RECOVERY_COLUMNS}
"""

_SELECT_DEBT_RECOVERY_BY_GRANT_FOR_UPDATE_SQL = f"""
/* product_growth_store:select_reward_debt_recovery_by_grant_for_update */
SELECT {_DEBT_RECOVERY_COLUMNS}
FROM product_growth_reward_debt_recoveries
WHERE reward_grant_id = %s
FOR UPDATE
"""

_SELECT_DEBT_RECOVERIES_BY_EVENT_SQL = f"""
/* product_growth_store:select_reward_debt_recoveries_by_event */
SELECT {_DEBT_RECOVERY_COLUMNS}
FROM product_growth_reward_debt_recoveries
WHERE business_event_id = %s
  AND tenant_id = %s
  AND event_owner_user_id = %s
ORDER BY reward_kind, owner_user_id, id
"""

_LIST_DEBT_RECOVERIES_BY_OWNER_SQL = f"""
/* product_growth_store:list_reward_debt_recoveries_by_owner */
SELECT {_DEBT_RECOVERY_COLUMNS}
FROM product_growth_reward_debt_recoveries
WHERE tenant_id = %s AND owner_user_id = %s
ORDER BY created_at DESC, id DESC
LIMIT %s
"""

_INSERT_REVERSAL_STATE_SQL = f"""
/* product_growth_store:insert_reversal_state */
INSERT INTO product_growth_reward_reversal_states (
    id, tenant_id, owner_user_id, invite_relation_id,
    source_order_id, original_business_event_id,
    original_reward_grant_id, reward_owner_user_id,
    original_paid_cents, original_reward_points
) VALUES (
    %s, %s, %s, %s,
    %s, %s,
    %s, %s,
    %s, %s
)
ON CONFLICT DO NOTHING
RETURNING {_REVERSAL_STATE_COLUMNS}
"""

_SELECT_REVERSAL_STATE_FOR_UPDATE_SQL = f"""
/* product_growth_store:select_reversal_state_for_update */
SELECT {_REVERSAL_STATE_COLUMNS}
FROM product_growth_reward_reversal_states
WHERE tenant_id = %s
  AND owner_user_id = %s
  AND source_order_id = %s
FOR UPDATE
"""

_UPDATE_REVERSAL_STATE_SQL = f"""
/* product_growth_store:update_reversal_state */
UPDATE product_growth_reward_reversal_states
SET max_cumulative_refunded_cents = %s,
    reversed_points = %s,
    recovered_points = %s,
    outstanding_points = %s,
    version = version + 1,
    updated_at = CURRENT_TIMESTAMP
WHERE id = %s
  AND tenant_id = %s
  AND owner_user_id = %s
  AND version = %s
  AND max_cumulative_refunded_cents = %s
  AND reversed_points = %s
  AND recovered_points = %s
  AND outstanding_points = %s
RETURNING {_REVERSAL_STATE_COLUMNS}
"""

_INSERT_REWARD_DEBT_SQL = f"""
/* product_growth_store:insert_reward_debt */
INSERT INTO product_growth_reward_debts (tenant_id, owner_user_id)
VALUES (%s, %s)
ON CONFLICT (tenant_id, owner_user_id) DO NOTHING
RETURNING {_DEBT_COLUMNS}
"""

_SELECT_REWARD_DEBT_FOR_UPDATE_SQL = f"""
/* product_growth_store:select_reward_debt_for_update */
SELECT {_DEBT_COLUMNS}
FROM product_growth_reward_debts
WHERE tenant_id = %s AND owner_user_id = %s
FOR UPDATE
"""

_SELECT_REWARD_DEBT_SQL = f"""
/* product_growth_store:select_reward_debt */
SELECT {_DEBT_COLUMNS}
FROM product_growth_reward_debts
WHERE tenant_id = %s AND owner_user_id = %s
"""

_UPDATE_REWARD_DEBT_SQL = f"""
/* product_growth_store:update_reward_debt */
UPDATE product_growth_reward_debts
SET outstanding_points = outstanding_points + %s,
    lifetime_assessed_points = lifetime_assessed_points + %s,
    lifetime_recovered_points = lifetime_recovered_points + %s,
    version = version + 1,
    updated_at = CURRENT_TIMESTAMP
WHERE tenant_id = %s
  AND owner_user_id = %s
  AND version = %s
  AND outstanding_points = %s
  AND lifetime_assessed_points = %s
  AND lifetime_recovered_points = %s
RETURNING {_DEBT_COLUMNS}
"""

_RECOVER_REWARD_DEBT_SQL = f"""
/* product_growth_store:recover_reward_debt */
UPDATE product_growth_reward_debts
SET outstanding_points = outstanding_points - %s,
    lifetime_recovered_points = lifetime_recovered_points + %s,
    version = version + 1,
    updated_at = CURRENT_TIMESTAMP
WHERE tenant_id = %s
  AND owner_user_id = %s
  AND version = %s
  AND outstanding_points = %s
  AND lifetime_assessed_points = %s
  AND lifetime_recovered_points = %s
  AND outstanding_points >= %s
RETURNING {_DEBT_COLUMNS}
"""

_INSERT_REVERSAL_SQL = f"""
/* product_growth_store:insert_reward_reversal */
INSERT INTO product_growth_reward_reversals (
    id, tenant_id, owner_user_id, business_event_id,
    event_owner_user_id, reversal_state_id,
    original_reward_grant_id, reversal_kind,
    assessed_points, recovered_points,
    outstanding_points_added, outstanding_points_after,
    wallet_order_id, cumulative_refunded_cents,
    cumulative_reversed_points, rule_version,
    content_sha256, metadata
) VALUES (
    %s, %s, %s, %s,
    %s, %s,
    %s, %s,
    %s, %s,
    %s, %s,
    %s, %s,
    %s, %s,
    %s, %s::jsonb
)
ON CONFLICT DO NOTHING
RETURNING {_REVERSAL_COLUMNS}
"""

_SELECT_REVERSAL_BY_EVENT_FOR_UPDATE_SQL = f"""
/* product_growth_store:select_reversal_by_event_for_update */
SELECT {_REVERSAL_COLUMNS}
FROM product_growth_reward_reversals
WHERE tenant_id = %s AND business_event_id = %s
FOR UPDATE
"""

_LIST_REVERSALS_BY_OWNER_SQL = f"""
/* product_growth_store:list_reward_reversals_by_owner */
SELECT {_REVERSAL_COLUMNS}
FROM product_growth_reward_reversals
WHERE tenant_id = %s AND owner_user_id = %s
ORDER BY created_at DESC, id DESC
LIMIT %s
"""

_INSERT_AUDIT_SQL = f"""
/* product_growth_store:insert_audit */
INSERT INTO product_growth_audit_events (
    action_id, tenant_id, owner_user_id, actor_user_id,
    action, target_type, target_id, status, metadata,
    content_sha256
) VALUES (
    %s, %s, %s, %s,
    %s, %s, %s, %s, %s::jsonb,
    %s
)
ON CONFLICT DO NOTHING
RETURNING {_AUDIT_COLUMNS}
"""

_SELECT_AUDIT_FOR_UPDATE_SQL = f"""
/* product_growth_store:select_audit_for_update */
SELECT {_AUDIT_COLUMNS}
FROM product_growth_audit_events
WHERE action_id = %s AND tenant_id = %s
FOR UPDATE
"""

_INSERT_POINT_ACCOUNT_SQL = f"""
/* product_growth_store:insert_point_account */
INSERT INTO product_point_accounts (owner_user_id, metadata)
VALUES (%s, '{{}}'::jsonb)
ON CONFLICT (owner_user_id) DO NOTHING
RETURNING {_ACCOUNT_COLUMNS}
"""

_SELECT_POINT_ACCOUNT_FOR_UPDATE_SQL = f"""
/* product_growth_store:select_point_account_for_update */
SELECT {_ACCOUNT_COLUMNS}
FROM product_point_accounts
WHERE owner_user_id = %s
FOR UPDATE
"""

_INSERT_POINT_ORDER_SQL = f"""
/* product_growth_store:insert_point_order */
INSERT INTO product_point_orders (
    id, owner_user_id, order_kind, points,
    source_order_id, source_order_kind, job_id,
    request_sha256, metadata
) VALUES (
    %s, %s, 'credit', %s,
    NULL, NULL, NULL,
    %s, %s::jsonb
)
ON CONFLICT (id) DO NOTHING
RETURNING {_POINT_ORDER_COLUMNS}
"""

_INSERT_POINT_DEBIT_ORDER_SQL = f"""
/* product_growth_store:insert_point_debit_order */
INSERT INTO product_point_orders (
    id, owner_user_id, order_kind, points,
    source_order_id, source_order_kind, job_id,
    request_sha256, metadata
) VALUES (
    %s, %s, 'debit', %s,
    NULL, NULL, NULL,
    %s, %s::jsonb
)
ON CONFLICT (id) DO NOTHING
RETURNING {_POINT_ORDER_COLUMNS}
"""

_SELECT_POINT_ORDER_FOR_UPDATE_SQL = f"""
/* product_growth_store:select_point_order_for_update */
SELECT {_POINT_ORDER_COLUMNS}
FROM product_point_orders
WHERE id = %s
FOR UPDATE
"""

_CREDIT_POINT_ACCOUNT_SQL = f"""
/* product_growth_store:credit_point_account */
UPDATE product_point_accounts
SET balance_points = balance_points + %s,
    lifetime_credited_points = lifetime_credited_points + %s,
    version = version + 1,
    updated_at = CURRENT_TIMESTAMP
WHERE owner_user_id = %s
RETURNING {_ACCOUNT_COLUMNS}
"""

_DEBIT_POINT_ACCOUNT_SQL = f"""
/* product_growth_store:debit_point_account */
UPDATE product_point_accounts
SET balance_points = balance_points - %s,
    lifetime_debited_points = lifetime_debited_points + %s,
    version = version + 1,
    updated_at = CURRENT_TIMESTAMP
WHERE owner_user_id = %s
  AND balance_points >= %s
RETURNING {_ACCOUNT_COLUMNS}
"""

_INSERT_POINT_LEDGER_SQL = f"""
/* product_growth_store:insert_point_ledger */
INSERT INTO product_point_ledger (
    owner_user_id, order_id, order_kind, points,
    delta_points, balance_after_points, request_sha256,
    metadata
) VALUES (
    %s, %s, 'credit', %s,
    %s, %s, %s,
    %s::jsonb
)
RETURNING {_POINT_LEDGER_COLUMNS}
"""

_INSERT_POINT_DEBIT_LEDGER_SQL = f"""
/* product_growth_store:insert_point_debit_ledger */
INSERT INTO product_point_ledger (
    owner_user_id, order_id, order_kind, points,
    delta_points, balance_after_points, request_sha256,
    metadata
) VALUES (
    %s, %s, 'debit', %s,
    %s, %s, %s,
    %s::jsonb
)
RETURNING {_POINT_LEDGER_COLUMNS}
"""

_SELECT_POINT_LEDGER_FOR_UPDATE_SQL = f"""
/* product_growth_store:select_point_ledger_for_update */
SELECT {_POINT_LEDGER_COLUMNS}
FROM product_point_ledger
WHERE order_id = %s
FOR UPDATE
"""

_SELECT_PAYMENT_ORDER_FOR_UPDATE_SQL = f"""
/* product_growth_store:select_payment_order_for_update */
SELECT {_PAYMENT_ORDER_COLUMNS}
FROM product_payment_orders
WHERE id = %s AND owner_user_id = %s
FOR UPDATE
"""

_SELECT_FIRST_PAYMENT_ORDER_FOR_UPDATE_SQL = f"""
/* product_growth_store:select_first_payment_order_for_update */
SELECT {_PAYMENT_ORDER_COLUMNS}
FROM product_payment_orders
WHERE owner_user_id = %s
  AND status IN ('paid', 'partially_refunded', 'refunded')
  AND paid_at IS NOT NULL
ORDER BY paid_at, created_at, id
LIMIT 1
FOR UPDATE
"""


def stable_growth_id(prefix: str, tenant_id: str, *identity: str) -> str:
    clean_prefix = _id_prefix(prefix)
    tenant = _tenant_id(tenant_id)
    if not identity:
        raise InvalidProductGrowthInput("at least one identity value is required")
    parts = [_identity_value(value, "identity") for value in identity]
    digest = hashlib.sha256(
        _canonical_json(
            {
                "prefix": clean_prefix,
                "tenant_id": tenant,
                "identity": parts,
            }
        ).encode("utf-8")
    ).hexdigest()
    return f"{clean_prefix}_{digest[:40]}"


def create_or_get_agent(
    cursor: CursorLike,
    *,
    tenant_id: str,
    owner_user_id: str,
    idempotency_key: str,
    agent_code: str = "",
    metadata: Mapping[str, Any] | None = None,
    actor_user_id: str | None = None,
    rule_version: str = growth_rules.GROWTH_RULE_VERSION,
) -> GrowthCreateResult:
    """Create one tenant-scoped agent profile without committing."""

    normalized = _normalize_agent(
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        idempotency_key=idempotency_key,
        agent_code=agent_code,
        metadata=metadata,
        rule_version=rule_version,
    )
    actor = _identifier(
        actor_user_id or normalized["owner_user_id"],
        "actor_user_id",
    )
    _lock_subject(
        cursor,
        normalized["tenant_id"],
        normalized["owner_user_id"],
    )
    cursor.execute(
        _INSERT_AGENT_SQL,
        (
            normalized["id"],
            normalized["tenant_id"],
            normalized["owner_user_id"],
            normalized["idempotency_key"],
            normalized["agent_code"],
            normalized["rule_version"],
            normalized["commission_depth"],
            normalized["first_order_commission_bps"],
            normalized["repeat_order_commission_bps"],
            normalized["request_sha256"],
            _canonical_json(normalized["metadata"]),
        ),
    )
    inserted = _fetchone_dict(cursor)
    if inserted is not None:
        record = _decode_record(inserted)
        _append_audit(
            cursor,
            tenant_id=record["tenant_id"],
            owner_user_id=record["owner_user_id"],
            actor_user_id=actor,
            action="growth.agent.created",
            target_type="growth_agent",
            target_id=record["id"],
            status="succeeded",
            metadata={
                "ruleVersion": record["rule_version"],
                "commissionDepth": record["commission_depth"],
            },
        )
        return GrowthCreateResult(record=record, created=True)

    cursor.execute(
        _SELECT_AGENT_BY_IDEMPOTENCY_FOR_UPDATE_SQL,
        (normalized["tenant_id"], normalized["idempotency_key"]),
    )
    existing = _fetchone_dict(cursor)
    if existing is not None:
        record = _decode_record(existing)
        _require_exact_request_replay(record, normalized, "agent")
        return GrowthCreateResult(record=record, created=False)

    cursor.execute(
        _SELECT_AGENT_BY_USER_FOR_UPDATE_SQL,
        (normalized["tenant_id"], normalized["owner_user_id"]),
    )
    existing = _fetchone_dict(cursor)
    if existing is not None:
        record = _decode_record(existing)
        _require_logical_agent_replay(record, normalized)
        return GrowthCreateResult(record=record, created=False)

    cursor.execute(
        _SELECT_AGENT_BY_CODE_FOR_UPDATE_SQL,
        (normalized["tenant_id"], normalized["agent_code"]),
    )
    if _fetchone_dict(cursor) is not None:
        raise ProductGrowthConflict(
            "agent code already belongs to another tenant user"
        )
    raise ProductGrowthConflict(
        "agent insert conflicted without a resolvable tenant owner"
    )


def create_agent_profile(
    cursor: CursorLike,
    **kwargs: Any,
) -> GrowthCreateResult:
    """Compatibility name for the cursor-owned agent creation API."""

    return create_or_get_agent(cursor, **kwargs)


def bind_agent_customer(
    cursor: CursorLike,
    *,
    tenant_id: str,
    owner_user_id: str,
    agent_id: str,
    idempotency_key: str,
    source: str = "direct",
    metadata: Mapping[str, Any] | None = None,
    actor_user_id: str | None = None,
) -> GrowthCreateResult:
    """Bind one customer to one direct agent inside the caller transaction."""

    tenant = _tenant_id(tenant_id)
    owner = _identifier(owner_user_id, "owner_user_id")
    agent_identifier = _growth_id(agent_id, "growth_agent", "agent_id")
    idem = _identifier(idempotency_key, "idempotency_key")
    clean_source = _identifier(source, "source")
    clean_metadata = _json_object(metadata or {}, "metadata")
    actor = _identifier(actor_user_id or owner, "actor_user_id")

    _lock_subject(cursor, tenant, owner)
    cursor.execute(
        _SELECT_AGENT_BY_ID_FOR_UPDATE_SQL,
        (agent_identifier, tenant),
    )
    raw_agent = _fetchone_dict(cursor)
    if raw_agent is None:
        raise ProductGrowthNotFound("agent not found in tenant")
    agent = _decode_record(raw_agent)
    if str(agent["status"]) != "active":
        raise ProductGrowthStateConflict("agent is not active")
    if hmac.compare_digest(str(agent["owner_user_id"]), owner):
        raise ProductGrowthConflict("agent cannot bind itself as a customer")

    binding_id = stable_growth_id("growth_binding", tenant, owner)
    content = {
        "id": binding_id,
        "tenant_id": tenant,
        "owner_user_id": owner,
        "idempotency_key": idem,
        "agent_id": agent_identifier,
        "agent_owner_user_id": str(agent["owner_user_id"]),
        "customer_user_id": owner,
        "relation_depth": 1,
        "source": clean_source,
        "rule_version": str(agent["rule_version"]),
        "first_order_commission_bps": int(
            agent["first_order_commission_bps"]
        ),
        "repeat_order_commission_bps": int(
            agent["repeat_order_commission_bps"]
        ),
        "metadata": clean_metadata,
    }
    content["request_sha256"] = _content_sha256(content)
    cursor.execute(
        _INSERT_BINDING_SQL,
        (
            content["id"],
            content["tenant_id"],
            content["owner_user_id"],
            content["idempotency_key"],
            content["agent_id"],
            content["agent_owner_user_id"],
            content["customer_user_id"],
            content["relation_depth"],
            content["source"],
            content["rule_version"],
            content["first_order_commission_bps"],
            content["repeat_order_commission_bps"],
            content["request_sha256"],
            _canonical_json(content["metadata"]),
        ),
    )
    inserted = _fetchone_dict(cursor)
    if inserted is not None:
        record = _decode_record(inserted)
        _append_audit(
            cursor,
            tenant_id=tenant,
            owner_user_id=owner,
            actor_user_id=actor,
            action="growth.agent_customer.bound",
            target_type="growth_agent_binding",
            target_id=record["id"],
            status="succeeded",
            metadata={
                "agentId": record["agent_id"],
                "ruleVersion": record["rule_version"],
                "relationDepth": 1,
            },
        )
        return GrowthCreateResult(record=record, created=True)

    cursor.execute(
        _SELECT_BINDING_BY_IDEMPOTENCY_FOR_UPDATE_SQL,
        (tenant, idem),
    )
    existing = _fetchone_dict(cursor)
    if existing is not None:
        record = _decode_record(existing)
        _require_exact_request_replay(record, content, "agent binding")
        return GrowthCreateResult(record=record, created=False)

    cursor.execute(_SELECT_BINDING_BY_OWNER_FOR_UPDATE_SQL, (tenant, owner))
    existing = _fetchone_dict(cursor)
    if existing is not None:
        record = _decode_record(existing)
        _require_logical_binding_replay(record, content)
        return GrowthCreateResult(record=record, created=False)
    raise ProductGrowthConflict(
        "agent binding insert conflicted without a resolvable tenant owner"
    )


def issue_consumer_invite_code(
    cursor: CursorLike,
    *,
    tenant_id: str,
    owner_user_id: str,
    idempotency_key: str,
    plaintext_code: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    actor_user_id: str | None = None,
    rule_version: str = growth_rules.GROWTH_RULE_VERSION,
) -> InviteCodeIssueResult:
    """Issue one tenant-scoped code, returning plaintext only on creation."""

    tenant = _tenant_id(tenant_id)
    owner = _identifier(owner_user_id, "owner_user_id")
    idem = _identifier(idempotency_key, "idempotency_key")
    actor = _identifier(actor_user_id or owner, "actor_user_id")
    version = _current_rule_version(rule_version)
    clean_metadata = _bounded_json_object(metadata or {}, "metadata")
    supplied_code = (
        None
        if plaintext_code is None
        else _invite_code_plaintext(plaintext_code)
    )
    supplied_digest = (
        None
        if supplied_code is None
        else hashlib.sha256(supplied_code.encode("utf-8")).hexdigest()
    )

    _lock_subject(cursor, tenant, owner)
    cursor.execute(
        _SELECT_INVITE_CODE_BY_IDEMPOTENCY_FOR_UPDATE_SQL,
        (tenant, idem),
    )
    existing = _fetchone_dict(cursor)
    if existing is not None:
        record = _decode_record(existing)
        _validate_invite_code_replay(
            record,
            owner_user_id=owner,
            idempotency_key=idem,
            supplied_digest=supplied_digest,
            rule_version=version,
            metadata=clean_metadata,
        )
        return InviteCodeIssueResult(
            record=record,
            created=False,
            plaintext_code=None,
        )

    cursor.execute(
        _SELECT_INVITE_CODE_BY_OWNER_FOR_UPDATE_SQL,
        (tenant, owner),
    )
    existing = _fetchone_dict(cursor)
    if existing is not None:
        record = _decode_record(existing)
        _validate_invite_code_logical_replay(
            record,
            supplied_digest=supplied_digest,
            rule_version=version,
            metadata=clean_metadata,
        )
        return InviteCodeIssueResult(
            record=record,
            created=False,
            plaintext_code=None,
        )

    code = supplied_code or secrets.token_urlsafe(24)
    code_digest = hashlib.sha256(code.encode("utf-8")).hexdigest()
    code_id = stable_growth_id("growth_invite_code", tenant, owner)
    content = {
        "id": code_id,
        "tenant_id": tenant,
        "owner_user_id": owner,
        "idempotency_key": idem,
        "code_sha256": code_digest,
        "rule_version": version,
        "metadata": clean_metadata,
    }
    content["request_sha256"] = _content_sha256(content)
    cursor.execute(
        _INSERT_INVITE_CODE_SQL,
        (
            content["id"],
            content["tenant_id"],
            content["owner_user_id"],
            content["idempotency_key"],
            content["code_sha256"],
            content["rule_version"],
            content["request_sha256"],
            _canonical_json(content["metadata"]),
        ),
    )
    inserted = _fetchone_dict(cursor)
    if inserted is None:
        cursor.execute(
            _SELECT_INVITE_CODE_BY_IDEMPOTENCY_FOR_UPDATE_SQL,
            (tenant, idem),
        )
        existing = _fetchone_dict(cursor)
        if existing is not None:
            record = _decode_record(existing)
            _validate_invite_code_replay(
                record,
                owner_user_id=owner,
                idempotency_key=idem,
                supplied_digest=code_digest,
                rule_version=version,
                metadata=clean_metadata,
            )
            return InviteCodeIssueResult(record, False, None)
        cursor.execute(
            _SELECT_INVITE_CODE_BY_DIGEST_FOR_UPDATE_SQL,
            (tenant, code_digest),
        )
        if _fetchone_dict(cursor) is not None:
            raise ProductGrowthConflict(
                "invite code already belongs to another tenant user"
            )
        raise ProductGrowthConflict(
            "invite code insert conflicted without a resolvable owner"
        )

    record = _decode_record(inserted)
    _append_audit(
        cursor,
        tenant_id=tenant,
        owner_user_id=owner,
        actor_user_id=actor,
        action="growth.consumer_invite_code.issued",
        target_type="growth_invite_code",
        target_id=record["id"],
        status="succeeded",
        metadata={"ruleVersion": record["rule_version"]},
    )
    return InviteCodeIssueResult(
        record=record,
        created=True,
        plaintext_code=code,
    )


def resolve_consumer_invite_code(
    cursor: CursorLike,
    *,
    tenant_id: str,
    invite_code: str,
) -> dict[str, Any] | None:
    """Resolve an inviter only through tenant-scoped server state."""

    tenant = _tenant_id(tenant_id)
    code = _invite_code_plaintext(invite_code)
    digest = hashlib.sha256(code.encode("utf-8")).hexdigest()
    cursor.execute(_SELECT_INVITE_CODE_BY_DIGEST_SQL, (tenant, digest))
    row = _fetchone_dict(cursor)
    return None if row is None else _decode_record(row)


def accept_consumer_invite(
    cursor: CursorLike,
    *,
    tenant_id: str,
    owner_user_id: str,
    idempotency_key: str,
    invite_code: str,
    inviter_user_id: str | None = None,
    risk_snapshot: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
    actor_user_id: str | None = None,
    rule_version: str = growth_rules.GROWTH_RULE_VERSION,
) -> GrowthCreateResult:
    """Accept a direct invite after resolving its server-owned code."""

    tenant = _tenant_id(tenant_id)
    owner = _identifier(owner_user_id, "owner_user_id")
    idem = _identifier(idempotency_key, "idempotency_key")
    code = _invite_code_plaintext(invite_code)
    code_digest = hashlib.sha256(code.encode("utf-8")).hexdigest()
    supplied_inviter = (
        None
        if inviter_user_id is None
        else _identifier(inviter_user_id, "inviter_user_id")
    )
    actor = _identifier(
        actor_user_id or owner,
        "actor_user_id",
    )
    _lock_subject(cursor, tenant, owner)
    cursor.execute(
        _SELECT_INVITE_CODE_BY_DIGEST_FOR_UPDATE_SQL,
        (tenant, code_digest),
    )
    code_row = _fetchone_dict(cursor)
    if code_row is None:
        raise ProductGrowthNotFound("invite code not found in tenant")
    code_record = _decode_record(code_row)
    resolved_inviter = str(code_record["owner_user_id"])
    if (
        supplied_inviter is not None
        and not hmac.compare_digest(supplied_inviter, resolved_inviter)
    ):
        raise ProductGrowthConflict(
            "supplied inviter_user_id does not match the invite code owner"
        )
    normalized = _normalize_invite(
        tenant_id=tenant,
        owner_user_id=owner,
        idempotency_key=idem,
        invite_code=code_record,
        risk_snapshot=risk_snapshot,
        metadata=metadata,
        rule_version=rule_version,
    )
    cursor.execute(
        _INSERT_INVITE_SQL,
        (
            normalized["id"],
            normalized["tenant_id"],
            normalized["owner_user_id"],
            normalized["idempotency_key"],
            normalized["inviter_user_id"],
            normalized["invitee_user_id"],
            normalized["invite_code_id"],
            normalized["invite_code_sha256"],
            normalized["relation_depth"],
            normalized["rule_version"],
            normalized["registration_inviter_points"],
            normalized["registration_invitee_points"],
            normalized["first_recharge_rebate_percent"],
            normalized["cash_points_per_yuan"],
            normalized["cents_per_yuan"],
            normalized["request_sha256"],
            _canonical_json(normalized["risk_snapshot"]),
            _canonical_json(normalized["metadata"]),
        ),
    )
    inserted = _fetchone_dict(cursor)
    if inserted is not None:
        record = _decode_record(inserted)
        _append_audit(
            cursor,
            tenant_id=record["tenant_id"],
            owner_user_id=record["owner_user_id"],
            actor_user_id=actor,
            action="growth.consumer_invite.accepted",
            target_type="growth_invite_relation",
            target_id=record["id"],
            status="succeeded",
            metadata={
                "inviterUserId": record["inviter_user_id"],
                "ruleVersion": record["rule_version"],
                "relationDepth": 1,
            },
        )
        return GrowthCreateResult(record=record, created=True)

    cursor.execute(
        _SELECT_INVITE_BY_IDEMPOTENCY_FOR_UPDATE_SQL,
        (normalized["tenant_id"], normalized["idempotency_key"]),
    )
    existing = _fetchone_dict(cursor)
    if existing is not None:
        record = _decode_record(existing)
        _require_exact_request_replay(record, normalized, "invite relation")
        return GrowthCreateResult(record=record, created=False)

    cursor.execute(
        _SELECT_INVITE_BY_OWNER_FOR_UPDATE_SQL,
        (normalized["tenant_id"], normalized["owner_user_id"]),
    )
    existing = _fetchone_dict(cursor)
    if existing is not None:
        record = _decode_record(existing)
        _require_logical_invite_replay(record, normalized)
        return GrowthCreateResult(record=record, created=False)
    raise ProductGrowthConflict(
        "invite insert conflicted without a resolvable tenant owner"
    )


def get_agent_by_user(
    cursor: CursorLike,
    *,
    tenant_id: str,
    owner_user_id: str,
) -> dict[str, Any] | None:
    tenant = _tenant_id(tenant_id)
    owner = _identifier(owner_user_id, "owner_user_id")
    cursor.execute(_SELECT_AGENT_BY_USER_SQL, (tenant, owner))
    row = _fetchone_dict(cursor)
    return None if row is None else _decode_record(row)


def get_agent_binding(
    cursor: CursorLike,
    *,
    tenant_id: str,
    owner_user_id: str,
) -> dict[str, Any] | None:
    tenant = _tenant_id(tenant_id)
    owner = _identifier(owner_user_id, "owner_user_id")
    cursor.execute(_SELECT_BINDING_BY_OWNER_SQL, (tenant, owner))
    row = _fetchone_dict(cursor)
    return None if row is None else _decode_record(row)


def get_binding_for_customer(
    cursor: CursorLike,
    **kwargs: Any,
) -> dict[str, Any] | None:
    return get_agent_binding(cursor, **kwargs)


def get_invite_relation(
    cursor: CursorLike,
    *,
    tenant_id: str,
    owner_user_id: str,
) -> dict[str, Any] | None:
    tenant = _tenant_id(tenant_id)
    owner = _identifier(owner_user_id, "owner_user_id")
    cursor.execute(_SELECT_INVITE_BY_OWNER_SQL, (tenant, owner))
    row = _fetchone_dict(cursor)
    return None if row is None else _decode_record(row)


def get_invite_for_invitee(
    cursor: CursorLike,
    **kwargs: Any,
) -> dict[str, Any] | None:
    return get_invite_relation(cursor, **kwargs)


def get_business_event(
    cursor: CursorLike,
    *,
    event_id: str,
    tenant_id: str,
    owner_user_id: str,
) -> dict[str, Any] | None:
    identifier = _growth_id(event_id, "growth_event", "event_id")
    tenant = _tenant_id(tenant_id)
    owner = _identifier(owner_user_id, "owner_user_id")
    cursor.execute(_SELECT_EVENT_BY_ID_SQL, (identifier, tenant, owner))
    row = _fetchone_dict(cursor)
    return None if row is None else _decode_record(row)


def list_reward_grants(
    cursor: CursorLike,
    *,
    tenant_id: str,
    owner_user_id: str,
    limit: int = 100,
) -> list[dict[str, Any]]:
    tenant = _tenant_id(tenant_id)
    owner = _identifier(owner_user_id, "owner_user_id")
    clean_limit = _bounded_positive_int(
        limit,
        "limit",
        maximum=MAX_LIST_LIMIT,
    )
    cursor.execute(_LIST_GRANTS_BY_OWNER_SQL, (tenant, owner, clean_limit))
    return [_decode_record(row) for row in _fetchall_dicts(cursor)]


def list_reward_reversals(
    cursor: CursorLike,
    *,
    tenant_id: str,
    owner_user_id: str,
    limit: int = 100,
) -> list[dict[str, Any]]:
    tenant = _tenant_id(tenant_id)
    owner = _identifier(owner_user_id, "owner_user_id")
    clean_limit = _bounded_positive_int(
        limit,
        "limit",
        maximum=MAX_LIST_LIMIT,
    )
    cursor.execute(
        _LIST_REVERSALS_BY_OWNER_SQL,
        (tenant, owner, clean_limit),
    )
    return [_decode_record(row) for row in _fetchall_dicts(cursor)]


def list_reward_debt_recoveries(
    cursor: CursorLike,
    *,
    tenant_id: str,
    owner_user_id: str,
    limit: int = 100,
) -> list[dict[str, Any]]:
    tenant = _tenant_id(tenant_id)
    owner = _identifier(owner_user_id, "owner_user_id")
    clean_limit = _bounded_positive_int(
        limit,
        "limit",
        maximum=MAX_LIST_LIMIT,
    )
    cursor.execute(
        _LIST_DEBT_RECOVERIES_BY_OWNER_SQL,
        (tenant, owner, clean_limit),
    )
    return [_decode_record(row) for row in _fetchall_dicts(cursor)]


def get_reward_debt(
    cursor: CursorLike,
    *,
    tenant_id: str,
    owner_user_id: str,
) -> dict[str, Any] | None:
    tenant = _tenant_id(tenant_id)
    owner = _identifier(owner_user_id, "owner_user_id")
    cursor.execute(_SELECT_REWARD_DEBT_SQL, (tenant, owner))
    row = _fetchone_dict(cursor)
    return None if row is None else _decode_record(row)


def process_registration_reward(
    cursor: CursorLike,
    *,
    tenant_id: str,
    owner_user_id: str,
    invite_relation_id: str,
    source_event_id: str,
    idempotency_key: str,
    source_payload: Mapping[str, Any] | None = None,
    occurred_at: datetime | str | None = None,
    actor_user_id: str = DEFAULT_EVENT_ACTOR,
) -> GrowthEventApplyResult:
    """Apply inviter and invitee registration credits without committing.

    The caller can mark the claimed growth outbox row succeeded on this same
    cursor. A later commit then publishes the wallet effects and the outbox
    terminal state atomically.
    """

    tenant = _tenant_id(tenant_id)
    owner = _identifier(owner_user_id, "owner_user_id")
    relation_id = _growth_id(
        invite_relation_id,
        "growth_invite",
        "invite_relation_id",
    )
    source_id = _identifier(source_event_id, "source_event_id")
    idem = _identifier(idempotency_key, "idempotency_key")
    actor = _identifier(actor_user_id, "actor_user_id")
    clean_source_payload = _bounded_json_object(
        source_payload or {},
        "source_payload",
    )

    _lock_subject(cursor, tenant, owner)
    invite = _locked_invite(cursor, relation_id, tenant, owner)
    event_time = _event_timestamp(occurred_at, fallback=invite["created_at"])
    event_id = stable_growth_id(
        "growth_event",
        tenant,
        EVENT_REGISTRATION_REWARD,
        relation_id,
    )
    grant_specs = (
        _grant_spec(
            tenant_id=tenant,
            event_id=event_id,
            owner_user_id=str(invite["inviter_user_id"]),
            reward_kind="registration_inviter",
            points=int(invite["registration_inviter_points"]),
        ),
        _grant_spec(
            tenant_id=tenant,
            event_id=event_id,
            owner_user_id=owner,
            reward_kind="registration_invitee",
            points=int(invite["registration_invitee_points"]),
        ),
    )
    expected = _event_record(
        event_id=event_id,
        tenant_id=tenant,
        owner_user_id=owner,
        idempotency_key=idem,
        source_event_id=source_id,
        business_key=relation_id,
        event_type=EVENT_REGISTRATION_REWARD,
        invite=invite,
        source_order_id="",
        source_payload=clean_source_payload,
        outcome="applied",
        outcome_reason="",
        occurred_at=event_time,
        grant_specs=grant_specs,
        canonical_first_order_id="",
        paid_cents=0,
    )
    return _persist_event_and_effects(
        cursor,
        expected=expected,
        invite=invite,
        grant_specs=grant_specs,
        actor_user_id=actor,
    )


def process_first_recharge_reward(
    cursor: CursorLike,
    *,
    tenant_id: str,
    owner_user_id: str,
    invite_relation_id: str,
    source_event_id: str,
    source_order_id: str,
    paid_cents: int,
    idempotency_key: str,
    source_payload: Mapping[str, Any] | None = None,
    actor_user_id: str = DEFAULT_EVENT_ACTOR,
) -> GrowthEventApplyResult:
    """Apply only the chronologically first paid-order referral reward.

    Processing order is not trusted. The current and canonical first payment
    rows are locked and compared by paid_at, created_at, then order ID.
    """

    tenant = _tenant_id(tenant_id)
    owner = _identifier(owner_user_id, "owner_user_id")
    relation_id = _growth_id(
        invite_relation_id,
        "growth_invite",
        "invite_relation_id",
    )
    source_id = _identifier(source_event_id, "source_event_id")
    order_id = _identifier(source_order_id, "source_order_id")
    expected_paid_cents = _positive_int(paid_cents, "paid_cents")
    idem = _identifier(idempotency_key, "idempotency_key")
    actor = _identifier(actor_user_id, "actor_user_id")
    clean_source_payload = _bounded_json_object(
        source_payload or {},
        "source_payload",
    )

    _lock_subject(cursor, tenant, owner)
    invite = _locked_invite(cursor, relation_id, tenant, owner)
    payment = _locked_payment_order(cursor, order_id, owner)
    if int(payment["amount_cents"]) != expected_paid_cents:
        raise ProductGrowthConflict(
            "paid_cents does not match the durable payment order"
        )
    cursor.execute(_SELECT_FIRST_PAYMENT_ORDER_FOR_UPDATE_SQL, (owner,))
    first_row = _fetchone_dict(cursor)
    if first_row is None:
        raise ProductGrowthIntegrityError(
            "paid payment order has no canonical first-payment row"
        )
    first_payment = _decode_record(first_row)
    canonical_first_order_id = str(first_payment["id"])
    is_first = hmac.compare_digest(canonical_first_order_id, order_id)
    event_id = stable_growth_id(
        "growth_event",
        tenant,
        EVENT_FIRST_RECHARGE_REWARD,
        order_id,
    )

    if is_first:
        inviter_points = _first_recharge_points(
            paid_cents=expected_paid_cents,
            invite=invite,
        )
        if inviter_points > 0:
            grant_specs = (
                _grant_spec(
                    tenant_id=tenant,
                    event_id=event_id,
                    owner_user_id=str(invite["inviter_user_id"]),
                    reward_kind="first_recharge_inviter",
                    points=inviter_points,
                ),
            )
            outcome = "applied"
            reason = ""
            cursor.execute(
                _SELECT_APPLIED_EVENT_FOR_UPDATE_SQL,
                (tenant, relation_id, EVENT_FIRST_RECHARGE_REWARD),
            )
            applied_row = _fetchone_dict(cursor)
            if applied_row is not None:
                applied = _decode_record(applied_row)
                if str(applied["source_order_id"]) != order_id:
                    raise ProductGrowthIntegrityError(
                        "another payment order already owns the "
                        "first-recharge reward"
                    )
        else:
            grant_specs = ()
            outcome = "ignored"
            reason = "reward_rounds_to_zero"
    else:
        grant_specs = ()
        outcome = "ignored"
        reason = "not_first_recharge"

    expected = _event_record(
        event_id=event_id,
        tenant_id=tenant,
        owner_user_id=owner,
        idempotency_key=idem,
        source_event_id=source_id,
        business_key=order_id,
        event_type=EVENT_FIRST_RECHARGE_REWARD,
        invite=invite,
        source_order_id=order_id,
        source_payload=clean_source_payload,
        outcome=outcome,
        outcome_reason=reason,
        occurred_at=_event_timestamp(payment["paid_at"]),
        grant_specs=grant_specs,
        canonical_first_order_id=canonical_first_order_id,
        paid_cents=expected_paid_cents,
    )
    return _persist_event_and_effects(
        cursor,
        expected=expected,
        invite=invite,
        grant_specs=grant_specs,
        actor_user_id=actor,
    )


def process_first_recharge_refund(
    cursor: CursorLike,
    *,
    tenant_id: str,
    owner_user_id: str,
    invite_relation_id: str,
    source_event_id: str,
    source_order_id: str,
    refund_cents: int,
    cumulative_refunded_cents: int,
    cumulative_refunded_points: int,
    idempotency_key: str,
    source_payload: Mapping[str, Any] | None = None,
    actor_user_id: str = DEFAULT_EVENT_ACTOR,
) -> GrowthRefundApplyResult:
    """Monotonically claw back a consumer first-recharge referral reward.

    A missing original payment-reward event is deferred without writes. Stale
    cumulative refund events are recorded as ignored, and every actual debit is
    bounded by the original frozen reward.
    """

    tenant = _tenant_id(tenant_id)
    owner = _identifier(owner_user_id, "owner_user_id")
    relation_id = _growth_id(
        invite_relation_id,
        "growth_invite",
        "invite_relation_id",
    )
    source_id = _identifier(source_event_id, "source_event_id")
    order_id = _identifier(source_order_id, "source_order_id")
    idem = _identifier(idempotency_key, "idempotency_key")
    event_refund_cents = _positive_int(refund_cents, "refund_cents")
    cumulative_cents = _nonnegative_int(
        cumulative_refunded_cents,
        "cumulative_refunded_cents",
    )
    cumulative_points = _nonnegative_int(
        cumulative_refunded_points,
        "cumulative_refunded_points",
    )
    if event_refund_cents > cumulative_cents:
        raise InvalidProductGrowthInput(
            "refund_cents cannot exceed cumulative_refunded_cents"
        )
    actor = _identifier(actor_user_id, "actor_user_id")
    clean_source_payload = _bounded_json_object(
        source_payload or {},
        "source_payload",
    )

    _lock_subject(cursor, tenant, owner)
    invite = _locked_invite(cursor, relation_id, tenant, owner)
    payment = _locked_payment_order(cursor, order_id, owner)
    paid_cents = int(payment["amount_cents"])
    if cumulative_cents > paid_cents:
        raise ProductGrowthConflict(
            "cumulative_refunded_cents exceeds the durable paid amount"
        )
    durable_refunded_cents = int(payment.get("refunded_amount_cents") or 0)
    durable_refunded_points = int(payment.get("refunded_points") or 0)
    if durable_refunded_cents < cumulative_cents:
        raise ProductGrowthEventDeferred(
            "durable payment refund state has not reached this event"
        )
    if durable_refunded_points < cumulative_points:
        raise ProductGrowthEventDeferred(
            "durable payment refunded points have not reached this event"
        )

    refund_payload = _refund_event_payload(
        tenant_id=tenant,
        owner_user_id=owner,
        invite=invite,
        source_event_id=source_id,
        source_order_id=order_id,
        refund_cents=event_refund_cents,
        cumulative_refunded_cents=cumulative_cents,
        cumulative_refunded_points=cumulative_points,
        source_payload=clean_source_payload,
    )
    replay = _find_refund_input_replay(
        cursor,
        tenant_id=tenant,
        owner_user_id=owner,
        idempotency_key=idem,
        source_event_id=source_id,
        business_key=source_id,
        payload=refund_payload,
    )
    if replay is not None:
        return _replayed_refund_result(cursor, replay)

    cursor.execute(_SELECT_FIRST_PAYMENT_ORDER_FOR_UPDATE_SQL, (owner,))
    first_row = _fetchone_dict(cursor)
    if first_row is None:
        raise ProductGrowthIntegrityError(
            "refunded payment has no canonical first-payment row"
        )
    canonical_first = _decode_record(first_row)
    canonical_first_order_id = str(canonical_first["id"])
    occurred_at = _event_timestamp(
        payment.get("refunded_at") or payment.get("paid_at")
    )
    event_id = stable_growth_id(
        "growth_event",
        tenant,
        EVENT_FIRST_RECHARGE_REFUND,
        source_id,
    )

    if not hmac.compare_digest(canonical_first_order_id, order_id):
        expected = _refund_event_record(
            event_id=event_id,
            tenant_id=tenant,
            owner_user_id=owner,
            idempotency_key=idem,
            source_event_id=source_id,
            source_order_id=order_id,
            invite=invite,
            payload=refund_payload,
            outcome="ignored",
            outcome_reason="not_first_recharge",
            occurred_at=occurred_at,
            canonical_first_order_id=canonical_first_order_id,
            original_event_id="",
            original_grant_id="",
            previous_cumulative_refunded_cents=0,
            cumulative_refunded_cents=cumulative_cents,
            previous_reversed_points=0,
            cumulative_reversed_points=0,
            previous_recovered_points=0,
            cumulative_recovered_points=0,
            previous_reward_outstanding_points=0,
            reward_outstanding_points_after=0,
            owner_debt_outstanding_points_after=0,
            reversal_spec=None,
        )
        return _persist_refund_event(
            cursor,
            expected=expected,
            invite=invite,
            reversal_state=None,
            reversal_spec=None,
            actor_user_id=actor,
        )

    cursor.execute(
        _SELECT_EVENT_BY_BUSINESS_FOR_UPDATE_SQL,
        (tenant, EVENT_FIRST_RECHARGE_REWARD, order_id),
    )
    original_row = _fetchone_dict(cursor)
    if original_row is None:
        raise ProductGrowthEventDeferred(
            "first-recharge reward event has not been processed yet"
        )
    original_event = _decode_record(original_row)
    if str(original_event["owner_user_id"]) != owner:
        raise ProductGrowthIntegrityError(
            "first-recharge reward event crossed the owner boundary"
        )
    if str(original_event["outcome"]) != "applied":
        expected = _refund_event_record(
            event_id=event_id,
            tenant_id=tenant,
            owner_user_id=owner,
            idempotency_key=idem,
            source_event_id=source_id,
            source_order_id=order_id,
            invite=invite,
            payload=refund_payload,
            outcome="ignored",
            outcome_reason="original_reward_not_applied",
            occurred_at=occurred_at,
            canonical_first_order_id=canonical_first_order_id,
            original_event_id=str(original_event["id"]),
            original_grant_id="",
            previous_cumulative_refunded_cents=0,
            cumulative_refunded_cents=cumulative_cents,
            previous_reversed_points=0,
            cumulative_reversed_points=0,
            previous_recovered_points=0,
            cumulative_recovered_points=0,
            previous_reward_outstanding_points=0,
            reward_outstanding_points_after=0,
            owner_debt_outstanding_points_after=0,
            reversal_spec=None,
        )
        return _persist_refund_event(
            cursor,
            expected=expected,
            invite=invite,
            reversal_state=None,
            reversal_spec=None,
            actor_user_id=actor,
        )

    cursor.execute(
        _SELECT_GRANT_BY_EVENT_KIND_FOR_UPDATE_SQL,
        (
            original_event["id"],
            tenant,
            "first_recharge_inviter",
        ),
    )
    grant_row = _fetchone_dict(cursor)
    if grant_row is None:
        raise ProductGrowthIntegrityError(
            "applied first-recharge event is missing its inviter grant"
        )
    original_grant = _decode_record(grant_row)
    reversal_state = _ensure_reversal_state(
        cursor,
        tenant_id=tenant,
        owner_user_id=owner,
        invite=invite,
        source_order_id=order_id,
        original_event=original_event,
        original_grant=original_grant,
        original_paid_cents=paid_cents,
    )
    previous_cumulative = int(
        reversal_state["max_cumulative_refunded_cents"]
    )
    previous_reversed = int(reversal_state["reversed_points"])
    previous_recovered = int(reversal_state["recovered_points"])
    previous_reward_outstanding = int(reversal_state["outstanding_points"])

    if cumulative_cents < previous_cumulative:
        target_cumulative = previous_cumulative
        target_reversed = previous_reversed
        outcome = "ignored"
        reason = "stale_cumulative_refund"
    else:
        target_cumulative = max(previous_cumulative, cumulative_cents)
        remaining_paid_cents = max(paid_cents - target_cumulative, 0)
        remaining_reward = _first_recharge_points(
            paid_cents=remaining_paid_cents,
            invite=invite,
        )
        target_reversed = min(
            int(reversal_state["original_reward_points"]),
            max(
                int(reversal_state["original_reward_points"])
                - remaining_reward,
                previous_reversed,
            ),
        )
        if (
            target_cumulative == previous_cumulative
            and target_reversed == previous_reversed
        ):
            outcome = "ignored"
            reason = "cumulative_refund_already_applied"
        else:
            outcome = "applied"
            reason = ""

    points_to_assess = max(target_reversed - previous_reversed, 0)
    reversal_spec = (
        _prepare_reversal_spec(
            cursor,
            tenant_id=tenant,
            event_id=event_id,
            reward_owner_user_id=str(original_grant["owner_user_id"]),
            assessed_points=points_to_assess,
        )
        if points_to_assess > 0
        else None
    )
    existing_debt = (
        reversal_spec["debt"]
        if reversal_spec is not None
        else _locked_reward_debt(
            cursor,
            tenant,
            str(original_grant["owner_user_id"]),
        )
    )
    recovered_now = (
        0 if reversal_spec is None else int(reversal_spec["recovered_points"])
    )
    outstanding_added = (
        0
        if reversal_spec is None
        else int(reversal_spec["outstanding_points_added"])
    )
    target_recovered = previous_recovered + recovered_now
    target_reward_outstanding = (
        previous_reward_outstanding + outstanding_added
    )
    if target_recovered + target_reward_outstanding != target_reversed:
        raise ProductGrowthIntegrityError(
            "refund recovery split does not equal cumulative reversal"
        )
    owner_debt_after = (
        int(existing_debt["outstanding_points"])
        if reversal_spec is None
        else int(reversal_spec["outstanding_points_after"])
    )
    expected = _refund_event_record(
        event_id=event_id,
        tenant_id=tenant,
        owner_user_id=owner,
        idempotency_key=idem,
        source_event_id=source_id,
        source_order_id=order_id,
        invite=invite,
        payload=refund_payload,
        outcome=outcome,
        outcome_reason=reason,
        occurred_at=occurred_at,
        canonical_first_order_id=canonical_first_order_id,
        original_event_id=str(original_event["id"]),
        original_grant_id=str(original_grant["id"]),
        previous_cumulative_refunded_cents=previous_cumulative,
        cumulative_refunded_cents=target_cumulative,
        previous_reversed_points=previous_reversed,
        cumulative_reversed_points=target_reversed,
        previous_recovered_points=previous_recovered,
        cumulative_recovered_points=target_recovered,
        previous_reward_outstanding_points=previous_reward_outstanding,
        reward_outstanding_points_after=target_reward_outstanding,
        owner_debt_outstanding_points_after=owner_debt_after,
        reversal_spec=reversal_spec,
    )
    return _persist_refund_event(
        cursor,
        expected=expected,
        invite=invite,
        reversal_state=reversal_state,
        reversal_spec=reversal_spec,
        actor_user_id=actor,
    )


def classify_agent_payment(
    cursor: CursorLike,
    *,
    tenant_id: str,
    owner_user_id: str,
    source_order_id: str,
    paid_cents: int,
) -> AgentPaymentClassification | None:
    """Return server-owned first/repeat commission classification.

    This function does not write commission or finance rows. It locks the
    tenant customer, direct binding, current payment, and canonical first paid
    order so a caller can persist finance effects on the same cursor.
    Refunded rows remain in the chronology because they were successful cash
    payments; a separate refund lifecycle owns commission clawback.
    """

    tenant = _tenant_id(tenant_id)
    owner = _identifier(owner_user_id, "owner_user_id")
    order_id = _identifier(source_order_id, "source_order_id")
    expected_paid_cents = _positive_int(paid_cents, "paid_cents")
    _lock_subject(cursor, tenant, owner)

    cursor.execute(_SELECT_BINDING_BY_OWNER_FOR_UPDATE_SQL, (tenant, owner))
    binding_row = _fetchone_dict(cursor)
    if binding_row is None:
        return None
    binding = _decode_record(binding_row)

    payment = _locked_payment_order(cursor, order_id, owner)
    if int(payment["amount_cents"]) != expected_paid_cents:
        raise ProductGrowthConflict(
            "paid_cents does not match the durable payment order"
        )
    cursor.execute(_SELECT_FIRST_PAYMENT_ORDER_FOR_UPDATE_SQL, (owner,))
    first_row = _fetchone_dict(cursor)
    if first_row is None:
        raise ProductGrowthIntegrityError(
            "paid payment order has no canonical first-payment row"
        )
    first_payment = _decode_record(first_row)
    canonical_id = str(first_payment["id"])
    is_first = hmac.compare_digest(canonical_id, order_id)
    rate_field = (
        "first_order_commission_bps"
        if is_first
        else "repeat_order_commission_bps"
    )
    rate_bps = int(binding[rate_field])
    return AgentPaymentClassification(
        tenant_id=tenant,
        customer_user_id=owner,
        agent_id=str(binding["agent_id"]),
        agent_owner_user_id=str(binding["agent_owner_user_id"]),
        binding_id=str(binding["id"]),
        source_order_id=order_id,
        canonical_first_order_id=canonical_id,
        paid_cents=expected_paid_cents,
        is_first_order=is_first,
        commission_rate_bps=rate_bps,
        commission_amount_cents=expected_paid_cents * rate_bps // 10000,
        rule_version=str(binding["rule_version"]),
    )


def resolve_agent_payment_classification(
    cursor: CursorLike,
    **kwargs: Any,
) -> AgentPaymentClassification | None:
    """Alias emphasizing use from a caller-owned event transaction."""

    return classify_agent_payment(cursor, **kwargs)


class ProductGrowthStore:
    """Short-transaction wrapper around caller-owned cursor APIs."""

    def __init__(self, connection: ConnectionLike) -> None:
        if bool(getattr(connection, "autocommit", False)):
            raise InvalidProductGrowthInput(
                "ProductGrowthStore requires an autocommit-disabled connection"
            )
        self.connection = connection

    def create_or_get_agent(self, **kwargs: Any) -> GrowthCreateResult:
        with self._transaction() as cursor:
            return create_or_get_agent(cursor, **kwargs)

    def create_agent_profile(self, **kwargs: Any) -> GrowthCreateResult:
        with self._transaction() as cursor:
            return create_agent_profile(cursor, **kwargs)

    def bind_agent_customer(self, **kwargs: Any) -> GrowthCreateResult:
        with self._transaction() as cursor:
            return bind_agent_customer(cursor, **kwargs)

    def issue_consumer_invite_code(
        self,
        **kwargs: Any,
    ) -> InviteCodeIssueResult:
        with self._transaction() as cursor:
            return issue_consumer_invite_code(cursor, **kwargs)

    def resolve_consumer_invite_code(
        self,
        **kwargs: Any,
    ) -> dict[str, Any] | None:
        with self._transaction() as cursor:
            return resolve_consumer_invite_code(cursor, **kwargs)

    def accept_consumer_invite(self, **kwargs: Any) -> GrowthCreateResult:
        with self._transaction() as cursor:
            return accept_consumer_invite(cursor, **kwargs)

    def get_agent_by_user(self, **kwargs: Any) -> dict[str, Any] | None:
        with self._transaction() as cursor:
            return get_agent_by_user(cursor, **kwargs)

    def get_agent_binding(self, **kwargs: Any) -> dict[str, Any] | None:
        with self._transaction() as cursor:
            return get_agent_binding(cursor, **kwargs)

    def get_binding_for_customer(
        self,
        **kwargs: Any,
    ) -> dict[str, Any] | None:
        with self._transaction() as cursor:
            return get_binding_for_customer(cursor, **kwargs)

    def get_invite_relation(self, **kwargs: Any) -> dict[str, Any] | None:
        with self._transaction() as cursor:
            return get_invite_relation(cursor, **kwargs)

    def get_invite_for_invitee(
        self,
        **kwargs: Any,
    ) -> dict[str, Any] | None:
        with self._transaction() as cursor:
            return get_invite_for_invitee(cursor, **kwargs)

    def get_business_event(self, **kwargs: Any) -> dict[str, Any] | None:
        with self._transaction() as cursor:
            return get_business_event(cursor, **kwargs)

    def list_reward_grants(self, **kwargs: Any) -> list[dict[str, Any]]:
        with self._transaction() as cursor:
            return list_reward_grants(cursor, **kwargs)

    def list_reward_reversals(
        self,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        with self._transaction() as cursor:
            return list_reward_reversals(cursor, **kwargs)

    def list_reward_debt_recoveries(
        self,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        with self._transaction() as cursor:
            return list_reward_debt_recoveries(cursor, **kwargs)

    def get_reward_debt(self, **kwargs: Any) -> dict[str, Any] | None:
        with self._transaction() as cursor:
            return get_reward_debt(cursor, **kwargs)

    def process_registration_reward(
        self,
        **kwargs: Any,
    ) -> GrowthEventApplyResult:
        with self._transaction() as cursor:
            return process_registration_reward(cursor, **kwargs)

    def process_first_recharge_reward(
        self,
        **kwargs: Any,
    ) -> GrowthEventApplyResult:
        with self._transaction() as cursor:
            return process_first_recharge_reward(cursor, **kwargs)

    def process_first_recharge_refund(
        self,
        **kwargs: Any,
    ) -> GrowthRefundApplyResult:
        with self._transaction() as cursor:
            return process_first_recharge_refund(cursor, **kwargs)

    def classify_agent_payment(
        self,
        **kwargs: Any,
    ) -> AgentPaymentClassification | None:
        with self._transaction() as cursor:
            return classify_agent_payment(cursor, **kwargs)

    def resolve_agent_payment_classification(
        self,
        **kwargs: Any,
    ) -> AgentPaymentClassification | None:
        with self._transaction() as cursor:
            return resolve_agent_payment_classification(cursor, **kwargs)

    @contextmanager
    def _transaction(self) -> Iterator[CursorLike]:
        cursor = self.connection.cursor()
        try:
            yield cursor
        except Exception:
            self.connection.rollback()
            raise
        else:
            try:
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
        finally:
            close = getattr(cursor, "close", None)
            if callable(close):
                close()


def _persist_event_and_effects(
    cursor: CursorLike,
    *,
    expected: Mapping[str, Any],
    invite: Mapping[str, Any],
    grant_specs: Sequence[Mapping[str, Any]],
    actor_user_id: str,
) -> GrowthEventApplyResult:
    existing = _find_existing_event(cursor, expected)
    if existing is not None:
        return _replayed_event_result(cursor, existing, expected)

    reward_plans = _prepare_growth_reward_plans(
        cursor,
        tenant_id=str(expected["tenant_id"]),
        event_id=str(expected["id"]),
        grant_specs=grant_specs,
    )
    finalized = _finalize_growth_reward_event(expected, reward_plans)
    cursor.execute(
        _INSERT_EVENT_SQL,
        (
            finalized["id"],
            finalized["tenant_id"],
            finalized["owner_user_id"],
            finalized["idempotency_key"],
            finalized["source_event_id"],
            finalized["business_key"],
            finalized["event_type"],
            finalized["invite_relation_id"],
            finalized["source_order_id"],
            finalized["rule_version"],
            _canonical_json(finalized["payload"]),
            finalized["payload_sha256"],
            finalized["outcome"],
            finalized["outcome_reason"],
            _canonical_json(finalized["result_payload"]),
            finalized["result_sha256"],
            finalized["occurred_at"],
        ),
    )
    inserted = _fetchone_dict(cursor)
    if inserted is None:
        raced = _find_existing_event(cursor, finalized)
        if raced is None:
            raise ProductGrowthConflict(
                "growth event insert conflicted without a resolvable event"
            )
        return _replayed_event_result(cursor, raced, finalized)

    event = _decode_record(inserted)
    accounts: list[dict[str, Any]] = []
    grants: list[dict[str, Any]] = []
    recoveries: list[dict[str, Any]] = []
    for plan in reward_plans:
        updated_debt = _recover_growth_reward_debt(cursor, plan=plan)
        account = _credit_growth_reward(
            cursor,
            tenant_id=str(finalized["tenant_id"]),
            event=event,
            invite=invite,
            spec=plan,
        )
        grant = _insert_reward_grant(
            cursor,
            event=event,
            invite=invite,
            spec=plan,
        )
        if int(plan["debt_offset_points"]) > 0:
            recoveries.append(
                _insert_reward_debt_recovery(
                    cursor,
                    event=event,
                    grant=grant,
                    plan=plan,
                    updated_debt=updated_debt,
                )
            )
        accounts.append(account)
        grants.append(grant)

    result_payload = event["result_payload"]
    _append_audit(
        cursor,
        tenant_id=str(event["tenant_id"]),
        owner_user_id=str(event["owner_user_id"]),
        actor_user_id=actor_user_id,
        action=f"growth.{event['event_type']}.processed",
        target_type="growth_business_event",
        target_id=str(event["id"]),
        status=(
            "succeeded" if str(event["outcome"]) == "applied" else "ignored"
        ),
        metadata={
            "sourceEventId": event["source_event_id"],
            "sourceOrderId": event["source_order_id"],
            "ruleVersion": event["rule_version"],
            "outcomeReason": event["outcome_reason"],
            "grantIds": [row["id"] for row in grants],
            "debtRecoveryIds": [row["id"] for row in recoveries],
            "grossRewardPoints": result_payload["grossRewardPoints"],
            "debtOffsetPoints": result_payload["debtOffsetPoints"],
            "netWalletPoints": result_payload["netWalletPoints"],
        },
    )
    return GrowthEventApplyResult(
        event=event,
        grants=tuple(grants),
        wallet_accounts=tuple(accounts),
        applied=str(event["outcome"]) == "applied",
        idempotent=False,
        debt_recoveries=tuple(recoveries),
        gross_reward_points=int(result_payload["grossRewardPoints"]),
        debt_offset_points=int(result_payload["debtOffsetPoints"]),
        net_wallet_points=int(result_payload["netWalletPoints"]),
    )


def _find_existing_event(
    cursor: CursorLike,
    expected: Mapping[str, Any],
) -> dict[str, Any] | None:
    cursor.execute(
        _SELECT_EVENT_BY_IDEMPOTENCY_FOR_UPDATE_SQL,
        (
            expected["tenant_id"],
            expected["owner_user_id"],
            expected["idempotency_key"],
        ),
    )
    row = _fetchone_dict(cursor)
    if row is not None:
        event = _decode_record(row)
        _require_exact_event_input_replay(event, expected)
        return event

    cursor.execute(
        _SELECT_EVENT_BY_SOURCE_FOR_UPDATE_SQL,
        (expected["tenant_id"], expected["source_event_id"]),
    )
    row = _fetchone_dict(cursor)
    if row is not None:
        event = _decode_record(row)
        _require_exact_event_input_replay(event, expected)
        return event

    cursor.execute(
        _SELECT_EVENT_BY_BUSINESS_FOR_UPDATE_SQL,
        (
            expected["tenant_id"],
            expected["event_type"],
            expected["business_key"],
        ),
    )
    row = _fetchone_dict(cursor)
    if row is None:
        return None
    event = _decode_record(row)
    _require_semantic_event_input_replay(event, expected)
    return event


def _replayed_event_result(
    cursor: CursorLike,
    event: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> GrowthEventApplyResult:
    cursor.execute(
        _SELECT_GRANTS_BY_EVENT_SQL,
        (
            event["id"],
            event["tenant_id"],
            event["owner_user_id"],
        ),
    )
    grants = tuple(
        _decode_record(row) for row in _fetchall_dicts(cursor)
    )
    cursor.execute(
        _SELECT_DEBT_RECOVERIES_BY_EVENT_SQL,
        (
            event["id"],
            event["tenant_id"],
            event["owner_user_id"],
        ),
    )
    recoveries = tuple(
        _decode_record(row) for row in _fetchall_dicts(cursor)
    )
    expected_grants = list(expected["result_payload"].get("rewardGrants") or [])
    stored_result = event.get("result_payload") or {}
    if not hmac.compare_digest(
        str(event.get("result_sha256") or ""),
        _content_sha256(stored_result),
    ):
        raise ProductGrowthIntegrityError(
            "growth event result digest changed"
        )
    stored_grants = list(stored_result.get("rewardGrants") or [])
    if len(grants) != len(expected_grants) or len(grants) != len(stored_grants):
        raise ProductGrowthIntegrityError(
            "growth event reward grant count does not match its result"
        )
    actual_by_id = {str(row["id"]): row for row in grants}
    stored_by_id = {str(row["grantId"]): row for row in stored_grants}
    recovery_by_grant = {
        str(row["reward_grant_id"]): row for row in recoveries
    }
    gross_total = 0
    offset_total = 0
    net_total = 0
    for expected_spec in expected_grants:
        grant_id = str(expected_spec["grantId"])
        grant = actual_by_id.get(grant_id)
        stored_spec = stored_by_id.get(grant_id)
        if stored_spec is None:
            raise ProductGrowthIntegrityError(
                "growth event result is missing a frozen reward grant"
            )
        if grant is None:
            raise ProductGrowthIntegrityError(
                "growth event is missing a frozen reward grant"
            )
        expected_gross = int(
            expected_spec.get(
                "grossRewardPoints",
                expected_spec.get("points") or 0,
            )
        )
        gross = int(
            stored_spec.get(
                "grossRewardPoints",
                stored_spec.get("points") or 0,
            )
        )
        offset = int(stored_spec.get("debtOffsetPoints") or 0)
        net = int(
            stored_spec.get(
                "netWalletPoints",
                gross - offset,
            )
        )
        wallet_order_id = stored_spec.get("walletOrderId") or None
        recovery_id = stored_spec.get("debtRecoveryId") or None
        if (
            str(grant["owner_user_id"])
            != str(expected_spec["ownerUserId"])
            or str(grant["reward_kind"])
            != str(expected_spec["rewardKind"])
            or int(grant["points"]) != expected_gross
            or gross != expected_gross
            or int(grant.get("debt_offset_points") or 0) != offset
            or int(grant.get("net_wallet_points") or 0) != net
            or (grant.get("wallet_order_id") or None) != wallet_order_id
            or gross != offset + net
            or (net == 0) != (wallet_order_id is None)
        ):
            raise ProductGrowthIntegrityError(
                "growth event reward grant content changed"
            )
        if int(stored_result.get("schemaVersion") or 1) >= 2:
            grant_content = {
                "id": str(grant["id"]),
                "tenant_id": str(grant["tenant_id"]),
                "owner_user_id": str(grant["owner_user_id"]),
                "business_event_id": str(grant["business_event_id"]),
                "event_owner_user_id": str(
                    grant["event_owner_user_id"]
                ),
                "invite_relation_id": str(grant["invite_relation_id"]),
                "reward_kind": str(grant["reward_kind"]),
                "points": int(grant["points"]),
                "debt_offset_points": int(grant["debt_offset_points"]),
                "net_wallet_points": int(grant["net_wallet_points"]),
                "wallet_order_id": grant.get("wallet_order_id"),
                "rule_version": str(grant["rule_version"]),
                "metadata": grant.get("metadata") or {},
            }
            if not hmac.compare_digest(
                str(grant.get("content_sha256") or ""),
                _content_sha256(grant_content),
            ):
                raise ProductGrowthIntegrityError(
                    "growth reward grant digest changed"
                )
        _validate_replayed_growth_wallet_effect(
            cursor,
            event=event,
            owner_user_id=str(grant["owner_user_id"]),
            reward_kind=str(grant["reward_kind"]),
            gross_reward_points=gross,
            debt_offset_points=offset,
            net_wallet_points=net,
            wallet_order_id=wallet_order_id,
            result_schema_version=int(
                stored_result.get("schemaVersion") or 1
            ),
        )
        recovery = recovery_by_grant.get(grant_id)
        if offset == 0:
            if recovery is not None or recovery_id is not None:
                raise ProductGrowthIntegrityError(
                    "growth reward without debt offset has recovery evidence"
                )
        elif (
            recovery is None
            or str(recovery["id"]) != str(recovery_id)
            or int(recovery["gross_reward_points"]) != gross
            or int(recovery["recovered_points"]) != offset
            or int(recovery["net_wallet_points"]) != net
            or int(recovery["debt_outstanding_before"])
            - int(recovery["debt_outstanding_after"])
            != offset
        ):
            raise ProductGrowthIntegrityError(
                "growth debt recovery evidence changed"
            )
        if recovery is not None:
            recovery_content = {
                "id": str(recovery["id"]),
                "tenant_id": str(recovery["tenant_id"]),
                "owner_user_id": str(recovery["owner_user_id"]),
                "business_event_id": str(recovery["business_event_id"]),
                "event_owner_user_id": str(
                    recovery["event_owner_user_id"]
                ),
                "reward_grant_id": str(recovery["reward_grant_id"]),
                "reward_kind": str(recovery["reward_kind"]),
                "gross_reward_points": int(
                    recovery["gross_reward_points"]
                ),
                "recovered_points": int(recovery["recovered_points"]),
                "net_wallet_points": int(recovery["net_wallet_points"]),
                "debt_outstanding_before": int(
                    recovery["debt_outstanding_before"]
                ),
                "debt_outstanding_after": int(
                    recovery["debt_outstanding_after"]
                ),
                "rule_version": str(recovery["rule_version"]),
                "metadata": recovery.get("metadata") or {},
            }
            if not hmac.compare_digest(
                str(recovery.get("content_sha256") or ""),
                _content_sha256(recovery_content),
            ):
                raise ProductGrowthIntegrityError(
                    "growth debt recovery digest changed"
                )
        gross_total += gross
        offset_total += offset
        net_total += net
    if len(recoveries) != sum(
        1
        for spec in stored_grants
        if int(spec.get("debtOffsetPoints") or 0) > 0
    ):
        raise ProductGrowthIntegrityError(
            "growth event debt recovery count does not match its result"
        )
    if (
        int(stored_result.get("grossRewardPoints", gross_total)) != gross_total
        or int(stored_result.get("debtOffsetPoints", offset_total))
        != offset_total
        or int(stored_result.get("netWalletPoints", net_total)) != net_total
        or int(stored_result.get("totalRewardPoints", gross_total))
        != gross_total
    ):
        raise ProductGrowthIntegrityError(
            "growth event reward totals changed"
        )
    return GrowthEventApplyResult(
        event=dict(event),
        grants=grants,
        wallet_accounts=(),
        applied=str(event["outcome"]) == "applied",
        idempotent=True,
        debt_recoveries=recoveries,
        gross_reward_points=gross_total,
        debt_offset_points=offset_total,
        net_wallet_points=net_total,
    )


def _normalize_agent(
    *,
    tenant_id: Any,
    owner_user_id: Any,
    idempotency_key: Any,
    agent_code: Any,
    metadata: Mapping[str, Any] | None,
    rule_version: Any,
) -> dict[str, Any]:
    tenant = _tenant_id(tenant_id)
    owner = _identifier(owner_user_id, "owner_user_id")
    idem = _identifier(idempotency_key, "idempotency_key")
    version = _current_rule_version(rule_version)
    growth_rules.validate_agent_commission_depth(1)
    first_bps = growth_rules.agent_commission_rate_bps(
        growth_rules.LEVEL_AGENT,
        is_first_order=True,
    )
    repeat_bps = growth_rules.agent_commission_rate_bps(
        growth_rules.LEVEL_AGENT,
        is_first_order=False,
    )
    agent_id = stable_growth_id("growth_agent", tenant, owner)
    clean_code = _agent_code(agent_code, agent_id=agent_id)
    clean_metadata = _bounded_json_object(metadata or {}, "metadata")
    content = {
        "id": agent_id,
        "tenant_id": tenant,
        "owner_user_id": owner,
        "idempotency_key": idem,
        "agent_code": clean_code,
        "status": "active",
        "rule_version": version,
        "commission_depth": 1,
        "first_order_commission_bps": first_bps,
        "repeat_order_commission_bps": repeat_bps,
        "metadata": clean_metadata,
    }
    content["request_sha256"] = _content_sha256(
        {
            key: value
            for key, value in content.items()
            if key != "status"
        }
    )
    return content


def _normalize_invite(
    *,
    tenant_id: Any,
    owner_user_id: Any,
    idempotency_key: Any,
    invite_code: Mapping[str, Any],
    risk_snapshot: Mapping[str, Any] | None,
    metadata: Mapping[str, Any] | None,
    rule_version: Any,
) -> dict[str, Any]:
    tenant = _tenant_id(tenant_id)
    owner = _identifier(owner_user_id, "owner_user_id")
    code_tenant = _tenant_id(invite_code.get("tenant_id"))
    if not hmac.compare_digest(tenant, code_tenant):
        raise ProductGrowthConflict("invite code crossed the tenant boundary")
    inviter = _identifier(
        invite_code.get("owner_user_id"),
        "invite code owner_user_id",
    )
    if hmac.compare_digest(owner, inviter):
        raise ProductGrowthConflict("self invitation is not allowed")
    idem = _identifier(idempotency_key, "idempotency_key")
    version = _current_rule_version(rule_version)
    if str(invite_code.get("rule_version") or "") != version:
        raise ProductGrowthConflict(
            "invite code rule version does not match the acceptance rule"
        )
    code_id = _growth_id(
        invite_code.get("id"),
        "growth_invite_code",
        "invite_code_id",
    )
    code_digest = _sha256(
        invite_code.get("code_sha256"),
        "invite_code.code_sha256",
    )
    risk = _bounded_json_object(risk_snapshot or {}, "risk_snapshot")
    clean_metadata = _bounded_json_object(metadata or {}, "metadata")
    growth_rules.validate_consumer_referral_depth(1)
    registration = growth_rules.consumer_referral_rewards(
        growth_rules.EVENT_INVITEE_REGISTERED
    )
    relation_id = stable_growth_id("growth_invite", tenant, owner)
    content = {
        "id": relation_id,
        "tenant_id": tenant,
        "owner_user_id": owner,
        "idempotency_key": idem,
        "inviter_user_id": inviter,
        "invitee_user_id": owner,
        "invite_code_id": code_id,
        "invite_code_sha256": code_digest,
        "relation_depth": 1,
        "rule_version": version,
        "registration_inviter_points": int(
            registration["inviter_points"]
        ),
        "registration_invitee_points": int(
            registration["invitee_points"]
        ),
        "first_recharge_rebate_percent": int(
            growth_rules.CONSUMER_FIRST_RECHARGE_REBATE_PERCENT
        ),
        "cash_points_per_yuan": int(growth_rules.POINTS_PER_YUAN),
        "cents_per_yuan": int(growth_rules.CENTS_PER_YUAN),
        "risk_snapshot": risk,
        "metadata": clean_metadata,
    }
    content["request_sha256"] = _content_sha256(content)
    return content


def _event_record(
    *,
    event_id: str,
    tenant_id: str,
    owner_user_id: str,
    idempotency_key: str,
    source_event_id: str,
    business_key: str,
    event_type: str,
    invite: Mapping[str, Any],
    source_order_id: str,
    source_payload: Mapping[str, Any],
    outcome: str,
    outcome_reason: str,
    occurred_at: datetime,
    grant_specs: Sequence[Mapping[str, Any]],
    canonical_first_order_id: str,
    paid_cents: int,
) -> dict[str, Any]:
    clean_type = _event_type(event_type)
    if outcome not in {"applied", "ignored"}:
        raise InvalidProductGrowthInput("invalid growth event outcome")
    if (outcome == "applied") == bool(outcome_reason):
        raise InvalidProductGrowthInput(
            "applied events need no reason and ignored events need a reason"
        )
    payload = {
        "schemaVersion": 1,
        "eventType": clean_type,
        "tenantId": tenant_id,
        "ownerUserId": owner_user_id,
        "inviteRelationId": str(invite["id"]),
        "inviterUserId": str(invite["inviter_user_id"]),
        "inviteeUserId": str(invite["invitee_user_id"]),
        "sourceEventId": source_event_id,
        "sourceOrderId": source_order_id,
        "paidCents": paid_cents,
        "ruleVersion": str(invite["rule_version"]),
        "source": dict(source_payload),
    }
    frozen_grants = [
        {
            "grantId": str(spec["grant_id"]),
            "ownerUserId": str(spec["owner_user_id"]),
            "rewardKind": str(spec["reward_kind"]),
            "points": int(spec["points"]),
            "grossRewardPoints": int(spec["points"]),
            "debtOffsetPoints": 0,
            "netWalletPoints": int(spec["points"]),
            "walletOrderId": str(spec["wallet_order_id"]),
            "debtRecoveryId": None,
        }
        for spec in grant_specs
    ]
    result = {
        "schemaVersion": 2,
        "businessEventId": event_id,
        "outcome": outcome,
        "reason": outcome_reason,
        "canonicalFirstOrderId": canonical_first_order_id,
        "rewardGrants": frozen_grants,
        "totalRewardPoints": sum(
            int(spec["points"]) for spec in grant_specs
        ),
        "grossRewardPoints": sum(
            int(spec["points"]) for spec in grant_specs
        ),
        "debtOffsetPoints": 0,
        "netWalletPoints": sum(
            int(spec["points"]) for spec in grant_specs
        ),
    }
    return {
        "id": event_id,
        "tenant_id": tenant_id,
        "owner_user_id": owner_user_id,
        "idempotency_key": idempotency_key,
        "source_event_id": source_event_id,
        "business_key": business_key,
        "event_type": clean_type,
        "invite_relation_id": str(invite["id"]),
        "source_order_id": source_order_id,
        "rule_version": str(invite["rule_version"]),
        "payload": payload,
        "payload_sha256": _content_sha256(payload),
        "outcome": outcome,
        "outcome_reason": outcome_reason,
        "result_payload": result,
        "result_sha256": _content_sha256(result),
        "occurred_at": occurred_at,
    }


def _grant_spec(
    *,
    tenant_id: str,
    event_id: str,
    owner_user_id: str,
    reward_kind: str,
    points: int,
) -> dict[str, Any]:
    owner = _identifier(owner_user_id, "reward owner_user_id")
    amount = _positive_int(points, "reward points")
    kind = _reward_kind(reward_kind)
    grant_id = stable_growth_id(
        "growth_grant",
        tenant_id,
        event_id,
        kind,
        owner,
    )
    wallet_order_id = stable_growth_id(
        "growth_credit",
        tenant_id,
        event_id,
        kind,
        owner,
    )
    return {
        "grant_id": grant_id,
        "owner_user_id": owner,
        "reward_kind": kind,
        "points": amount,
        "wallet_order_id": wallet_order_id,
    }


def _growth_reward_split(
    gross_reward_points: int,
    debt_outstanding_points: int,
) -> tuple[int, int]:
    gross = _positive_int(gross_reward_points, "gross_reward_points")
    outstanding = _nonnegative_int(
        debt_outstanding_points,
        "debt_outstanding_points",
    )
    offset = min(gross, outstanding)
    return offset, gross - offset


def _prepare_growth_reward_plans(
    cursor: CursorLike,
    *,
    tenant_id: str,
    event_id: str,
    grant_specs: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    plans_by_grant_id: dict[str, dict[str, Any]] = {}
    ordered_specs = sorted(
        grant_specs,
        key=lambda spec: (
            str(spec["owner_user_id"]),
            str(spec["reward_kind"]),
            str(spec["grant_id"]),
        ),
    )
    for spec in ordered_specs:
        owner = str(spec["owner_user_id"])
        debt = _locked_reward_debt(cursor, tenant_id, owner)
        cursor.execute(_INSERT_POINT_ACCOUNT_SQL, (owner,))
        _fetchone_dict(cursor)
        cursor.execute(_SELECT_POINT_ACCOUNT_FOR_UPDATE_SQL, (owner,))
        account_row = _fetchone_dict(cursor)
        if account_row is None:
            raise ProductGrowthIntegrityError(
                "growth reward wallet account could not be locked"
            )
        gross = int(spec["points"])
        offset, net = _growth_reward_split(
            gross,
            int(debt["outstanding_points"]),
        )
        plan = {
            **dict(spec),
            "tenant_id": tenant_id,
            "gross_reward_points": gross,
            "debt_offset_points": offset,
            "net_wallet_points": net,
            "wallet_order_id": (
                spec["wallet_order_id"] if net > 0 else None
            ),
            "debt_recovery_id": (
                stable_growth_id(
                    "growth_debt_recovery",
                    tenant_id,
                    event_id,
                    str(spec["grant_id"]),
                    owner,
                )
                if offset > 0
                else None
            ),
            "debt": debt,
            "account": _decode_record(account_row),
        }
        plans_by_grant_id[str(spec["grant_id"])] = plan
    return tuple(
        plans_by_grant_id[str(spec["grant_id"])] for spec in grant_specs
    )


def _finalize_growth_reward_event(
    expected: Mapping[str, Any],
    reward_plans: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    finalized = dict(expected)
    result = dict(expected["result_payload"])
    frozen_grants = [
        {
            "grantId": str(plan["grant_id"]),
            "ownerUserId": str(plan["owner_user_id"]),
            "rewardKind": str(plan["reward_kind"]),
            "points": int(plan["gross_reward_points"]),
            "grossRewardPoints": int(plan["gross_reward_points"]),
            "debtOffsetPoints": int(plan["debt_offset_points"]),
            "netWalletPoints": int(plan["net_wallet_points"]),
            "walletOrderId": plan["wallet_order_id"],
            "debtRecoveryId": plan["debt_recovery_id"],
        }
        for plan in reward_plans
    ]
    result.update(
        {
            "schemaVersion": 2,
            "rewardGrants": frozen_grants,
            "totalRewardPoints": sum(
                int(plan["gross_reward_points"]) for plan in reward_plans
            ),
            "grossRewardPoints": sum(
                int(plan["gross_reward_points"]) for plan in reward_plans
            ),
            "debtOffsetPoints": sum(
                int(plan["debt_offset_points"]) for plan in reward_plans
            ),
            "netWalletPoints": sum(
                int(plan["net_wallet_points"]) for plan in reward_plans
            ),
        }
    )
    finalized["result_payload"] = result
    finalized["result_sha256"] = _content_sha256(result)
    return finalized


def _growth_credit_metadata(
    *,
    event: Mapping[str, Any],
    owner_user_id: str,
    reward_kind: str,
    gross_reward_points: int,
    debt_offset_points: int,
    net_wallet_points: int,
    result_schema_version: int = 2,
) -> dict[str, Any]:
    _identifier(owner_user_id, "growth reward owner_user_id")
    metadata = {
        "domain": "growth",
        "tenantId": str(event["tenant_id"]),
        "growthEventId": str(event["id"]),
        "inviteRelationId": str(event["invite_relation_id"]),
        "rewardKind": reward_kind,
        "ruleVersion": str(event["rule_version"]),
    }
    if result_schema_version >= 2:
        metadata.update(
            {
                "grossRewardPoints": gross_reward_points,
                "debtOffsetPoints": debt_offset_points,
                "netWalletPoints": net_wallet_points,
            }
        )
    return metadata


def _validate_replayed_growth_wallet_effect(
    cursor: CursorLike,
    *,
    event: Mapping[str, Any],
    owner_user_id: str,
    reward_kind: str,
    gross_reward_points: int,
    debt_offset_points: int,
    net_wallet_points: int,
    wallet_order_id: str | None,
    result_schema_version: int,
) -> None:
    if net_wallet_points == 0:
        if wallet_order_id is not None:
            raise ProductGrowthIntegrityError(
                "fully offset reward replay exposed a wallet order"
            )
        return
    if wallet_order_id is None:
        raise ProductGrowthIntegrityError(
            "net reward replay is missing its wallet order"
        )
    metadata = _growth_credit_metadata(
        event=event,
        owner_user_id=owner_user_id,
        reward_kind=reward_kind,
        gross_reward_points=gross_reward_points,
        debt_offset_points=debt_offset_points,
        net_wallet_points=net_wallet_points,
        result_schema_version=result_schema_version,
    )
    wallet_content = {
        "id": wallet_order_id,
        "ownerUserId": owner_user_id,
        "orderKind": "credit",
        "points": net_wallet_points,
        "metadata": metadata,
    }
    request_sha256 = _content_sha256(wallet_content)
    cursor.execute(_SELECT_POINT_ORDER_FOR_UPDATE_SQL, (wallet_order_id,))
    order_row = _fetchone_dict(cursor)
    if order_row is None:
        raise ProductGrowthIntegrityError(
            "growth reward replay is missing its wallet order"
        )
    _validate_wallet_order_replay(
        _decode_record(order_row),
        owner_user_id=owner_user_id,
        points=net_wallet_points,
        request_sha256=request_sha256,
        metadata=metadata,
    )
    cursor.execute(_SELECT_POINT_LEDGER_FOR_UPDATE_SQL, (wallet_order_id,))
    ledger_row = _fetchone_dict(cursor)
    if ledger_row is None:
        raise ProductGrowthIntegrityError(
            "growth reward replay is missing its wallet ledger"
        )
    _validate_wallet_ledger_replay(
        _decode_record(ledger_row),
        owner_user_id=owner_user_id,
        wallet_order_id=wallet_order_id,
        points=net_wallet_points,
        request_sha256=request_sha256,
        metadata=metadata,
    )


def _credit_growth_reward(
    cursor: CursorLike,
    *,
    tenant_id: str,
    event: Mapping[str, Any],
    invite: Mapping[str, Any],
    spec: Mapping[str, Any],
) -> dict[str, Any]:
    owner = str(spec["owner_user_id"])
    if tenant_id != str(event["tenant_id"]):
        raise ProductGrowthIntegrityError(
            "growth reward crossed the event tenant boundary"
        )
    gross = int(spec["gross_reward_points"])
    offset = int(spec["debt_offset_points"])
    points = int(spec["net_wallet_points"])
    account = dict(spec["account"])
    wallet_order_id = spec.get("wallet_order_id")
    if points == 0:
        if wallet_order_id is not None:
            raise ProductGrowthIntegrityError(
                "fully offset growth reward cannot create a wallet order"
            )
        return account
    if not wallet_order_id:
        raise ProductGrowthIntegrityError(
            "net growth reward is missing its wallet order"
        )
    wallet_order_id = str(wallet_order_id)
    metadata = _growth_credit_metadata(
        event=event,
        owner_user_id=owner,
        reward_kind=str(spec["reward_kind"]),
        gross_reward_points=gross,
        debt_offset_points=offset,
        net_wallet_points=points,
    )
    if str(invite["id"]) != str(event["invite_relation_id"]):
        raise ProductGrowthIntegrityError(
            "growth reward invite relation changed before wallet credit"
        )
    wallet_content = {
        "id": str(wallet_order_id),
        "ownerUserId": owner,
        "orderKind": "credit",
        "points": points,
        "metadata": metadata,
    }
    request_sha256 = _content_sha256(wallet_content)

    cursor.execute(
        _INSERT_POINT_ORDER_SQL,
        (
            wallet_order_id,
            owner,
            points,
            request_sha256,
            _canonical_json(metadata),
        ),
    )
    order_row = _fetchone_dict(cursor)
    if order_row is None:
        cursor.execute(
            _SELECT_POINT_ORDER_FOR_UPDATE_SQL,
            (wallet_order_id,),
        )
        existing_order = _fetchone_dict(cursor)
        if existing_order is None:
            raise ProductGrowthIntegrityError(
                "reward point order conflict exposed no row"
            )
        order = _decode_record(existing_order)
        _validate_wallet_order_replay(
            order,
            owner_user_id=owner,
            points=points,
            request_sha256=request_sha256,
            metadata=metadata,
        )
        cursor.execute(
            _SELECT_POINT_LEDGER_FOR_UPDATE_SQL,
            (wallet_order_id,),
        )
        ledger_row = _fetchone_dict(cursor)
        if ledger_row is None:
            raise ProductGrowthIntegrityError(
                "reward point order is missing its immutable ledger row"
            )
        _validate_wallet_ledger_replay(
            _decode_record(ledger_row),
            owner_user_id=owner,
            wallet_order_id=wallet_order_id,
            points=points,
            request_sha256=request_sha256,
            metadata=metadata,
        )
        return account

    cursor.execute(
        _CREDIT_POINT_ACCOUNT_SQL,
        (points, points, owner),
    )
    credited_row = _fetchone_dict(cursor)
    if credited_row is None:
        raise ProductGrowthIntegrityError(
            "reward wallet credit did not return the locked account"
        )
    credited = _decode_record(credited_row)
    cursor.execute(
        _INSERT_POINT_LEDGER_SQL,
        (
            owner,
            wallet_order_id,
            points,
            points,
            int(credited["balance_points"]),
            request_sha256,
            _canonical_json(metadata),
        ),
    )
    if _fetchone_dict(cursor) is None:
        raise ProductGrowthIntegrityError(
            "reward wallet ledger insert returned no row"
        )
    return credited


def _insert_reward_grant(
    cursor: CursorLike,
    *,
    event: Mapping[str, Any],
    invite: Mapping[str, Any],
    spec: Mapping[str, Any],
) -> dict[str, Any]:
    metadata = {
        "sourceEventId": str(event["source_event_id"]),
        "sourceOrderId": str(event["source_order_id"]),
        "eventType": str(event["event_type"]),
        "grossRewardPoints": int(spec["gross_reward_points"]),
        "debtOffsetPoints": int(spec["debt_offset_points"]),
        "netWalletPoints": int(spec["net_wallet_points"]),
    }
    content = {
        "id": str(spec["grant_id"]),
        "tenant_id": str(event["tenant_id"]),
        "owner_user_id": str(spec["owner_user_id"]),
        "business_event_id": str(event["id"]),
        "event_owner_user_id": str(event["owner_user_id"]),
        "invite_relation_id": str(invite["id"]),
        "reward_kind": str(spec["reward_kind"]),
        "points": int(spec["gross_reward_points"]),
        "debt_offset_points": int(spec["debt_offset_points"]),
        "net_wallet_points": int(spec["net_wallet_points"]),
        "wallet_order_id": spec.get("wallet_order_id"),
        "rule_version": str(event["rule_version"]),
        "metadata": metadata,
    }
    content["content_sha256"] = _content_sha256(content)
    cursor.execute(
        _INSERT_GRANT_SQL,
        (
            content["id"],
            content["tenant_id"],
            content["owner_user_id"],
            content["business_event_id"],
            content["event_owner_user_id"],
            content["invite_relation_id"],
            content["reward_kind"],
            content["points"],
            content["debt_offset_points"],
            content["net_wallet_points"],
            content["wallet_order_id"],
            content["rule_version"],
            content["content_sha256"],
            _canonical_json(content["metadata"]),
        ),
    )
    inserted = _fetchone_dict(cursor)
    if inserted is not None:
        return _decode_record(inserted)
    cursor.execute(_SELECT_GRANT_BY_ID_FOR_UPDATE_SQL, (content["id"],))
    existing = _fetchone_dict(cursor)
    if existing is None:
        raise ProductGrowthIntegrityError(
            "reward grant conflict exposed no deterministic grant"
        )
    record = _decode_record(existing)
    if not hmac.compare_digest(
        str(record.get("content_sha256") or ""),
        str(content["content_sha256"]),
    ):
        raise ProductGrowthConflict("reward grant replay content changed")
    return record


def _refund_event_payload(
    *,
    tenant_id: str,
    owner_user_id: str,
    invite: Mapping[str, Any],
    source_event_id: str,
    source_order_id: str,
    refund_cents: int,
    cumulative_refunded_cents: int,
    cumulative_refunded_points: int,
    source_payload: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "eventType": EVENT_FIRST_RECHARGE_REFUND,
        "tenantId": tenant_id,
        "ownerUserId": owner_user_id,
        "inviteRelationId": str(invite["id"]),
        "inviterUserId": str(invite["inviter_user_id"]),
        "inviteeUserId": str(invite["invitee_user_id"]),
        "sourceEventId": source_event_id,
        "sourceOrderId": source_order_id,
        "refundCents": refund_cents,
        "cumulativeRefundedCents": cumulative_refunded_cents,
        "cumulativeRefundedPoints": cumulative_refunded_points,
        "ruleVersion": str(invite["rule_version"]),
        "source": dict(source_payload),
    }


def _refund_event_record(
    *,
    event_id: str,
    tenant_id: str,
    owner_user_id: str,
    idempotency_key: str,
    source_event_id: str,
    source_order_id: str,
    invite: Mapping[str, Any],
    payload: Mapping[str, Any],
    outcome: str,
    outcome_reason: str,
    occurred_at: datetime,
    canonical_first_order_id: str,
    original_event_id: str,
    original_grant_id: str,
    previous_cumulative_refunded_cents: int,
    cumulative_refunded_cents: int,
    previous_reversed_points: int,
    cumulative_reversed_points: int,
    previous_recovered_points: int,
    cumulative_recovered_points: int,
    previous_reward_outstanding_points: int,
    reward_outstanding_points_after: int,
    owner_debt_outstanding_points_after: int,
    reversal_spec: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if outcome not in {"applied", "ignored"}:
        raise InvalidProductGrowthInput("invalid refund event outcome")
    if (outcome == "applied") == bool(outcome_reason):
        raise InvalidProductGrowthInput(
            "applied refunds need no reason and ignored refunds need a reason"
        )
    frozen_reversals = []
    if reversal_spec is not None:
        frozen_reversals.append(
            {
                "reversalId": str(reversal_spec["reversal_id"]),
                "ownerUserId": str(reversal_spec["owner_user_id"]),
                "assessedPoints": int(reversal_spec["assessed_points"]),
                "recoveredPoints": int(reversal_spec["recovered_points"]),
                "outstandingPointsAdded": int(
                    reversal_spec["outstanding_points_added"]
                ),
                "outstandingPointsAfter": int(
                    reversal_spec["outstanding_points_after"]
                ),
                "walletOrderId": reversal_spec["wallet_order_id"],
            }
        )
    result = {
        "schemaVersion": 1,
        "businessEventId": event_id,
        "outcome": outcome,
        "reason": outcome_reason,
        "canonicalFirstOrderId": canonical_first_order_id,
        "originalRewardEventId": original_event_id,
        "originalRewardGrantId": original_grant_id,
        "previousCumulativeRefundedCents": (
            previous_cumulative_refunded_cents
        ),
        "cumulativeRefundedCents": cumulative_refunded_cents,
        "previousReversedPoints": previous_reversed_points,
        "cumulativeReversedPoints": cumulative_reversed_points,
        "previousRecoveredPoints": previous_recovered_points,
        "cumulativeRecoveredPoints": cumulative_recovered_points,
        "previousRewardOutstandingPoints": (
            previous_reward_outstanding_points
        ),
        "rewardOutstandingPointsAfter": reward_outstanding_points_after,
        "pointsAssessed": (
            0
            if reversal_spec is None
            else int(reversal_spec["assessed_points"])
        ),
        "pointsRecovered": (
            0
            if reversal_spec is None
            else int(reversal_spec["recovered_points"])
        ),
        "outstandingPointsAdded": (
            0
            if reversal_spec is None
            else int(reversal_spec["outstanding_points_added"])
        ),
        "ownerDebtOutstandingPointsAfter": (
            owner_debt_outstanding_points_after
        ),
        "rewardReversals": frozen_reversals,
    }
    return {
        "id": event_id,
        "tenant_id": tenant_id,
        "owner_user_id": owner_user_id,
        "idempotency_key": idempotency_key,
        "source_event_id": source_event_id,
        "business_key": source_event_id,
        "event_type": EVENT_FIRST_RECHARGE_REFUND,
        "invite_relation_id": str(invite["id"]),
        "source_order_id": source_order_id,
        "rule_version": str(invite["rule_version"]),
        "payload": dict(payload),
        "payload_sha256": _content_sha256(payload),
        "outcome": outcome,
        "outcome_reason": outcome_reason,
        "result_payload": result,
        "result_sha256": _content_sha256(result),
        "occurred_at": occurred_at,
    }


def _find_refund_input_replay(
    cursor: CursorLike,
    *,
    tenant_id: str,
    owner_user_id: str,
    idempotency_key: str,
    source_event_id: str,
    business_key: str,
    payload: Mapping[str, Any],
) -> dict[str, Any] | None:
    cursor.execute(
        _SELECT_EVENT_BY_IDEMPOTENCY_FOR_UPDATE_SQL,
        (tenant_id, owner_user_id, idempotency_key),
    )
    row = _fetchone_dict(cursor)
    if row is not None:
        event = _decode_record(row)
        _validate_refund_input_replay(
            event,
            owner_user_id=owner_user_id,
            idempotency_key=idempotency_key,
            source_event_id=source_event_id,
            business_key=business_key,
            payload=payload,
            require_idempotency_key=True,
        )
        return event

    cursor.execute(
        _SELECT_EVENT_BY_SOURCE_FOR_UPDATE_SQL,
        (tenant_id, source_event_id),
    )
    row = _fetchone_dict(cursor)
    if row is not None:
        event = _decode_record(row)
        _validate_refund_input_replay(
            event,
            owner_user_id=owner_user_id,
            idempotency_key=idempotency_key,
            source_event_id=source_event_id,
            business_key=business_key,
            payload=payload,
            require_idempotency_key=False,
        )
        return event

    cursor.execute(
        _SELECT_EVENT_BY_BUSINESS_FOR_UPDATE_SQL,
        (tenant_id, EVENT_FIRST_RECHARGE_REFUND, business_key),
    )
    row = _fetchone_dict(cursor)
    if row is None:
        return None
    event = _decode_record(row)
    _validate_refund_input_replay(
        event,
        owner_user_id=owner_user_id,
        idempotency_key=idempotency_key,
        source_event_id=source_event_id,
        business_key=business_key,
        payload=payload,
        require_idempotency_key=False,
    )
    return event


def _validate_refund_input_replay(
    event: Mapping[str, Any],
    *,
    owner_user_id: str,
    idempotency_key: str,
    source_event_id: str,
    business_key: str,
    payload: Mapping[str, Any],
    require_idempotency_key: bool,
) -> None:
    checks = (
        str(event.get("owner_user_id") or "") == owner_user_id,
        str(event.get("event_type") or "") == EVENT_FIRST_RECHARGE_REFUND,
        str(event.get("source_event_id") or "") == source_event_id,
        str(event.get("business_key") or "") == business_key,
        hmac.compare_digest(
            str(event.get("payload_sha256") or ""),
            _content_sha256(payload),
        ),
        (event.get("payload") or {}) == dict(payload),
    )
    if require_idempotency_key:
        checks += (
            str(event.get("idempotency_key") or "") == idempotency_key,
        )
    if not all(checks):
        raise ProductGrowthConflict(
            "refund business event replay content changed"
        )


def _persist_refund_event(
    cursor: CursorLike,
    *,
    expected: Mapping[str, Any],
    invite: Mapping[str, Any],
    reversal_state: Mapping[str, Any] | None,
    reversal_spec: Mapping[str, Any] | None,
    actor_user_id: str,
) -> GrowthRefundApplyResult:
    cursor.execute(
        _INSERT_EVENT_SQL,
        (
            expected["id"],
            expected["tenant_id"],
            expected["owner_user_id"],
            expected["idempotency_key"],
            expected["source_event_id"],
            expected["business_key"],
            expected["event_type"],
            expected["invite_relation_id"],
            expected["source_order_id"],
            expected["rule_version"],
            _canonical_json(expected["payload"]),
            expected["payload_sha256"],
            expected["outcome"],
            expected["outcome_reason"],
            _canonical_json(expected["result_payload"]),
            expected["result_sha256"],
            expected["occurred_at"],
        ),
    )
    inserted = _fetchone_dict(cursor)
    if inserted is None:
        replay = _find_refund_input_replay(
            cursor,
            tenant_id=str(expected["tenant_id"]),
            owner_user_id=str(expected["owner_user_id"]),
            idempotency_key=str(expected["idempotency_key"]),
            source_event_id=str(expected["source_event_id"]),
            business_key=str(expected["business_key"]),
            payload=expected["payload"],
        )
        if replay is None:
            raise ProductGrowthConflict(
                "refund event insert conflicted without a resolvable event"
            )
        _require_exact_event_replay(replay, expected)
        return _replayed_refund_result(cursor, replay)

    event = _decode_record(inserted)
    account: dict[str, Any] | None = None
    reversals: tuple[dict[str, Any], ...] = ()
    if reversal_spec is not None:
        if reversal_state is None:
            raise ProductGrowthIntegrityError(
                "refund reversal has no locked cumulative state"
            )
        if int(reversal_spec["recovered_points"]) > 0:
            account = _debit_growth_reward(
                cursor,
                tenant_id=str(event["tenant_id"]),
                event=event,
                state=reversal_state,
                spec=reversal_spec,
            )
        reversal = _insert_reward_reversal(
            cursor,
            event=event,
            state=reversal_state,
            spec=reversal_spec,
        )
        reversals = (reversal,)
        _advance_reward_debt(
            cursor,
            debt=reversal_spec["debt"],
            assessed_points=int(reversal_spec["assessed_points"]),
            recovered_points=int(reversal_spec["recovered_points"]),
        )

    result_payload = event["result_payload"]
    if reversal_state is not None and str(event["outcome"]) == "applied":
        _advance_reversal_state(
            cursor,
            state=reversal_state,
            cumulative_refunded_cents=int(
                result_payload["cumulativeRefundedCents"]
            ),
            cumulative_reversed_points=int(
                result_payload["cumulativeReversedPoints"]
            ),
            cumulative_recovered_points=int(
                result_payload["cumulativeRecoveredPoints"]
            ),
            outstanding_points=int(
                result_payload["rewardOutstandingPointsAfter"]
            ),
        )

    _append_audit(
        cursor,
        tenant_id=str(event["tenant_id"]),
        owner_user_id=str(event["owner_user_id"]),
        actor_user_id=actor_user_id,
        action="growth.consumer.first_recharge_refund.processed",
        target_type="growth_business_event",
        target_id=str(event["id"]),
        status=(
            "succeeded" if str(event["outcome"]) == "applied" else "ignored"
        ),
        metadata={
            "sourceEventId": event["source_event_id"],
            "sourceOrderId": event["source_order_id"],
            "ruleVersion": event["rule_version"],
            "outcomeReason": event["outcome_reason"],
            "pointsAssessed": result_payload["pointsAssessed"],
            "pointsRecovered": result_payload["pointsRecovered"],
            "outstandingPointsAdded": result_payload[
                "outstandingPointsAdded"
            ],
            "ownerDebtOutstandingPointsAfter": result_payload[
                "ownerDebtOutstandingPointsAfter"
            ],
            "cumulativeRefundedCents": result_payload[
                "cumulativeRefundedCents"
            ],
            "cumulativeReversedPoints": result_payload[
                "cumulativeReversedPoints"
            ],
        },
    )
    return GrowthRefundApplyResult(
        event=event,
        reversals=reversals,
        wallet_account=account,
        cumulative_refunded_cents=int(
            result_payload["cumulativeRefundedCents"]
        ),
        cumulative_reversed_points=int(
            result_payload["cumulativeReversedPoints"]
        ),
        points_assessed=int(result_payload["pointsAssessed"]),
        points_recovered=int(result_payload["pointsRecovered"]),
        outstanding_points_added=int(
            result_payload["outstandingPointsAdded"]
        ),
        outstanding_points_after=int(
            result_payload["ownerDebtOutstandingPointsAfter"]
        ),
        applied=str(event["outcome"]) == "applied",
        idempotent=False,
    )


def _replayed_refund_result(
    cursor: CursorLike,
    event: Mapping[str, Any],
) -> GrowthRefundApplyResult:
    cursor.execute(
        _SELECT_REVERSAL_BY_EVENT_FOR_UPDATE_SQL,
        (event["tenant_id"], event["id"]),
    )
    row = _fetchone_dict(cursor)
    reversals = () if row is None else (_decode_record(row),)
    result = event.get("result_payload") or {}
    expected_reversals = list(result.get("rewardReversals") or [])
    if len(reversals) != len(expected_reversals):
        raise ProductGrowthIntegrityError(
            "refund event reversal count does not match its result"
        )
    if reversals:
        reversal = reversals[0]
        expected = expected_reversals[0]
        if (
            str(reversal["id"]) != str(expected["reversalId"])
            or str(reversal["owner_user_id"])
            != str(expected["ownerUserId"])
            or int(reversal["assessed_points"])
            != int(expected["assessedPoints"])
            or int(reversal["recovered_points"])
            != int(expected["recoveredPoints"])
            or int(reversal["outstanding_points_added"])
            != int(expected["outstandingPointsAdded"])
            or (reversal.get("wallet_order_id") or None)
            != expected["walletOrderId"]
        ):
            raise ProductGrowthIntegrityError(
                "refund event reversal content changed"
            )
    return GrowthRefundApplyResult(
        event=dict(event),
        reversals=reversals,
        wallet_account=None,
        cumulative_refunded_cents=int(
            result.get("cumulativeRefundedCents") or 0
        ),
        cumulative_reversed_points=int(
            result.get("cumulativeReversedPoints") or 0
        ),
        points_assessed=int(result.get("pointsAssessed") or 0),
        points_recovered=int(result.get("pointsRecovered") or 0),
        outstanding_points_added=int(
            result.get("outstandingPointsAdded") or 0
        ),
        outstanding_points_after=int(
            result.get("ownerDebtOutstandingPointsAfter") or 0
        ),
        applied=str(event.get("outcome") or "") == "applied",
        idempotent=True,
    )


def _ensure_reversal_state(
    cursor: CursorLike,
    *,
    tenant_id: str,
    owner_user_id: str,
    invite: Mapping[str, Any],
    source_order_id: str,
    original_event: Mapping[str, Any],
    original_grant: Mapping[str, Any],
    original_paid_cents: int,
) -> dict[str, Any]:
    state_id = stable_growth_id(
        "growth_reversal_state",
        tenant_id,
        source_order_id,
    )
    cursor.execute(
        _INSERT_REVERSAL_STATE_SQL,
        (
            state_id,
            tenant_id,
            owner_user_id,
            invite["id"],
            source_order_id,
            original_event["id"],
            original_grant["id"],
            original_grant["owner_user_id"],
            original_paid_cents,
            original_grant["points"],
        ),
    )
    inserted = _fetchone_dict(cursor)
    if inserted is not None:
        return _decode_record(inserted)
    cursor.execute(
        _SELECT_REVERSAL_STATE_FOR_UPDATE_SQL,
        (tenant_id, owner_user_id, source_order_id),
    )
    row = _fetchone_dict(cursor)
    if row is None:
        raise ProductGrowthIntegrityError(
            "refund state conflict exposed no cumulative state"
        )
    state = _decode_record(row)
    checks = (
        str(state["id"]) == state_id,
        str(state["invite_relation_id"]) == str(invite["id"]),
        str(state["original_business_event_id"])
        == str(original_event["id"]),
        str(state["original_reward_grant_id"])
        == str(original_grant["id"]),
        str(state["reward_owner_user_id"])
        == str(original_grant["owner_user_id"]),
        int(state["original_paid_cents"]) == original_paid_cents,
        int(state["original_reward_points"]) == int(original_grant["points"]),
    )
    if not all(checks):
        raise ProductGrowthIntegrityError(
            "refund cumulative state no longer matches the original reward"
        )
    return state


def _advance_reversal_state(
    cursor: CursorLike,
    *,
    state: Mapping[str, Any],
    cumulative_refunded_cents: int,
    cumulative_reversed_points: int,
    cumulative_recovered_points: int,
    outstanding_points: int,
) -> dict[str, Any]:
    old_cumulative = int(state["max_cumulative_refunded_cents"])
    old_reversed = int(state["reversed_points"])
    old_recovered = int(state["recovered_points"])
    old_outstanding = int(state["outstanding_points"])
    if cumulative_refunded_cents < old_cumulative:
        raise ProductGrowthIntegrityError(
            "refund cumulative cents attempted to move backwards"
        )
    if cumulative_reversed_points < old_reversed:
        raise ProductGrowthIntegrityError(
            "refund cumulative points attempted to move backwards"
        )
    if cumulative_recovered_points < old_recovered:
        raise ProductGrowthIntegrityError(
            "refund recovered points attempted to move backwards"
        )
    if cumulative_recovered_points + outstanding_points != (
        cumulative_reversed_points
    ):
        raise ProductGrowthIntegrityError(
            "refund recovered and outstanding points do not balance"
        )
    if cumulative_refunded_cents > int(state["original_paid_cents"]):
        raise ProductGrowthIntegrityError(
            "refund cumulative cents exceeded the original paid amount"
        )
    if cumulative_reversed_points > int(state["original_reward_points"]):
        raise ProductGrowthIntegrityError(
            "refund cumulative points exceeded the original reward"
        )
    cursor.execute(
        _UPDATE_REVERSAL_STATE_SQL,
        (
            cumulative_refunded_cents,
            cumulative_reversed_points,
            cumulative_recovered_points,
            outstanding_points,
            state["id"],
            state["tenant_id"],
            state["owner_user_id"],
            int(state["version"]),
            old_cumulative,
            old_reversed,
            old_recovered,
            old_outstanding,
        ),
    )
    row = _fetchone_dict(cursor)
    if row is None:
        raise ProductGrowthStateConflict(
            "refund cumulative state update lost its row lock"
        )
    return _decode_record(row)


def _prepare_reversal_spec(
    cursor: CursorLike,
    *,
    tenant_id: str,
    event_id: str,
    reward_owner_user_id: str,
    assessed_points: int,
) -> dict[str, Any]:
    owner = _identifier(reward_owner_user_id, "reward_owner_user_id")
    assessed = _positive_int(assessed_points, "assessed_points")
    debt = _locked_reward_debt(cursor, tenant_id, owner)
    cursor.execute(_SELECT_POINT_ACCOUNT_FOR_UPDATE_SQL, (owner,))
    account_row = _fetchone_dict(cursor)
    if account_row is None:
        raise ProductGrowthIntegrityError(
            "reward reversal wallet account is missing"
        )
    account = _decode_record(account_row)
    recovered = min(int(account["balance_points"]), assessed)
    outstanding_added = assessed - recovered
    outstanding_after = int(debt["outstanding_points"]) + outstanding_added
    return {
        "reversal_id": stable_growth_id(
            "growth_reversal",
            tenant_id,
            event_id,
            owner,
        ),
        "owner_user_id": owner,
        "assessed_points": assessed,
        "recovered_points": recovered,
        "outstanding_points_added": outstanding_added,
        "outstanding_points_after": outstanding_after,
        "wallet_order_id": (
            stable_growth_id(
                "growth_debit",
                tenant_id,
                event_id,
                owner,
            )
            if recovered > 0
            else None
        ),
        "debt": debt,
    }


def _locked_reward_debt(
    cursor: CursorLike,
    tenant_id: str,
    owner_user_id: str,
) -> dict[str, Any]:
    cursor.execute(
        _INSERT_REWARD_DEBT_SQL,
        (tenant_id, owner_user_id),
    )
    inserted = _fetchone_dict(cursor)
    if inserted is not None:
        return _decode_record(inserted)
    cursor.execute(
        _SELECT_REWARD_DEBT_FOR_UPDATE_SQL,
        (tenant_id, owner_user_id),
    )
    row = _fetchone_dict(cursor)
    if row is None:
        raise ProductGrowthIntegrityError(
            "reward debt lock row could not be resolved"
        )
    return _decode_record(row)


def _recover_growth_reward_debt(
    cursor: CursorLike,
    *,
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    debt = plan["debt"]
    recovered = int(plan["debt_offset_points"])
    if recovered == 0:
        return dict(debt)
    outstanding_before = int(debt["outstanding_points"])
    if recovered > outstanding_before:
        raise ProductGrowthIntegrityError(
            "growth reward debt offset exceeded locked outstanding debt"
        )
    cursor.execute(
        _RECOVER_REWARD_DEBT_SQL,
        (
            recovered,
            recovered,
            debt["tenant_id"],
            debt["owner_user_id"],
            int(debt["version"]),
            outstanding_before,
            int(debt["lifetime_assessed_points"]),
            int(debt["lifetime_recovered_points"]),
            recovered,
        ),
    )
    row = _fetchone_dict(cursor)
    if row is None:
        raise ProductGrowthStateConflict(
            "growth reward debt recovery lost its locked row"
        )
    updated = _decode_record(row)
    if (
        int(updated["outstanding_points"])
        != outstanding_before - recovered
        or int(updated["lifetime_assessed_points"])
        != int(debt["lifetime_assessed_points"])
        or int(updated["lifetime_recovered_points"])
        != int(debt["lifetime_recovered_points"]) + recovered
    ):
        raise ProductGrowthIntegrityError(
            "growth reward debt recovery changed cumulative totals"
        )
    return updated


def _insert_reward_debt_recovery(
    cursor: CursorLike,
    *,
    event: Mapping[str, Any],
    grant: Mapping[str, Any],
    plan: Mapping[str, Any],
    updated_debt: Mapping[str, Any],
) -> dict[str, Any]:
    metadata = {
        "sourceEventId": str(event["source_event_id"]),
        "sourceOrderId": str(event["source_order_id"]),
        "eventType": str(event["event_type"]),
        "recoverySource": "growth_reward",
    }
    content = {
        "id": str(plan["debt_recovery_id"]),
        "tenant_id": str(event["tenant_id"]),
        "owner_user_id": str(plan["owner_user_id"]),
        "business_event_id": str(event["id"]),
        "event_owner_user_id": str(event["owner_user_id"]),
        "reward_grant_id": str(grant["id"]),
        "reward_kind": str(plan["reward_kind"]),
        "gross_reward_points": int(plan["gross_reward_points"]),
        "recovered_points": int(plan["debt_offset_points"]),
        "net_wallet_points": int(plan["net_wallet_points"]),
        "debt_outstanding_before": int(
            plan["debt"]["outstanding_points"]
        ),
        "debt_outstanding_after": int(
            updated_debt["outstanding_points"]
        ),
        "rule_version": str(event["rule_version"]),
        "metadata": metadata,
    }
    content["content_sha256"] = _content_sha256(content)
    cursor.execute(
        _INSERT_DEBT_RECOVERY_SQL,
        (
            content["id"],
            content["tenant_id"],
            content["owner_user_id"],
            content["business_event_id"],
            content["event_owner_user_id"],
            content["reward_grant_id"],
            content["reward_kind"],
            content["gross_reward_points"],
            content["recovered_points"],
            content["net_wallet_points"],
            content["debt_outstanding_before"],
            content["debt_outstanding_after"],
            content["rule_version"],
            content["content_sha256"],
            _canonical_json(content["metadata"]),
        ),
    )
    inserted = _fetchone_dict(cursor)
    if inserted is not None:
        return _decode_record(inserted)
    cursor.execute(
        _SELECT_DEBT_RECOVERY_BY_GRANT_FOR_UPDATE_SQL,
        (content["reward_grant_id"],),
    )
    row = _fetchone_dict(cursor)
    if row is None:
        raise ProductGrowthIntegrityError(
            "growth debt recovery conflict exposed no immutable evidence"
        )
    recovery = _decode_record(row)
    if not hmac.compare_digest(
        str(recovery.get("content_sha256") or ""),
        str(content["content_sha256"]),
    ):
        raise ProductGrowthConflict(
            "growth debt recovery replay content changed"
        )
    return recovery


def _advance_reward_debt(
    cursor: CursorLike,
    *,
    debt: Mapping[str, Any],
    assessed_points: int,
    recovered_points: int,
) -> dict[str, Any]:
    assessed = _positive_int(assessed_points, "assessed_points")
    recovered = _nonnegative_int(recovered_points, "recovered_points")
    if recovered > assessed:
        raise ProductGrowthIntegrityError(
            "recovered points exceeded assessed points"
        )
    outstanding_added = assessed - recovered
    cursor.execute(
        _UPDATE_REWARD_DEBT_SQL,
        (
            outstanding_added,
            assessed,
            recovered,
            debt["tenant_id"],
            debt["owner_user_id"],
            int(debt["version"]),
            int(debt["outstanding_points"]),
            int(debt["lifetime_assessed_points"]),
            int(debt["lifetime_recovered_points"]),
        ),
    )
    row = _fetchone_dict(cursor)
    if row is None:
        raise ProductGrowthStateConflict(
            "reward debt update lost its row lock"
        )
    updated = _decode_record(row)
    if int(updated["outstanding_points"]) != (
        int(debt["outstanding_points"]) + outstanding_added
    ):
        raise ProductGrowthIntegrityError(
            "reward debt outstanding balance changed unexpectedly"
        )
    return updated


def _debit_growth_reward(
    cursor: CursorLike,
    *,
    tenant_id: str,
    event: Mapping[str, Any],
    state: Mapping[str, Any],
    spec: Mapping[str, Any],
) -> dict[str, Any]:
    owner = str(spec["owner_user_id"])
    points = int(spec["recovered_points"])
    wallet_order_id = spec.get("wallet_order_id")
    if points <= 0 or not wallet_order_id:
        raise ProductGrowthIntegrityError(
            "wallet debit requires positive recovered points"
        )
    wallet_order_id = str(wallet_order_id)
    metadata = {
        "domain": "growth",
        "tenantId": tenant_id,
        "growthEventId": str(event["id"]),
        "reversalStateId": str(state["id"]),
        "originalRewardGrantId": str(state["original_reward_grant_id"]),
        "rewardKind": "first_recharge_inviter",
        "ruleVersion": str(event["rule_version"]),
    }
    wallet_content = {
        "id": wallet_order_id,
        "ownerUserId": owner,
        "orderKind": "debit",
        "points": points,
        "metadata": metadata,
    }
    request_sha256 = _content_sha256(wallet_content)
    cursor.execute(_SELECT_POINT_ACCOUNT_FOR_UPDATE_SQL, (owner,))
    account_row = _fetchone_dict(cursor)
    if account_row is None:
        raise ProductGrowthIntegrityError(
            "reward reversal wallet account is missing"
        )
    account = _decode_record(account_row)
    if int(account["balance_points"]) < points:
        raise ProductGrowthStateConflict(
            "reward owner balance changed under the wallet row lock"
        )

    cursor.execute(
        _INSERT_POINT_DEBIT_ORDER_SQL,
        (
            wallet_order_id,
            owner,
            points,
            request_sha256,
            _canonical_json(metadata),
        ),
    )
    order_row = _fetchone_dict(cursor)
    if order_row is None:
        cursor.execute(
            _SELECT_POINT_ORDER_FOR_UPDATE_SQL,
            (wallet_order_id,),
        )
        existing_order = _fetchone_dict(cursor)
        if existing_order is None:
            raise ProductGrowthIntegrityError(
                "reward debit order conflict exposed no row"
            )
        _validate_wallet_order_replay(
            _decode_record(existing_order),
            owner_user_id=owner,
            points=points,
            request_sha256=request_sha256,
            metadata=metadata,
            order_kind="debit",
        )
        cursor.execute(
            _SELECT_POINT_LEDGER_FOR_UPDATE_SQL,
            (wallet_order_id,),
        )
        ledger_row = _fetchone_dict(cursor)
        if ledger_row is None:
            raise ProductGrowthIntegrityError(
                "reward debit order is missing its ledger row"
            )
        _validate_wallet_ledger_replay(
            _decode_record(ledger_row),
            owner_user_id=owner,
            wallet_order_id=str(wallet_order_id),
            points=points,
            request_sha256=request_sha256,
            metadata=metadata,
            order_kind="debit",
        )
        return account

    cursor.execute(
        _DEBIT_POINT_ACCOUNT_SQL,
        (points, points, owner, points),
    )
    debited_row = _fetchone_dict(cursor)
    if debited_row is None:
        raise ProductGrowthStateConflict(
            "reward owner balance changed before the clawback debit"
        )
    debited = _decode_record(debited_row)
    cursor.execute(
        _INSERT_POINT_DEBIT_LEDGER_SQL,
        (
            owner,
            wallet_order_id,
            points,
            -points,
            int(debited["balance_points"]),
            request_sha256,
            _canonical_json(metadata),
        ),
    )
    if _fetchone_dict(cursor) is None:
        raise ProductGrowthIntegrityError(
            "reward debit ledger insert returned no row"
        )
    return debited


def _insert_reward_reversal(
    cursor: CursorLike,
    *,
    event: Mapping[str, Any],
    state: Mapping[str, Any],
    spec: Mapping[str, Any],
) -> dict[str, Any]:
    result = event["result_payload"]
    metadata = {
        "sourceEventId": str(event["source_event_id"]),
        "sourceOrderId": str(event["source_order_id"]),
        "previousReversedPoints": int(result["previousReversedPoints"]),
    }
    content = {
        "id": str(spec["reversal_id"]),
        "tenant_id": str(event["tenant_id"]),
        "owner_user_id": str(spec["owner_user_id"]),
        "business_event_id": str(event["id"]),
        "event_owner_user_id": str(event["owner_user_id"]),
        "reversal_state_id": str(state["id"]),
        "original_reward_grant_id": str(
            state["original_reward_grant_id"]
        ),
        "reversal_kind": "first_recharge_inviter",
        "assessed_points": int(spec["assessed_points"]),
        "recovered_points": int(spec["recovered_points"]),
        "outstanding_points_added": int(
            spec["outstanding_points_added"]
        ),
        "outstanding_points_after": int(
            spec["outstanding_points_after"]
        ),
        "wallet_order_id": spec.get("wallet_order_id"),
        "cumulative_refunded_cents": int(
            result["cumulativeRefundedCents"]
        ),
        "cumulative_reversed_points": int(
            result["cumulativeReversedPoints"]
        ),
        "rule_version": str(event["rule_version"]),
        "metadata": metadata,
    }
    content["content_sha256"] = _content_sha256(content)
    cursor.execute(
        _INSERT_REVERSAL_SQL,
        (
            content["id"],
            content["tenant_id"],
            content["owner_user_id"],
            content["business_event_id"],
            content["event_owner_user_id"],
            content["reversal_state_id"],
            content["original_reward_grant_id"],
            content["reversal_kind"],
            content["assessed_points"],
            content["recovered_points"],
            content["outstanding_points_added"],
            content["outstanding_points_after"],
            content["wallet_order_id"],
            content["cumulative_refunded_cents"],
            content["cumulative_reversed_points"],
            content["rule_version"],
            content["content_sha256"],
            _canonical_json(content["metadata"]),
        ),
    )
    inserted = _fetchone_dict(cursor)
    if inserted is not None:
        return _decode_record(inserted)
    cursor.execute(
        _SELECT_REVERSAL_BY_EVENT_FOR_UPDATE_SQL,
        (content["tenant_id"], content["business_event_id"]),
    )
    row = _fetchone_dict(cursor)
    if row is None:
        raise ProductGrowthIntegrityError(
            "reward reversal conflict exposed no row"
        )
    reversal = _decode_record(row)
    if not hmac.compare_digest(
        str(reversal.get("content_sha256") or ""),
        str(content["content_sha256"]),
    ):
        raise ProductGrowthConflict("reward reversal replay content changed")
    return reversal


def _lock_subject(
    cursor: CursorLike,
    tenant_id: str,
    owner_user_id: str,
) -> None:
    cursor.execute(
        _INSERT_SUBJECT_LOCK_SQL,
        (tenant_id, owner_user_id),
    )
    cursor.execute(
        _SELECT_SUBJECT_LOCK_FOR_UPDATE_SQL,
        (tenant_id, owner_user_id),
    )
    if _fetchone_dict(cursor) is None:
        raise ProductGrowthIntegrityError(
            "growth subject lock row could not be resolved"
        )


def _locked_invite(
    cursor: CursorLike,
    invite_relation_id: str,
    tenant_id: str,
    owner_user_id: str,
) -> dict[str, Any]:
    cursor.execute(
        _SELECT_INVITE_BY_ID_FOR_UPDATE_SQL,
        (invite_relation_id, tenant_id, owner_user_id),
    )
    row = _fetchone_dict(cursor)
    if row is None:
        raise ProductGrowthNotFound(
            "invite relation not found for tenant owner"
        )
    invite = _decode_record(row)
    if (
        str(invite["owner_user_id"]) != owner_user_id
        or str(invite["invitee_user_id"]) != owner_user_id
        or str(invite["tenant_id"]) != tenant_id
    ):
        raise ProductGrowthIntegrityError(
            "invite relation crossed its tenant owner boundary"
        )
    return invite


def _locked_payment_order(
    cursor: CursorLike,
    source_order_id: str,
    owner_user_id: str,
) -> dict[str, Any]:
    cursor.execute(
        _SELECT_PAYMENT_ORDER_FOR_UPDATE_SQL,
        (source_order_id, owner_user_id),
    )
    row = _fetchone_dict(cursor)
    if row is None:
        raise ProductGrowthNotFound(
            "payment order not found for growth event owner"
        )
    payment = _decode_record(row)
    if str(payment["owner_user_id"]) != owner_user_id:
        raise ProductGrowthNotFound(
            "payment order not found for growth event owner"
        )
    if str(payment["status"]) not in {
        "paid",
        "partially_refunded",
        "refunded",
    }:
        raise ProductGrowthStateConflict(
            "payment order is not a successful cash payment"
        )
    if payment.get("paid_at") is None:
        raise ProductGrowthIntegrityError(
            "successful payment order is missing paid_at"
        )
    return payment


def _first_recharge_points(
    *,
    paid_cents: int,
    invite: Mapping[str, Any],
) -> int:
    cents = _nonnegative_int(paid_cents, "paid_cents")
    cash_points = (
        cents
        * int(invite["cash_points_per_yuan"])
        // int(invite["cents_per_yuan"])
    )
    return (
        cash_points
        * int(invite["first_recharge_rebate_percent"])
        // 100
    )


def _append_audit(
    cursor: CursorLike,
    *,
    tenant_id: str,
    owner_user_id: str,
    actor_user_id: str,
    action: str,
    target_type: str,
    target_id: str,
    status: str,
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    tenant = _tenant_id(tenant_id)
    owner = _identifier(owner_user_id, "owner_user_id")
    actor = _identifier(actor_user_id, "actor_user_id")
    clean_action = _identifier(action, "action")
    clean_target_type = _identifier(target_type, "target_type")
    clean_target_id = _identifier(target_id, "target_id")
    if status not in {"succeeded", "ignored"}:
        raise InvalidProductGrowthInput("invalid growth audit status")
    clean_metadata = _bounded_json_object(metadata, "audit metadata")
    action_id = stable_growth_id(
        "growth_audit",
        tenant,
        clean_action,
        clean_target_type,
        clean_target_id,
    )
    content = {
        "action_id": action_id,
        "tenant_id": tenant,
        "owner_user_id": owner,
        "actor_user_id": actor,
        "action": clean_action,
        "target_type": clean_target_type,
        "target_id": clean_target_id,
        "status": status,
        "metadata": clean_metadata,
    }
    content["content_sha256"] = _content_sha256(content)
    cursor.execute(
        _INSERT_AUDIT_SQL,
        (
            content["action_id"],
            content["tenant_id"],
            content["owner_user_id"],
            content["actor_user_id"],
            content["action"],
            content["target_type"],
            content["target_id"],
            content["status"],
            _canonical_json(content["metadata"]),
            content["content_sha256"],
        ),
    )
    inserted = _fetchone_dict(cursor)
    if inserted is not None:
        return _decode_record(inserted)
    cursor.execute(
        _SELECT_AUDIT_FOR_UPDATE_SQL,
        (action_id, tenant),
    )
    row = _fetchone_dict(cursor)
    if row is None:
        raise ProductGrowthIntegrityError(
            "growth audit conflict exposed no immutable audit row"
        )
    existing = _decode_record(row)
    if not hmac.compare_digest(
        str(existing.get("content_sha256") or ""),
        str(content["content_sha256"]),
    ):
        raise ProductGrowthConflict("growth audit replay content changed")
    return existing


def _require_exact_request_replay(
    existing: Mapping[str, Any],
    expected: Mapping[str, Any],
    label: str,
) -> None:
    checks = (
        str(existing.get("id") or "") == str(expected["id"]),
        str(existing.get("tenant_id") or "") == str(expected["tenant_id"]),
        str(existing.get("owner_user_id") or "")
        == str(expected["owner_user_id"]),
        str(existing.get("idempotency_key") or "")
        == str(expected["idempotency_key"]),
        hmac.compare_digest(
            str(existing.get("request_sha256") or ""),
            str(expected["request_sha256"]),
        ),
    )
    if not all(checks):
        raise ProductGrowthConflict(f"{label} idempotency replay changed")


def _require_logical_agent_replay(
    existing: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> None:
    fields = (
        "id",
        "tenant_id",
        "owner_user_id",
        "agent_code",
        "status",
        "rule_version",
        "commission_depth",
        "first_order_commission_bps",
        "repeat_order_commission_bps",
        "metadata",
    )
    if not all(existing.get(field) == expected.get(field) for field in fields):
        raise ProductGrowthConflict(
            "tenant user already has a different agent profile"
        )


def _require_logical_binding_replay(
    existing: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> None:
    fields = (
        "id",
        "tenant_id",
        "owner_user_id",
        "agent_id",
        "agent_owner_user_id",
        "customer_user_id",
        "relation_depth",
        "source",
        "rule_version",
        "first_order_commission_bps",
        "repeat_order_commission_bps",
        "metadata",
    )
    if not all(existing.get(field) == expected.get(field) for field in fields):
        raise ProductGrowthConflict(
            "customer already belongs to a different direct agent"
        )


def _require_logical_invite_replay(
    existing: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> None:
    fields = (
        "id",
        "tenant_id",
        "owner_user_id",
        "inviter_user_id",
        "invitee_user_id",
        "invite_code_id",
        "invite_code_sha256",
        "relation_depth",
        "rule_version",
        "registration_inviter_points",
        "registration_invitee_points",
        "first_recharge_rebate_percent",
        "cash_points_per_yuan",
        "cents_per_yuan",
        "risk_snapshot",
        "metadata",
    )
    if not all(existing.get(field) == expected.get(field) for field in fields):
        raise ProductGrowthConflict(
            "invitee already belongs to a different inviter"
        )


def _require_exact_event_input_replay(
    existing: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> None:
    checks = (
        str(existing.get("id") or "") == str(expected["id"]),
        str(existing.get("tenant_id") or "") == str(expected["tenant_id"]),
        str(existing.get("owner_user_id") or "")
        == str(expected["owner_user_id"]),
        str(existing.get("idempotency_key") or "")
        == str(expected["idempotency_key"]),
        str(existing.get("source_event_id") or "")
        == str(expected["source_event_id"]),
        str(existing.get("business_key") or "")
        == str(expected["business_key"]),
        str(existing.get("event_type") or "")
        == str(expected["event_type"]),
        str(existing.get("invite_relation_id") or "")
        == str(expected["invite_relation_id"]),
        str(existing.get("source_order_id") or "")
        == str(expected["source_order_id"]),
        str(existing.get("rule_version") or "")
        == str(expected["rule_version"]),
        hmac.compare_digest(
            str(existing.get("payload_sha256") or ""),
            str(expected["payload_sha256"]),
        ),
        (existing.get("payload") or {}) == dict(expected["payload"]),
        str(existing.get("outcome") or "") == str(expected["outcome"]),
        str(existing.get("outcome_reason") or "")
        == str(expected["outcome_reason"]),
        _timestamps_equal(existing.get("occurred_at"), expected["occurred_at"]),
    )
    if not all(checks):
        raise ProductGrowthConflict(
            "growth business event input replay changed"
        )


def _require_semantic_event_input_replay(
    existing: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> None:
    checks = (
        str(existing.get("id") or "") == str(expected["id"]),
        str(existing.get("tenant_id") or "") == str(expected["tenant_id"]),
        str(existing.get("owner_user_id") or "")
        == str(expected["owner_user_id"]),
        str(existing.get("business_key") or "")
        == str(expected["business_key"]),
        str(existing.get("event_type") or "")
        == str(expected["event_type"]),
        str(existing.get("invite_relation_id") or "")
        == str(expected["invite_relation_id"]),
        str(existing.get("source_order_id") or "")
        == str(expected["source_order_id"]),
        str(existing.get("rule_version") or "")
        == str(expected["rule_version"]),
        str(existing.get("outcome") or "") == str(expected["outcome"]),
        str(existing.get("outcome_reason") or "")
        == str(expected["outcome_reason"]),
    )
    if not all(checks):
        raise ProductGrowthConflict(
            "growth business key already has different input"
        )


def _require_exact_event_replay(
    existing: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> None:
    checks = (
        str(existing.get("id") or "") == str(expected["id"]),
        str(existing.get("tenant_id") or "") == str(expected["tenant_id"]),
        str(existing.get("owner_user_id") or "")
        == str(expected["owner_user_id"]),
        str(existing.get("idempotency_key") or "")
        == str(expected["idempotency_key"]),
        str(existing.get("source_event_id") or "")
        == str(expected["source_event_id"]),
        str(existing.get("business_key") or "")
        == str(expected["business_key"]),
        str(existing.get("event_type") or "")
        == str(expected["event_type"]),
        str(existing.get("invite_relation_id") or "")
        == str(expected["invite_relation_id"]),
        str(existing.get("source_order_id") or "")
        == str(expected["source_order_id"]),
        str(existing.get("rule_version") or "")
        == str(expected["rule_version"]),
        hmac.compare_digest(
            str(existing.get("payload_sha256") or ""),
            str(expected["payload_sha256"]),
        ),
        (existing.get("payload") or {}) == dict(expected["payload"]),
        str(existing.get("outcome") or "") == str(expected["outcome"]),
        str(existing.get("outcome_reason") or "")
        == str(expected["outcome_reason"]),
        hmac.compare_digest(
            str(existing.get("result_sha256") or ""),
            str(expected["result_sha256"]),
        ),
        (existing.get("result_payload") or {})
        == dict(expected["result_payload"]),
        _timestamps_equal(existing.get("occurred_at"), expected["occurred_at"]),
    )
    if not all(checks):
        raise ProductGrowthConflict(
            "growth business event replay content changed"
        )


def _require_semantic_event_replay(
    existing: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> None:
    checks = (
        str(existing.get("id") or "") == str(expected["id"]),
        str(existing.get("tenant_id") or "") == str(expected["tenant_id"]),
        str(existing.get("owner_user_id") or "")
        == str(expected["owner_user_id"]),
        str(existing.get("business_key") or "")
        == str(expected["business_key"]),
        str(existing.get("event_type") or "")
        == str(expected["event_type"]),
        str(existing.get("invite_relation_id") or "")
        == str(expected["invite_relation_id"]),
        str(existing.get("source_order_id") or "")
        == str(expected["source_order_id"]),
        str(existing.get("rule_version") or "")
        == str(expected["rule_version"]),
        str(existing.get("outcome") or "") == str(expected["outcome"]),
        str(existing.get("outcome_reason") or "")
        == str(expected["outcome_reason"]),
        hmac.compare_digest(
            str(existing.get("result_sha256") or ""),
            str(expected["result_sha256"]),
        ),
        (existing.get("result_payload") or {})
        == dict(expected["result_payload"]),
    )
    if not all(checks):
        raise ProductGrowthConflict(
            "growth business key already has different content"
        )


def _validate_invite_code_replay(
    existing: Mapping[str, Any],
    *,
    owner_user_id: str,
    idempotency_key: str,
    supplied_digest: str | None,
    rule_version: str,
    metadata: Mapping[str, Any],
) -> None:
    if (
        str(existing.get("owner_user_id") or "") != owner_user_id
        or str(existing.get("idempotency_key") or "") != idempotency_key
        or str(existing.get("rule_version") or "") != rule_version
        or (existing.get("metadata") or {}) != dict(metadata)
    ):
        raise ProductGrowthConflict(
            "invite code idempotency replay content changed"
        )
    if supplied_digest is not None and not hmac.compare_digest(
        str(existing.get("code_sha256") or ""),
        supplied_digest,
    ):
        raise ProductGrowthConflict(
            "invite code idempotency replay used different plaintext"
        )
    expected = {
        "id": str(existing["id"]),
        "tenant_id": str(existing["tenant_id"]),
        "owner_user_id": owner_user_id,
        "idempotency_key": idempotency_key,
        "code_sha256": str(existing["code_sha256"]),
        "rule_version": rule_version,
        "metadata": dict(metadata),
    }
    digest = _content_sha256(expected)
    if not hmac.compare_digest(
        str(existing.get("request_sha256") or ""),
        digest,
    ):
        raise ProductGrowthIntegrityError(
            "invite code request digest no longer matches its content"
        )


def _validate_invite_code_logical_replay(
    existing: Mapping[str, Any],
    *,
    supplied_digest: str | None,
    rule_version: str,
    metadata: Mapping[str, Any],
) -> None:
    if (
        str(existing.get("rule_version") or "") != rule_version
        or (existing.get("metadata") or {}) != dict(metadata)
    ):
        raise ProductGrowthConflict(
            "tenant user already has a different invite code contract"
        )
    if supplied_digest is not None and not hmac.compare_digest(
        str(existing.get("code_sha256") or ""),
        supplied_digest,
    ):
        raise ProductGrowthConflict(
            "tenant user already has a different invite code"
        )


def _validate_wallet_order_replay(
    order: Mapping[str, Any],
    *,
    owner_user_id: str,
    points: int,
    request_sha256: str,
    metadata: Mapping[str, Any],
    order_kind: str = "credit",
) -> None:
    checks = (
        str(order.get("owner_user_id") or "") == owner_user_id,
        str(order.get("order_kind") or "") == order_kind,
        int(order.get("points") or 0) == points,
        order.get("source_order_id") is None,
        order.get("source_order_kind") is None,
        order.get("job_id") is None,
        hmac.compare_digest(
            str(order.get("request_sha256") or ""),
            request_sha256,
        ),
        (order.get("metadata") or {}) == dict(metadata),
    )
    if not all(checks):
        raise ProductGrowthIntegrityError(
            "growth wallet order replay content changed"
        )


def _validate_wallet_ledger_replay(
    ledger: Mapping[str, Any],
    *,
    owner_user_id: str,
    wallet_order_id: str,
    points: int,
    request_sha256: str,
    metadata: Mapping[str, Any],
    order_kind: str = "credit",
) -> None:
    expected_delta = points if order_kind == "credit" else -points
    checks = (
        str(ledger.get("owner_user_id") or "") == owner_user_id,
        str(ledger.get("order_id") or "") == wallet_order_id,
        str(ledger.get("order_kind") or "") == order_kind,
        int(ledger.get("points") or 0) == points,
        int(ledger.get("delta_points") or 0) == expected_delta,
        int(ledger.get("balance_after_points") or 0) >= 0,
        hmac.compare_digest(
            str(ledger.get("request_sha256") or ""),
            request_sha256,
        ),
        (ledger.get("metadata") or {}) == dict(metadata),
    )
    if not all(checks):
        raise ProductGrowthIntegrityError(
            "growth wallet ledger replay content changed"
        )


def _fetchone_dict(cursor: CursorLike) -> dict[str, Any] | None:
    row = cursor.fetchone()
    if row is None:
        return None
    return _row_to_dict(cursor, row)


def _fetchall_dicts(cursor: CursorLike) -> list[dict[str, Any]]:
    return [_row_to_dict(cursor, row) for row in cursor.fetchall()]


def _row_to_dict(cursor: CursorLike, row: Any) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return dict(row)
    description = cursor.description or ()
    names = [
        str(
            getattr(
                column,
                "name",
                column[0] if isinstance(column, Sequence) and column else "",
            )
        )
        for column in description
    ]
    if not names or len(names) != len(row):
        raise ProductGrowthStoreError(
            "DB-API cursor did not expose usable row metadata"
        )
    return dict(zip(names, row))


def _decode_record(record: Mapping[str, Any]) -> dict[str, Any]:
    decoded = dict(record)
    for field in (
        "metadata",
        "risk_snapshot",
        "payload",
        "result_payload",
    ):
        value = decoded.get(field)
        if isinstance(value, str):
            try:
                decoded[field] = json.loads(value)
            except json.JSONDecodeError:
                pass
    return decoded


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise InvalidProductGrowthInput(
            "value must contain only JSON-compatible data"
        ) from exc


def _content_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(dict(value)).encode("utf-8")).hexdigest()


def _json_object(
    value: Mapping[str, Any],
    field: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise InvalidProductGrowthInput(f"{field} must be a mapping")
    try:
        decoded = json.loads(_canonical_json(dict(value)))
    except InvalidProductGrowthInput:
        raise
    if not isinstance(decoded, dict):
        raise InvalidProductGrowthInput(f"{field} must encode an object")
    return decoded


def _bounded_json_object(
    value: Mapping[str, Any],
    field: str,
) -> dict[str, Any]:
    decoded = _json_object(value, field)
    encoded = _canonical_json(decoded).encode("utf-8")
    if len(encoded) > MAX_SOURCE_PAYLOAD_BYTES:
        raise InvalidProductGrowthInput(
            f"{field} must not exceed {MAX_SOURCE_PAYLOAD_BYTES} bytes"
        )
    return decoded


def _tenant_id(value: Any) -> str:
    if not isinstance(value, str):
        raise InvalidProductGrowthInput("tenant_id must be a string")
    if value != value.strip() or not TENANT_ID_RE.fullmatch(value):
        raise InvalidProductGrowthInput("tenant_id has an invalid format")
    return value


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise InvalidProductGrowthInput(f"{field} must be a string")
    if value != value.strip() or not IDENTIFIER_RE.fullmatch(value):
        raise InvalidProductGrowthInput(f"{field} has an invalid format")
    return value


def _sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise InvalidProductGrowthInput(
            f"{field} must be a lowercase SHA-256 digest"
        )
    return value


def _current_rule_version(value: Any) -> str:
    if not isinstance(value, str) or not VERSION_RE.fullmatch(value):
        raise InvalidProductGrowthInput("rule_version has an invalid format")
    if value != growth_rules.GROWTH_RULE_VERSION:
        raise InvalidProductGrowthInput(
            "unsupported growth rule version for this store"
        )
    return value


def _agent_code(value: Any, *, agent_id: str) -> str:
    if value in (None, ""):
        return f"A{agent_id.rsplit('_', 1)[-1][:11].upper()}"
    if not isinstance(value, str):
        raise InvalidProductGrowthInput("agent_code must be a string")
    if value != value.strip() or not AGENT_CODE_RE.fullmatch(value):
        raise InvalidProductGrowthInput("agent_code has an invalid format")
    return value


def _invite_code_plaintext(value: Any) -> str:
    if not isinstance(value, str):
        raise InvalidProductGrowthInput("invite_code must be a string")
    if (
        value != value.strip()
        or not 8 <= len(value) <= 256
        or any(ord(character) < 33 or ord(character) == 127 for character in value)
    ):
        raise InvalidProductGrowthInput("invite_code has an invalid format")
    return value


def _event_type(value: Any) -> str:
    if not isinstance(value, str) or value not in SUPPORTED_EVENT_TYPES:
        raise InvalidProductGrowthInput("unsupported growth event type")
    return value


def _reward_kind(value: Any) -> str:
    allowed = {
        "registration_inviter",
        "registration_invitee",
        "first_recharge_inviter",
    }
    if not isinstance(value, str) or value not in allowed:
        raise InvalidProductGrowthInput("unsupported growth reward kind")
    return value


def _growth_id(value: Any, prefix: str, field: str) -> str:
    if not isinstance(value, str):
        raise InvalidProductGrowthInput(f"{field} must be a string")
    if not re.fullmatch(rf"{re.escape(prefix)}_[0-9a-f]{{40}}", value):
        raise InvalidProductGrowthInput(f"{field} has an invalid format")
    return value


def _id_prefix(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[a-z][a-z0-9_]{0,31}",
        value,
    ):
        raise InvalidProductGrowthInput("growth ID prefix is invalid")
    return value


def _identity_value(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidProductGrowthInput(f"{field} must be a non-empty string")
    if len(value) > 512 or any(ord(character) < 32 for character in value):
        raise InvalidProductGrowthInput(f"{field} is invalid")
    return value


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise InvalidProductGrowthInput(f"{field} must be a positive integer")
    return value


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidProductGrowthInput(
            f"{field} must be a non-negative integer"
        )
    return value


def _bounded_positive_int(
    value: Any,
    field: str,
    *,
    maximum: int,
) -> int:
    clean = _positive_int(value, field)
    if clean > maximum:
        raise InvalidProductGrowthInput(
            f"{field} must not exceed {maximum}"
        )
    return clean


def _event_timestamp(
    value: Any,
    *,
    fallback: Any = None,
) -> datetime:
    candidate = fallback if value is None else value
    if isinstance(candidate, str):
        text = candidate[:-1] + "+00:00" if candidate.endswith("Z") else candidate
        try:
            candidate = datetime.fromisoformat(text)
        except ValueError as exc:
            raise InvalidProductGrowthInput(
                "growth event timestamp is invalid"
            ) from exc
    if not isinstance(candidate, datetime) or candidate.tzinfo is None:
        raise InvalidProductGrowthInput(
            "growth event timestamp must be timezone-aware"
        )
    return candidate.astimezone(timezone.utc)


def _timestamps_equal(left: Any, right: Any) -> bool:
    try:
        return _event_timestamp(left) == _event_timestamp(right)
    except InvalidProductGrowthInput:
        return False
