from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import re
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterator, Optional, Protocol


IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$")
ASSET_REFERENCE_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:@+/-]{0,511}$"
)
ADMIN_AUDIT_STATUSES = frozenset({"succeeded", "failed", "denied"})
RISK_DECISIONS = frozenset({"allow", "deny", "review"})
RISK_LEVELS = frozenset({"info", "low", "medium", "high", "critical"})
RISK_SUBJECT_TYPES = frozenset(
    {"user", "ip", "phone", "device", "agent", "asset"}
)
IDENTIFIER_SUBJECT_TYPES = frozenset({"user", "agent", "asset"})
LIST_ORDERS = {
    "newest": "created_at DESC, event_seq DESC",
    "oldest": "created_at ASC, event_seq ASC",
}
MAX_LIST_LIMIT = 500
MAX_LIST_OFFSET = 1_000_000
MAX_METADATA_BYTES = 65_536
RISK_LIST_SORTS = {
    "id": "action_id",
    "action_id": "action_id",
    "event_type": "event_type",
    "subject_type": "subject_type",
    "subject_value": "subject_value",
    "risk_level": "risk_level",
    "decision": "decision",
    "actor_user_id": "actor_user_id",
    "created_at": "created_at",
}
ASSET_ACCESS_LIST_SORTS = {
    "id": "action_id",
    "action_id": "action_id",
    "request_id": "request_id",
    "asset_id": "asset_id",
    "asset_type": "asset_type",
    "action": "action",
    "user_id": "user_id",
    "agent_id": "agent_id",
    "ip": "ip_address",
    "ip_address": "ip_address",
    "allowed": "allowed",
    "actor_user_id": "actor_user_id",
    "created_at": "created_at",
}


class CursorLike(Protocol):
    description: Optional[Sequence[Any]]
    rowcount: int

    def execute(
        self,
        operation: str,
        parameters: Sequence[Any] = (),
    ) -> Any: ...

    def fetchone(self) -> Any: ...

    def fetchall(self) -> Sequence[Any]: ...

    def close(self) -> Any: ...


class ConnectionLike(Protocol):
    autocommit: bool

    def cursor(self) -> CursorLike: ...

    def commit(self) -> Any: ...

    def rollback(self) -> Any: ...


class ProductAdminSecurityStoreError(RuntimeError):
    pass


class InvalidProductAdminSecurityInput(
    ProductAdminSecurityStoreError,
    ValueError,
):
    pass


class ProductAdminSecurityConflict(ProductAdminSecurityStoreError):
    pass


class ProductAdminSecurityReplayConflict(ProductAdminSecurityConflict):
    pass


@dataclass(frozen=True)
class AdminSecurityCreateResult:
    record: dict[str, Any]
    created: bool


_ADMIN_AUDIT_COLUMNS = """
    event_seq, action_id, actor_user_id, action, target_type, target_id,
    status, reason, metadata, content_sha256, created_at
""".strip()

_RISK_DECISION_COLUMNS = """
    event_seq, action_id, actor_user_id, event_type, subject_type,
    subject_value, risk_level, decision, deny_reason, metadata,
    content_sha256, created_at
""".strip()

_ASSET_ACCESS_COLUMNS = """
    event_seq, action_id, actor_user_id, request_id, asset_id, asset_type,
    action, user_id, agent_id, ip_address, allowed, deny_reason,
    user_agent, metadata, content_sha256, created_at
""".strip()

_INSERT_ADMIN_AUDIT_SQL = f"""
/* product_admin_security_store:insert_admin_audit */
INSERT INTO product_admin_audit_events (
    action_id, actor_user_id, action, target_type, target_id, status,
    reason, metadata, content_sha256
) VALUES (
    %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s
)
ON CONFLICT (action_id) DO NOTHING
RETURNING {_ADMIN_AUDIT_COLUMNS}
"""

_SELECT_ADMIN_AUDIT_SQL = f"""
/* product_admin_security_store:select_admin_audit */
SELECT {_ADMIN_AUDIT_COLUMNS}
FROM product_admin_audit_events
WHERE action_id = %s
"""

_INSERT_RISK_DECISION_SQL = f"""
/* product_admin_security_store:insert_risk_decision */
INSERT INTO product_risk_decisions (
    action_id, actor_user_id, event_type, subject_type, subject_value,
    risk_level, decision, deny_reason, metadata, content_sha256
) VALUES (
    %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s
)
ON CONFLICT (action_id) DO NOTHING
RETURNING {_RISK_DECISION_COLUMNS}
"""

_SELECT_RISK_DECISION_SQL = f"""
/* product_admin_security_store:select_risk_decision */
SELECT {_RISK_DECISION_COLUMNS}
FROM product_risk_decisions
WHERE action_id = %s
"""

_INSERT_ASSET_ACCESS_SQL = f"""
/* product_admin_security_store:insert_asset_access */
INSERT INTO product_asset_access_events (
    action_id, actor_user_id, request_id, asset_id, asset_type, action,
    user_id, agent_id, ip_address, allowed, deny_reason, user_agent,
    metadata, content_sha256
) VALUES (
    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s
)
ON CONFLICT (action_id) DO NOTHING
RETURNING {_ASSET_ACCESS_COLUMNS}
"""

_SELECT_ASSET_ACCESS_SQL = f"""
/* product_admin_security_store:select_asset_access */
SELECT {_ASSET_ACCESS_COLUMNS}
FROM product_asset_access_events
WHERE action_id = %s
"""

_SUMMARIZE_ADMIN_SQL = """
/* product_admin_security_store:summarize_admin */
SELECT
    COUNT(*)::BIGINT AS total,
    COUNT(*) FILTER (WHERE status = 'succeeded')::BIGINT AS succeeded,
    COUNT(*) FILTER (WHERE status = 'failed')::BIGINT AS failed,
    COUNT(*) FILTER (WHERE status = 'denied')::BIGINT AS denied
FROM product_admin_audit_events
"""

_SUMMARIZE_RISK_SQL = """
/* product_admin_security_store:summarize_risk */
WITH latest AS (
    SELECT DISTINCT ON (subject_type, subject_value)
        subject_type, subject_value, decision
    FROM product_risk_decisions
    ORDER BY
        subject_type,
        subject_value,
        created_at DESC,
        event_seq DESC
)
SELECT
    (SELECT COUNT(*) FROM product_risk_decisions)::BIGINT AS total,
    (
        SELECT COUNT(*) FROM product_risk_decisions
        WHERE decision = 'allow'
    )::BIGINT AS allow,
    (
        SELECT COUNT(*) FROM product_risk_decisions
        WHERE decision = 'deny'
    )::BIGINT AS deny,
    (
        SELECT COUNT(*) FROM product_risk_decisions
        WHERE decision = 'review'
    )::BIGINT AS review,
    (
        SELECT COUNT(*) FROM latest
        WHERE decision = 'deny'
    )::BIGINT AS current_denied_subjects
"""

_SUMMARIZE_ACCESS_SQL = """
/* product_admin_security_store:summarize_access */
SELECT
    COUNT(*)::BIGINT AS total,
    COUNT(*) FILTER (WHERE allowed)::BIGINT AS allowed,
    COUNT(*) FILTER (WHERE NOT allowed)::BIGINT AS denied,
    COUNT(DISTINCT asset_id)::BIGINT AS distinct_assets
FROM product_asset_access_events
"""

_REGISTRATION_RISK_LATEST_SQL = f"""
/* product_admin_security_store:registration_risk_latest */
SELECT DISTINCT ON (subject_type, subject_value)
    {_RISK_DECISION_COLUMNS}
FROM product_risk_decisions
WHERE
    (subject_type = 'user' AND subject_value = %s)
    OR (subject_type = 'ip' AND subject_value = %s)
ORDER BY
    subject_type,
    subject_value,
    created_at DESC,
    event_seq DESC
"""


def record_admin_audit(
    cursor: CursorLike,
    *,
    action_id: str,
    actor_user_id: str,
    action: str,
    target_type: str,
    target_id: str,
    status: str = "succeeded",
    reason: str = "",
    metadata: Optional[Mapping[str, Any]] = None,
) -> AdminSecurityCreateResult:
    """Record an immutable admin action using an explicitly trusted actor."""

    payload = _admin_audit_payload(
        action_id=action_id,
        actor_user_id=actor_user_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        status=status,
        reason=reason,
        metadata=metadata,
    )
    content_sha256 = _content_sha256(payload)
    cursor.execute(
        _INSERT_ADMIN_AUDIT_SQL,
        (
            payload["action_id"],
            payload["actor_user_id"],
            payload["action"],
            payload["target_type"],
            payload["target_id"],
            payload["status"],
            payload["reason"],
            _canonical_json(payload["metadata"]),
            content_sha256,
        ),
    )
    return _exact_replay_result(
        cursor,
        inserted=_fetchone_dict(cursor),
        select_sql=_SELECT_ADMIN_AUDIT_SQL,
        action_id=payload["action_id"],
        content_sha256=content_sha256,
        label="admin audit",
    )


def record_risk_decision(
    cursor: CursorLike,
    *,
    action_id: str,
    actor_user_id: str,
    event_type: str,
    subject_type: str,
    subject_value: str,
    decision: str,
    risk_level: str = "info",
    deny_reason: str = "",
    metadata: Optional[Mapping[str, Any]] = None,
) -> AdminSecurityCreateResult:
    """Append one decision for exactly one normalized risk subject."""

    payload = _risk_decision_payload(
        action_id=action_id,
        actor_user_id=actor_user_id,
        event_type=event_type,
        subject_type=subject_type,
        subject_value=subject_value,
        decision=decision,
        risk_level=risk_level,
        deny_reason=deny_reason,
        metadata=metadata,
    )
    content_sha256 = _content_sha256(payload)
    cursor.execute(
        _INSERT_RISK_DECISION_SQL,
        (
            payload["action_id"],
            payload["actor_user_id"],
            payload["event_type"],
            payload["subject_type"],
            payload["subject_value"],
            payload["risk_level"],
            payload["decision"],
            payload["deny_reason"],
            _canonical_json(payload["metadata"]),
            content_sha256,
        ),
    )
    return _exact_replay_result(
        cursor,
        inserted=_fetchone_dict(cursor),
        select_sql=_SELECT_RISK_DECISION_SQL,
        action_id=payload["action_id"],
        content_sha256=content_sha256,
        label="risk decision",
    )


def record_asset_access(
    cursor: CursorLike,
    *,
    action_id: str,
    actor_user_id: str,
    request_id: str,
    asset_id: str,
    asset_type: str,
    action: str,
    allowed: bool,
    user_id: str = "",
    agent_id: str = "",
    ip: str = "",
    deny_reason: str = "",
    user_agent: str = "",
    metadata: Optional[Mapping[str, Any]] = None,
) -> AdminSecurityCreateResult:
    """Append an immutable asset-access decision with a trusted actor."""

    payload = _asset_access_payload(
        action_id=action_id,
        actor_user_id=actor_user_id,
        request_id=request_id,
        asset_id=asset_id,
        asset_type=asset_type,
        action=action,
        allowed=allowed,
        user_id=user_id,
        agent_id=agent_id,
        ip=ip,
        deny_reason=deny_reason,
        user_agent=user_agent,
        metadata=metadata,
    )
    content_sha256 = _content_sha256(payload)
    cursor.execute(
        _INSERT_ASSET_ACCESS_SQL,
        (
            payload["action_id"],
            payload["actor_user_id"],
            payload["request_id"],
            payload["asset_id"],
            payload["asset_type"],
            payload["action"],
            payload["user_id"],
            payload["agent_id"],
            payload["ip_address"],
            payload["allowed"],
            payload["deny_reason"],
            payload["user_agent"],
            _canonical_json(payload["metadata"]),
            content_sha256,
        ),
    )
    return _exact_replay_result(
        cursor,
        inserted=_fetchone_dict(cursor),
        select_sql=_SELECT_ASSET_ACCESS_SQL,
        action_id=payload["action_id"],
        content_sha256=content_sha256,
        label="asset access",
    )


def list_risk_decisions(
    cursor: CursorLike,
    *,
    subject_type: str = "",
    subject_value: str = "",
    decision: str = "",
    risk_level: str = "",
    actor_user_id: str = "",
    order: str = "newest",
    limit: int = 100,
) -> list[dict[str, Any]]:
    clean_subject_type = _optional_choice(
        subject_type,
        RISK_SUBJECT_TYPES,
        "subject_type",
    )
    clean_subject_value = ""
    if str(subject_value or "").strip():
        if not clean_subject_type:
            raise InvalidProductAdminSecurityInput(
                "subject_type is required when subject_value is provided"
            )
        clean_subject_value = _normalize_subject(
            clean_subject_type,
            subject_value,
        )
    clean_decision = _optional_choice(
        decision,
        RISK_DECISIONS,
        "decision",
    )
    clean_risk_level = _optional_choice(
        risk_level,
        RISK_LEVELS,
        "risk_level",
    )
    clean_actor = _optional_identifier(actor_user_id, "actor_user_id")
    order_clause = _order_clause(order)
    clean_limit = _bounded_limit(limit)
    sql = f"""
/* product_admin_security_store:list_risk_decisions */
SELECT {_RISK_DECISION_COLUMNS}
FROM product_risk_decisions
WHERE (%s = '' OR subject_type = %s)
  AND (%s = '' OR subject_value = %s)
  AND (%s = '' OR decision = %s)
  AND (%s = '' OR risk_level = %s)
  AND (%s = '' OR actor_user_id = %s)
ORDER BY {order_clause}
LIMIT %s
"""
    cursor.execute(
        sql,
        (
            clean_subject_type,
            clean_subject_type,
            clean_subject_value,
            clean_subject_value,
            clean_decision,
            clean_decision,
            clean_risk_level,
            clean_risk_level,
            clean_actor,
            clean_actor,
            clean_limit,
        ),
    )
    return [_decode_row(row) for row in _fetchall_dicts(cursor)]


def list_asset_access(
    cursor: CursorLike,
    *,
    asset_id: str = "",
    user_id: str = "",
    actor_user_id: str = "",
    action: str = "",
    allowed: Optional[bool] = None,
    order: str = "newest",
    limit: int = 100,
) -> list[dict[str, Any]]:
    clean_asset = (
        _asset_reference(asset_id)
        if str(asset_id or "").strip()
        else ""
    )
    clean_user = _optional_identifier(user_id, "user_id")
    clean_actor = _optional_identifier(actor_user_id, "actor_user_id")
    clean_action = _optional_identifier(action, "action")
    if allowed is not None and not isinstance(allowed, bool):
        raise InvalidProductAdminSecurityInput(
            "allowed must be a boolean or None"
        )
    allowed_filter = (
        ""
        if allowed is None
        else ("allowed" if allowed else "denied")
    )
    order_clause = _order_clause(order)
    clean_limit = _bounded_limit(limit)
    sql = f"""
/* product_admin_security_store:list_asset_access */
SELECT {_ASSET_ACCESS_COLUMNS}
FROM product_asset_access_events
WHERE (%s = '' OR asset_id = %s)
  AND (%s = '' OR user_id = %s)
  AND (%s = '' OR actor_user_id = %s)
  AND (%s = '' OR action = %s)
  AND (%s = '' OR allowed = (%s = 'allowed'))
ORDER BY {order_clause}
LIMIT %s
"""
    cursor.execute(
        sql,
        (
            clean_asset,
            clean_asset,
            clean_user,
            clean_user,
            clean_actor,
            clean_actor,
            clean_action,
            clean_action,
            allowed_filter,
            allowed_filter,
            clean_limit,
        ),
    )
    return [_decode_row(row) for row in _fetchall_dicts(cursor)]


def page_risk_decisions(
    cursor: CursorLike,
    *,
    subject_type: str = "",
    subject_value: str = "",
    decision: str = "",
    risk_level: str = "",
    actor_user_id: str = "",
    event_type: str = "",
    search: str = "",
    created_from: Any = None,
    created_to: Any = None,
    sort: str = "created_at",
    order: str = "desc",
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    clean_subject_type = _optional_choice(
        subject_type,
        RISK_SUBJECT_TYPES,
        "subject_type",
    )
    clean_subject_value = ""
    if str(subject_value or "").strip():
        if not clean_subject_type:
            raise InvalidProductAdminSecurityInput(
                "subject_type is required when subject_value is provided"
            )
        clean_subject_value = _normalize_subject(
            clean_subject_type,
            subject_value,
        )
    clean_decision = _optional_choice(
        decision,
        RISK_DECISIONS,
        "decision",
    )
    clean_risk_level = _optional_choice(
        risk_level,
        RISK_LEVELS,
        "risk_level",
    )
    clean_actor = _optional_identifier(actor_user_id, "actor_user_id")
    clean_event_type = _optional_identifier(event_type, "event_type")
    clean_search = _optional_search(search)
    clean_created_from = _optional_timestamp(created_from, "created_from")
    clean_created_to = _optional_timestamp(created_to, "created_to")
    sort_key, sort_clause, direction = _page_sort(
        sort,
        order,
        RISK_LIST_SORTS,
    )
    clean_limit = _bounded_limit(limit)
    clean_offset = _bounded_offset(offset)
    search_pattern = f"%{clean_search}%" if clean_search else ""
    params = (
        clean_subject_type,
        clean_subject_type,
        clean_subject_value,
        clean_subject_value,
        clean_decision,
        clean_decision,
        clean_risk_level,
        clean_risk_level,
        clean_actor,
        clean_actor,
        clean_event_type,
        clean_event_type,
        clean_created_from,
        clean_created_from,
        clean_created_to,
        clean_created_to,
        search_pattern,
        search_pattern,
        search_pattern,
        search_pattern,
        search_pattern,
    )
    where_sql = """
WHERE (%s = '' OR subject_type = %s)
  AND (%s = '' OR subject_value = %s)
  AND (%s = '' OR decision = %s)
  AND (%s = '' OR risk_level = %s)
  AND (%s = '' OR actor_user_id = %s)
  AND (%s = '' OR event_type = %s)
  AND (%s::timestamptz IS NULL OR created_at >= %s)
  AND (%s::timestamptz IS NULL OR created_at <= %s)
  AND (
      %s = ''
      OR action_id ILIKE %s
      OR event_type ILIKE %s
      OR subject_value ILIKE %s
      OR deny_reason ILIKE %s
  )
"""
    cursor.execute(
        f"""
/* product_admin_security_store:page_risk_decisions_count */
SELECT COUNT(*)::BIGINT AS total
FROM product_risk_decisions
{where_sql}
""",
        params,
    )
    total = _count_row(cursor, "risk decision")
    cursor.execute(
        f"""
/* product_admin_security_store:page_risk_decisions_rows */
SELECT {_RISK_DECISION_COLUMNS}
FROM product_risk_decisions
{where_sql}
ORDER BY {sort_clause} {direction}, event_seq {direction}
LIMIT %s OFFSET %s
""",
        (*params, clean_limit, clean_offset),
    )
    return {
        "items": [_decode_row(row) for row in _fetchall_dicts(cursor)],
        "total": total,
        "limit": clean_limit,
        "offset": clean_offset,
        "sort": sort_key,
        "order": direction.lower(),
    }


def page_asset_access(
    cursor: CursorLike,
    *,
    asset_id: str = "",
    asset_type: str = "",
    user_id: str = "",
    agent_id: str = "",
    actor_user_id: str = "",
    action: str = "",
    allowed: Optional[bool] = None,
    search: str = "",
    created_from: Any = None,
    created_to: Any = None,
    sort: str = "created_at",
    order: str = "desc",
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    clean_asset = (
        _asset_reference(asset_id)
        if str(asset_id or "").strip()
        else ""
    )
    clean_asset_type = _optional_identifier(asset_type, "asset_type")
    clean_user = _optional_identifier(user_id, "user_id")
    clean_agent = _optional_identifier(agent_id, "agent_id")
    clean_actor = _optional_identifier(actor_user_id, "actor_user_id")
    clean_action = _optional_identifier(action, "action")
    if allowed is not None and not isinstance(allowed, bool):
        raise InvalidProductAdminSecurityInput(
            "allowed must be a boolean or None"
        )
    allowed_filter = (
        ""
        if allowed is None
        else ("allowed" if allowed else "denied")
    )
    clean_search = _optional_search(search)
    clean_created_from = _optional_timestamp(created_from, "created_from")
    clean_created_to = _optional_timestamp(created_to, "created_to")
    sort_key, sort_clause, direction = _page_sort(
        sort,
        order,
        ASSET_ACCESS_LIST_SORTS,
    )
    clean_limit = _bounded_limit(limit)
    clean_offset = _bounded_offset(offset)
    search_pattern = f"%{clean_search}%" if clean_search else ""
    params = (
        clean_asset,
        clean_asset,
        clean_asset_type,
        clean_asset_type,
        clean_user,
        clean_user,
        clean_agent,
        clean_agent,
        clean_actor,
        clean_actor,
        clean_action,
        clean_action,
        allowed_filter,
        allowed_filter,
        clean_created_from,
        clean_created_from,
        clean_created_to,
        clean_created_to,
        search_pattern,
        search_pattern,
        search_pattern,
        search_pattern,
        search_pattern,
    )
    where_sql = """
WHERE (%s = '' OR asset_id = %s)
  AND (%s = '' OR asset_type = %s)
  AND (%s = '' OR user_id = %s)
  AND (%s = '' OR agent_id = %s)
  AND (%s = '' OR actor_user_id = %s)
  AND (%s = '' OR action = %s)
  AND (%s = '' OR allowed = (%s = 'allowed'))
  AND (%s::timestamptz IS NULL OR created_at >= %s)
  AND (%s::timestamptz IS NULL OR created_at <= %s)
  AND (
      %s = ''
      OR action_id ILIKE %s
      OR request_id ILIKE %s
      OR asset_id ILIKE %s
      OR deny_reason ILIKE %s
  )
"""
    cursor.execute(
        f"""
/* product_admin_security_store:page_asset_access_count */
SELECT COUNT(*)::BIGINT AS total
FROM product_asset_access_events
{where_sql}
""",
        params,
    )
    total = _count_row(cursor, "asset access")
    cursor.execute(
        f"""
/* product_admin_security_store:page_asset_access_rows */
SELECT {_ASSET_ACCESS_COLUMNS}
FROM product_asset_access_events
{where_sql}
ORDER BY {sort_clause} {direction}, event_seq {direction}
LIMIT %s OFFSET %s
""",
        (*params, clean_limit, clean_offset),
    )
    return {
        "items": [_decode_row(row) for row in _fetchall_dicts(cursor)],
        "total": total,
        "limit": clean_limit,
        "offset": clean_offset,
        "sort": sort_key,
        "order": direction.lower(),
    }


def summarize_admin_audits(cursor: CursorLike) -> dict[str, int]:
    cursor.execute(_SUMMARIZE_ADMIN_SQL)
    return _summary_row(
        cursor,
        ("total", "succeeded", "failed", "denied"),
    )


def summarize_risk_decisions(cursor: CursorLike) -> dict[str, int]:
    cursor.execute(_SUMMARIZE_RISK_SQL)
    return _summary_row(
        cursor,
        ("total", "allow", "deny", "review", "current_denied_subjects"),
    )


def summarize_asset_access(cursor: CursorLike) -> dict[str, int]:
    cursor.execute(_SUMMARIZE_ACCESS_SQL)
    return _summary_row(
        cursor,
        ("total", "allowed", "denied", "distinct_assets"),
    )


def registration_risk_blocked(
    cursor: CursorLike,
    *,
    user_id: str,
    ip: str,
) -> bool:
    """Return true if either subject's own latest decision is deny."""

    clean_user_id = _identifier(user_id, "user_id")
    clean_ip = _normalize_ip(ip, required=True)
    cursor.execute(
        _REGISTRATION_RISK_LATEST_SQL,
        (clean_user_id, clean_ip),
    )
    latest = [_decode_row(row) for row in _fetchall_dicts(cursor)]
    return any(row.get("decision") == "deny" for row in latest)


class ProductAdminSecurityStore:
    """PostgreSQL admin/security persistence over one injected connection."""

    def __init__(self, connection: ConnectionLike) -> None:
        if getattr(connection, "autocommit", None) is not False:
            raise InvalidProductAdminSecurityInput(
                "ProductAdminSecurityStore requires autocommit=False"
            )
        self.connection = connection

    def record_admin_audit(
        self,
        **kwargs: Any,
    ) -> AdminSecurityCreateResult:
        with self._transaction() as cursor:
            return record_admin_audit(cursor, **kwargs)

    def record_risk_decision(
        self,
        **kwargs: Any,
    ) -> AdminSecurityCreateResult:
        with self._transaction() as cursor:
            return record_risk_decision(cursor, **kwargs)

    def record_asset_access(
        self,
        **kwargs: Any,
    ) -> AdminSecurityCreateResult:
        with self._transaction() as cursor:
            return record_asset_access(cursor, **kwargs)

    def list_risk_decisions(
        self,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        with self._transaction() as cursor:
            return list_risk_decisions(cursor, **kwargs)

    def list_asset_access(
        self,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        with self._transaction() as cursor:
            return list_asset_access(cursor, **kwargs)

    def page_risk_decisions(
        self,
        **kwargs: Any,
    ) -> dict[str, Any]:
        with self._transaction() as cursor:
            return page_risk_decisions(cursor, **kwargs)

    def page_asset_access(
        self,
        **kwargs: Any,
    ) -> dict[str, Any]:
        with self._transaction() as cursor:
            return page_asset_access(cursor, **kwargs)

    def admin_summary(self) -> dict[str, int]:
        with self._transaction() as cursor:
            return summarize_admin_audits(cursor)

    def risk_summary(self) -> dict[str, int]:
        with self._transaction() as cursor:
            return summarize_risk_decisions(cursor)

    def access_summary(self) -> dict[str, int]:
        with self._transaction() as cursor:
            return summarize_asset_access(cursor)

    def registration_risk_blocked(
        self,
        *,
        user_id: str,
        ip: str,
    ) -> bool:
        with self._transaction() as cursor:
            return registration_risk_blocked(
                cursor,
                user_id=user_id,
                ip=ip,
            )

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


def _admin_audit_payload(
    *,
    action_id: str,
    actor_user_id: str,
    action: str,
    target_type: str,
    target_id: str,
    status: str,
    reason: str,
    metadata: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "action_id": _identifier(action_id, "action_id"),
        "actor_user_id": _identifier(actor_user_id, "actor_user_id"),
        "action": _identifier(action, "action"),
        "target_type": _identifier(target_type, "target_type"),
        "target_id": _identifier(target_id, "target_id"),
        "status": _choice(status, ADMIN_AUDIT_STATUSES, "status"),
        "reason": _bounded_text(reason, "reason", maximum=2048),
        "metadata": _json_object(metadata or {}, "metadata"),
    }


def _risk_decision_payload(
    *,
    action_id: str,
    actor_user_id: str,
    event_type: str,
    subject_type: str,
    subject_value: str,
    decision: str,
    risk_level: str,
    deny_reason: str,
    metadata: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    clean_subject_type = _choice(
        subject_type,
        RISK_SUBJECT_TYPES,
        "subject_type",
    )
    clean_decision = _choice(decision, RISK_DECISIONS, "decision")
    clean_deny_reason = _bounded_text(
        deny_reason,
        "deny_reason",
        maximum=2048,
    )
    if clean_decision == "deny" and not clean_deny_reason:
        raise InvalidProductAdminSecurityInput(
            "deny_reason is required for a deny decision"
        )
    return {
        "action_id": _identifier(action_id, "action_id"),
        "actor_user_id": _identifier(actor_user_id, "actor_user_id"),
        "event_type": _identifier(event_type, "event_type"),
        "subject_type": clean_subject_type,
        "subject_value": _normalize_subject(
            clean_subject_type,
            subject_value,
        ),
        "risk_level": _choice(
            risk_level,
            RISK_LEVELS,
            "risk_level",
        ),
        "decision": clean_decision,
        "deny_reason": clean_deny_reason,
        "metadata": _json_object(metadata or {}, "metadata"),
    }


def _asset_access_payload(
    *,
    action_id: str,
    actor_user_id: str,
    request_id: str,
    asset_id: str,
    asset_type: str,
    action: str,
    allowed: bool,
    user_id: str,
    agent_id: str,
    ip: str,
    deny_reason: str,
    user_agent: str,
    metadata: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    if not isinstance(allowed, bool):
        raise InvalidProductAdminSecurityInput("allowed must be a boolean")
    clean_deny_reason = _bounded_text(
        deny_reason,
        "deny_reason",
        maximum=2048,
    )
    if allowed and clean_deny_reason:
        raise InvalidProductAdminSecurityInput(
            "allowed access cannot have a deny_reason"
        )
    if not allowed and not clean_deny_reason:
        raise InvalidProductAdminSecurityInput(
            "deny_reason is required when access is denied"
        )
    return {
        "action_id": _identifier(action_id, "action_id"),
        "actor_user_id": _identifier(actor_user_id, "actor_user_id"),
        "request_id": _identifier(request_id, "request_id"),
        "asset_id": _asset_reference(asset_id),
        "asset_type": _identifier(asset_type, "asset_type"),
        "action": _identifier(action, "action"),
        "user_id": _optional_identifier(user_id, "user_id"),
        "agent_id": _optional_identifier(agent_id, "agent_id"),
        "ip_address": _normalize_ip(ip, required=False),
        "allowed": allowed,
        "deny_reason": clean_deny_reason,
        "user_agent": _bounded_text(
            user_agent,
            "user_agent",
            maximum=2048,
        ),
        "metadata": _json_object(metadata or {}, "metadata"),
    }


def _exact_replay_result(
    cursor: CursorLike,
    *,
    inserted: Optional[dict[str, Any]],
    select_sql: str,
    action_id: str,
    content_sha256: str,
    label: str,
) -> AdminSecurityCreateResult:
    if inserted is not None:
        return AdminSecurityCreateResult(
            record=_decode_row(inserted),
            created=True,
        )
    cursor.execute(select_sql, (action_id,))
    existing_row = _fetchone_dict(cursor)
    if existing_row is None:
        raise ProductAdminSecurityConflict(
            f"{label} insert conflicted without a resolvable action: "
            f"{action_id}"
        )
    existing = _decode_row(existing_row)
    actual_sha256 = str(existing.get("content_sha256") or "")
    if not hmac.compare_digest(actual_sha256, content_sha256):
        raise ProductAdminSecurityReplayConflict(
            f"{label} replay content changed: {action_id}"
        )
    return AdminSecurityCreateResult(record=existing, created=False)


def _summary_row(
    cursor: CursorLike,
    fields: Sequence[str],
) -> dict[str, int]:
    row = _fetchone_dict(cursor)
    if row is None:
        raise ProductAdminSecurityStoreError(
            "summary query returned no row"
        )
    return {field: int(row.get(field) or 0) for field in fields}


def _count_row(cursor: CursorLike, label: str) -> int:
    row = _fetchone_dict(cursor)
    if row is None:
        raise ProductAdminSecurityStoreError(
            f"{label} count query returned no row"
        )
    return int(row.get("total") or 0)


def _fetchone_dict(cursor: CursorLike) -> Optional[dict[str, Any]]:
    row = cursor.fetchone()
    if row is None:
        return None
    if isinstance(row, Mapping):
        return dict(row)
    description = cursor.description or ()
    columns = [
        str(getattr(column, "name", column[0] if column else ""))
        for column in description
    ]
    if not columns or len(columns) != len(row):
        raise ProductAdminSecurityStoreError(
            "DB-API cursor did not expose usable row metadata"
        )
    return dict(zip(columns, row))


def _fetchall_dicts(cursor: CursorLike) -> list[dict[str, Any]]:
    rows = cursor.fetchall()
    if not rows:
        return []
    if isinstance(rows[0], Mapping):
        return [dict(row) for row in rows]
    description = cursor.description or ()
    columns = [
        str(getattr(column, "name", column[0] if column else ""))
        for column in description
    ]
    if not columns:
        raise ProductAdminSecurityStoreError(
            "DB-API cursor did not expose usable row metadata"
        )
    return [dict(zip(columns, row)) for row in rows]


def _decode_row(row: Mapping[str, Any]) -> dict[str, Any]:
    decoded = dict(row)
    metadata = decoded.get("metadata")
    if isinstance(metadata, str):
        try:
            decoded["metadata"] = json.loads(metadata)
        except json.JSONDecodeError as exc:
            raise ProductAdminSecurityStoreError(
                "database returned invalid JSON metadata"
            ) from exc
    ip_value = decoded.get("ip_address")
    if ip_value is not None and not isinstance(ip_value, str):
        decoded["ip_address"] = str(ip_value)
    return decoded


def _content_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        _canonical_json(value).encode("utf-8")
    ).hexdigest()


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _json_object(
    value: Mapping[str, Any],
    field: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise InvalidProductAdminSecurityInput(
            f"{field} must be an object"
        )
    try:
        canonical = _canonical_json(value)
        decoded = json.loads(canonical)
    except (TypeError, ValueError) as exc:
        raise InvalidProductAdminSecurityInput(
            f"{field} must be JSON serializable"
        ) from exc
    if len(canonical.encode("utf-8")) > MAX_METADATA_BYTES:
        raise InvalidProductAdminSecurityInput(
            f"{field} exceeds {MAX_METADATA_BYTES} bytes"
        )
    return decoded


def _identifier(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not IDENTIFIER_RE.fullmatch(text):
        raise InvalidProductAdminSecurityInput(
            f"invalid {field}: {value}"
        )
    return text


def _optional_identifier(value: Any, field: str) -> str:
    text = str(value or "").strip()
    return "" if not text else _identifier(text, field)


def _asset_reference(value: Any) -> str:
    text = str(value or "").strip()
    if (
        not ASSET_REFERENCE_RE.fullmatch(text)
        or "\\" in text
        or "://" in text
        or "//" in text
        or any(part in {".", ".."} for part in text.split("/"))
        or any(ord(character) < 32 or ord(character) == 127 for character in text)
    ):
        raise InvalidProductAdminSecurityInput(
            f"invalid asset_id: {value}"
        )
    return text


def _choice(value: Any, allowed: frozenset[str], field: str) -> str:
    clean = str(value or "").strip().lower()
    if clean not in allowed:
        raise InvalidProductAdminSecurityInput(
            f"invalid {field}: {value}"
        )
    return clean


def _optional_choice(
    value: Any,
    allowed: frozenset[str],
    field: str,
) -> str:
    clean = str(value or "").strip().lower()
    return "" if not clean else _choice(clean, allowed, field)


def _bounded_text(
    value: Any,
    field: str,
    *,
    maximum: int,
) -> str:
    text = str(value or "").strip()
    if len(text) > maximum:
        raise InvalidProductAdminSecurityInput(
            f"{field} exceeds {maximum} characters"
        )
    return text


def _normalize_ip(value: Any, *, required: bool) -> str:
    text = str(value or "").strip()
    if not text:
        if required:
            raise InvalidProductAdminSecurityInput("ip is required")
        return ""
    try:
        return ipaddress.ip_address(text).compressed.lower()
    except ValueError as exc:
        raise InvalidProductAdminSecurityInput(
            f"invalid ip: {value}"
        ) from exc


def _normalize_subject(subject_type: str, value: Any) -> str:
    if subject_type == "ip":
        return _normalize_ip(value, required=True)
    if subject_type in IDENTIFIER_SUBJECT_TYPES:
        return _identifier(value, "subject_value")
    text = _bounded_text(
        value,
        "subject_value",
        maximum=512,
    )
    if not text:
        raise InvalidProductAdminSecurityInput(
            "subject_value is required"
        )
    return text


def _order_clause(value: Any) -> str:
    clean = str(value or "").strip().lower()
    try:
        return LIST_ORDERS[clean]
    except KeyError as exc:
        raise InvalidProductAdminSecurityInput(
            f"invalid order: {value}"
        ) from exc


def _bounded_limit(value: Any) -> int:
    if isinstance(value, bool):
        raise InvalidProductAdminSecurityInput(
            "limit must be an integer"
        )
    try:
        clean = int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidProductAdminSecurityInput(
            "limit must be an integer"
        ) from exc
    if clean < 1 or clean > MAX_LIST_LIMIT:
        raise InvalidProductAdminSecurityInput(
            f"limit must be between 1 and {MAX_LIST_LIMIT}"
        )
    return clean


def _bounded_offset(value: Any) -> int:
    if isinstance(value, bool):
        raise InvalidProductAdminSecurityInput(
            "offset must be an integer"
        )
    try:
        clean = int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidProductAdminSecurityInput(
            "offset must be an integer"
        ) from exc
    if clean < 0 or clean > MAX_LIST_OFFSET:
        raise InvalidProductAdminSecurityInput(
            f"offset must be between 0 and {MAX_LIST_OFFSET}"
        )
    return clean


def _optional_search(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) > 256:
        raise InvalidProductAdminSecurityInput(
            "search exceeds 256 characters"
        )
    return text


def _optional_timestamp(value: Any, field: str) -> Optional[datetime]:
    if value is None or str(value).strip() == "":
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidProductAdminSecurityInput(
            f"invalid {field}: {value}"
        ) from exc


def _page_sort(
    sort: Any,
    order: Any,
    allowed: Mapping[str, str],
) -> tuple[str, str, str]:
    sort_key = _snake_case(str(sort or "created_at").strip())
    try:
        expression = allowed[sort_key]
    except KeyError as exc:
        raise InvalidProductAdminSecurityInput(
            f"invalid sort: {sort}"
        ) from exc
    direction = str(order or "desc").strip().lower()
    if direction not in {"asc", "desc"}:
        raise InvalidProductAdminSecurityInput(
            f"invalid order: {order}"
        )
    return sort_key, expression, direction.upper()


def _snake_case(value: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", value).lower()
