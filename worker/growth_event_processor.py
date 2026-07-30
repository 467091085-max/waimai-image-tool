from __future__ import annotations

import logging
from typing import Any, Callable, Mapping, Optional, Protocol

from shared import product_growth_outbox


LOGGER = logging.getLogger("waimai.growth-event-processor")
MAX_BATCH_LIMIT = 100


class ConnectionLike(Protocol):
    def cursor(self) -> Any: ...

    def commit(self) -> Any: ...

    def rollback(self) -> Any: ...


GrowthEventHandler = Callable[
    [Any, Mapping[str, Any]],
    Optional[Mapping[str, Any]],
]


class GrowthEventProcessorError(RuntimeError):
    pass


class PermanentGrowthEventError(GrowthEventProcessorError):
    pass


class InvalidGrowthEventClaim(PermanentGrowthEventError, ValueError):
    pass


def process_claimed_event(
    connection: ConnectionLike,
    claim: Mapping[str, Any],
    *,
    handler: GrowthEventHandler,
) -> dict[str, Any]:
    """Commit a business effect and fenced outbox success atomically."""

    event = _claimed_event(claim)
    cursor = connection.cursor()
    try:
        result_payload = handler(cursor, event)
        if result_payload is None:
            result_payload = {}
        if not isinstance(result_payload, Mapping):
            raise PermanentGrowthEventError(
                "growth event handler must return an object"
            )
        succeeded = product_growth_outbox.mark_growth_event_succeeded(
            cursor,
            event_id=event["id"],
            claim_token=event["claim_token"],
            fence=event["fence"],
            result_payload=dict(result_payload),
        )
        connection.commit()
        return {
            "eventId": event["id"],
            "eventType": event["event_type"],
            "status": "succeeded",
            "idempotent": bool(succeeded.idempotent),
            "result": dict(result_payload),
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        close = getattr(cursor, "close", None)
        if callable(close):
            close()


def process_growth_events_once(
    outbox: Any,
    connection: ConnectionLike,
    *,
    handler: GrowthEventHandler,
    worker_id: str,
    limit: int = 10,
    lease_seconds: int = 60,
    retry_in_seconds: int = 30,
) -> dict[str, Any]:
    worker = _required_text(worker_id, "worker_id")
    batch_limit = _bounded_positive_int(
        limit,
        "limit",
        maximum=MAX_BATCH_LIMIT,
    )
    lease = _positive_int(lease_seconds, "lease_seconds")
    retry_delay = _nonnegative_int(
        retry_in_seconds,
        "retry_in_seconds",
    )
    claims = outbox.claim(
        worker_id=worker,
        limit=batch_limit,
        lease_seconds=lease,
    )
    if not isinstance(claims, list):
        raise GrowthEventProcessorError("growth outbox claim must return a list")

    report: dict[str, Any] = {
        "claimed": len(claims),
        "succeeded": 0,
        "retried": 0,
        "deadLettered": 0,
        "claimLost": 0,
        "failed": 0,
        "results": [],
        "failures": [],
    }
    for raw_claim in claims:
        event_id = (
            str(raw_claim.get("id") or "")
            if isinstance(raw_claim, Mapping)
            else ""
        )
        try:
            result = process_claimed_event(
                connection,
                raw_claim,
                handler=handler,
            )
            report["succeeded"] += 1
            report["results"].append(result)
        except product_growth_outbox.GrowthOutboxClaimLost as exc:
            report["claimLost"] += 1
            report["failures"].append(
                _failure(event_id, exc, "claim_lost")
            )
        except PermanentGrowthEventError as exc:
            try:
                claim = _claimed_event(raw_claim)
                outbox.mark_dead_letter(
                    event_id=claim["id"],
                    claim_token=claim["claim_token"],
                    fence=claim["fence"],
                    error=_safe_error(exc),
                )
                report["deadLettered"] += 1
            except product_growth_outbox.GrowthOutboxClaimLost as claim_error:
                report["claimLost"] += 1
                report["failures"].append(
                    _failure(event_id, claim_error, "claim_lost")
                )
            except Exception as terminal_error:  # noqa: BLE001
                report["failed"] += 1
                report["failures"].append(
                    _failure(event_id, terminal_error, "dead_letter_failed")
                )
                LOGGER.exception(
                    "growth event dead-letter transition failed",
                    extra={
                        "event_id": event_id,
                        "error_type": type(terminal_error).__name__,
                    },
                )
        except Exception as exc:  # noqa: BLE001 - DB failures vary by driver
            try:
                claim = _claimed_event(raw_claim)
                mutation = outbox.mark_retry(
                    event_id=claim["id"],
                    claim_token=claim["claim_token"],
                    fence=claim["fence"],
                    error=_safe_error(exc),
                    retry_in_seconds=retry_delay,
                )
                if str(mutation.event.get("status") or "") == "dead_letter":
                    report["deadLettered"] += 1
                else:
                    report["retried"] += 1
            except product_growth_outbox.GrowthOutboxClaimLost as claim_error:
                report["claimLost"] += 1
                report["failures"].append(
                    _failure(event_id, claim_error, "claim_lost")
                )
            except Exception as retry_error:  # noqa: BLE001
                report["failed"] += 1
                report["failures"].append(
                    _failure(event_id, retry_error, "retry_failed")
                )
                LOGGER.exception(
                    "growth event retry transition failed",
                    extra={
                        "event_id": event_id,
                        "error_type": type(retry_error).__name__,
                    },
                )
    return report


def _claimed_event(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise InvalidGrowthEventClaim("growth outbox claim must be an object")
    payload = value.get("payload")
    if not isinstance(payload, Mapping):
        raise InvalidGrowthEventClaim(
            "growth outbox claim payload must be an object"
        )
    status = _required_text(value.get("status"), "status")
    if status != "claimed":
        raise InvalidGrowthEventClaim(
            f"growth outbox event is not claimed: {status}"
        )
    event_type = _required_text(value.get("event_type"), "event_type")
    if event_type not in product_growth_outbox.SUPPORTED_EVENT_TYPES:
        raise InvalidGrowthEventClaim(
            f"unsupported growth event type: {event_type}"
        )
    return {
        **dict(value),
        "id": _required_text(value.get("id"), "event_id"),
        "event_type": event_type,
        "claim_token": _required_text(
            value.get("claim_token"),
            "claim_token",
        ),
        "fence": _positive_int(value.get("fence"), "fence"),
        "payload": dict(payload),
    }


def _failure(
    event_id: str,
    exc: BaseException,
    status: str,
) -> dict[str, str]:
    return {
        "eventId": event_id,
        "status": status,
        "errorType": type(exc).__name__,
    }


def _safe_error(exc: BaseException) -> str:
    message = " ".join(str(exc or "").split())
    if not message:
        message = type(exc).__name__
    return message[:4000]


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise InvalidGrowthEventClaim(f"{field} is required")
    return text


def _positive_int(value: Any, field: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidGrowthEventClaim(f"{field} must be an integer") from exc
    if isinstance(value, bool) or number <= 0:
        raise InvalidGrowthEventClaim(f"{field} must be positive")
    return number


def _nonnegative_int(value: Any, field: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidGrowthEventClaim(f"{field} must be an integer") from exc
    if isinstance(value, bool) or number < 0:
        raise InvalidGrowthEventClaim(f"{field} must be nonnegative")
    return number


def _bounded_positive_int(
    value: Any,
    field: str,
    *,
    maximum: int,
) -> int:
    number = _positive_int(value, field)
    if number > maximum:
        raise InvalidGrowthEventClaim(
            f"{field} must not exceed {maximum}"
        )
    return number
