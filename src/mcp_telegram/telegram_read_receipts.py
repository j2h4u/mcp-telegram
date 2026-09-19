"""Daemon-owned Telethon adapter for precise outbox read dates."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Protocol, cast

from telethon.tl.functions.messages import GetOutboxReadDateRequest
from telethon.tl.types import TypeInputPeer

from .flood import TelegramRpcThrottled
from .telegram_demand import AcquisitionKind, RpcAttemptBudgetExhaustedError
from .telegram_gateway import CATCHABLE_GATEWAY_FAILURES, translate_gateway_failure
from .telegram_reading import (
    GatewayFailure,
    GatewayFailureKind,
    ReadDateFetchResult,
    ReadDateReason,
    TelegramReadReceiptGateway,
)
from .telegram_rpc_error import describe_telegram_rpc_error
from .telegram_rpc_scheduler import RpcAdmissionClosedError, TelegramRpcSource, rpc_scope


class _TelegramClientLike(Protocol):
    async def get_input_entity(self, entity: object) -> object: ...

    async def __call__(self, request: object) -> object: ...


def _read_failure(
    exc: BaseException,
    *,
    reason: ReadDateReason,
    retryable: bool,
    retry_after: int | None = None,
    kind: GatewayFailureKind = GatewayFailureKind.TRANSIENT,
) -> ReadDateFetchResult:
    failure = GatewayFailure(
        kind=kind,
        error_type=type(exc).__name__,
        error_message=str(exc).replace("\n", "\\n") or type(exc).__name__,
        retryable=retryable,
        retry_after=retry_after,
    )
    return ReadDateFetchResult(status="unavailable", failure=failure, reason=reason)


def classify_read_date_exception(exc: BaseException) -> ReadDateFetchResult:  # noqa: PLR0911
    """Classify read-date-only Telegram failures without changing shared translation."""
    if isinstance(exc, TelegramRpcThrottled):
        failure = replace(translate_gateway_failure(exc), retryable=True)
        return ReadDateFetchResult(
            status="unavailable",
            failure=failure,
            reason=ReadDateReason.FLOOD_WAIT,
        )

    symbol = describe_telegram_rpc_error(exc).symbol
    if symbol == "MESSAGE_NOT_READ_YET":
        return ReadDateFetchResult(status="missing", reason=ReadDateReason.MESSAGE_NOT_READ_YET)
    if symbol == "MSG_TOO_OLD":
        return _read_failure(exc, reason=ReadDateReason.MESSAGE_TOO_OLD, retryable=False)
    if symbol in {"USER_PRIVACY_RESTRICTED", "YOUR_PRIVACY_RESTRICTED"}:
        return _read_failure(exc, reason=ReadDateReason.PRIVACY_RESTRICTED, retryable=False)
    if symbol == "USER_NOT_MUTUAL_CONTACT":
        return _read_failure(exc, reason=ReadDateReason.NOT_MUTUAL_CONTACT, retryable=False)
    if symbol in {"PEER_ID_INVALID", "MESSAGE_ID_INVALID", "MSG_ID_INVALID"}:
        return _read_failure(
            exc,
            reason=ReadDateReason.INVALID_TARGET,
            retryable=False,
            kind=GatewayFailureKind.INVALID_TARGET,
        )

    failure = translate_gateway_failure(exc)
    if failure.kind is GatewayFailureKind.INVALID_TARGET:
        failure = replace(failure, kind=GatewayFailureKind.TRANSIENT, retryable=True)
        return ReadDateFetchResult(status="unavailable", failure=failure, reason=ReadDateReason.TRANSIENT)
    reason = {
        GatewayFailureKind.INVALID_TARGET: ReadDateReason.INVALID_TARGET,
        GatewayFailureKind.ACCESS_LOST: ReadDateReason.ACCESS_LOST,
        GatewayFailureKind.FLOOD_WAIT: ReadDateReason.FLOOD_WAIT,
        GatewayFailureKind.TRANSIENT: ReadDateReason.TRANSIENT,
    }[failure.kind]
    return ReadDateFetchResult(status="unavailable", failure=failure, reason=reason)


class TelethonTelegramReadReceiptGateway:
    """Fetch exact read dates; the background fact probe owns the RPC scope."""

    def __init__(self, client: object) -> None:
        self._client = cast(_TelegramClientLike, client)

    async def fetch_outbox_read_date(self, entity: object, message_id: int) -> ReadDateFetchResult:
        with rpc_scope(
            TelegramRpcSource.READ_RECEIPT_PROBE,
            acquisition_kind=AcquisitionKind.READ_RECEIPT_SNAPSHOT,
        ):
            try:
                try:
                    peer = await self._client.get_input_entity(entity) if isinstance(entity, int) else entity
                except (KeyError, ValueError) as exc:
                    return _read_failure(exc, reason=ReadDateReason.TRANSIENT, retryable=True)
                response = await self._client(
                    GetOutboxReadDateRequest(peer=cast(TypeInputPeer, peer), msg_id=message_id)
                )
                value = getattr(response, "date", None)
                if not isinstance(value, datetime):
                    # Telegram can return an empty/permission-limited response. It
                    # is a successful probe with no event timestamp, not an error.
                    return ReadDateFetchResult(status="missing", reason=ReadDateReason.DATE_OMITTED)
                if value.tzinfo is None:
                    value = value.replace(tzinfo=UTC)
                return ReadDateFetchResult(
                    read_at=int(value.timestamp()),
                    status="complete",
                    reason=ReadDateReason.RESOLVED,
                )
            except RpcAdmissionClosedError, RpcAttemptBudgetExhaustedError:
                raise
            except CATCHABLE_GATEWAY_FAILURES as exc:
                return classify_read_date_exception(exc)


__all__ = [
    "TelegramReadReceiptGateway",
    "TelethonTelegramReadReceiptGateway",
    "classify_read_date_exception",
]
