"""Daemon-owned Telethon adapter for precise outbox read dates."""

from __future__ import annotations

from datetime import UTC, datetime
from importlib import import_module
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
from .telegram_rpc_scheduler import RpcAdmissionClosedError, TelegramRpcSource, rpc_scope


class _TelegramClientLike(Protocol):
    async def get_input_entity(self, entity: object) -> object: ...

    async def __call__(self, request: object) -> object: ...


def _optional_error(name: str) -> type[BaseException] | None:
    candidate = getattr(import_module("telethon.errors"), name, None)
    return candidate if isinstance(candidate, type) and issubclass(candidate, BaseException) else None


def _is_optional_error(exc: BaseException, name: str) -> bool:
    error_type = _optional_error(name)
    return error_type is not None and isinstance(exc, error_type)


def _read_failure(
    exc: BaseException,
    *,
    reason: ReadDateReason,
    retryable: bool,
    retry_after: int | None = None,
) -> ReadDateFetchResult:
    failure = GatewayFailure(
        kind=GatewayFailureKind.TRANSIENT,
        error_type=type(exc).__name__,
        error_message=str(exc).replace("\n", "\\n") or type(exc).__name__,
        retryable=retryable,
        retry_after=retry_after,
    )
    return ReadDateFetchResult(status="unavailable", failure=failure, reason=reason)


def classify_read_date_exception(exc: BaseException) -> ReadDateFetchResult:  # noqa: PLR0911
    """Classify read-date-only Telegram failures without changing shared translation."""
    if _is_optional_error(exc, "MessageNotReadYetError"):
        return ReadDateFetchResult(status="missing", reason=ReadDateReason.MESSAGE_NOT_READ_YET)
    if _is_optional_error(exc, "MsgTooOldError"):
        return _read_failure(exc, reason=ReadDateReason.MESSAGE_TOO_OLD, retryable=False)
    if _is_optional_error(exc, "UserPrivacyRestrictedError") or _is_optional_error(exc, "YourPrivacyRestrictedError"):
        return _read_failure(exc, reason=ReadDateReason.PRIVACY_RESTRICTED, retryable=False)
    if _is_optional_error(exc, "UserNotMutualContactError"):
        return _read_failure(exc, reason=ReadDateReason.NOT_MUTUAL_CONTACT, retryable=False)
    if _is_optional_error(exc, "PeerIdInvalidError"):
        return _read_failure(exc, reason=ReadDateReason.INVALID_TARGET, retryable=False)
    if isinstance(exc, TelegramRpcThrottled):
        failure = translate_gateway_failure(exc)
        return ReadDateFetchResult(
            status="unavailable",
            failure=failure,
            reason=ReadDateReason.FLOOD_WAIT,
        )

    failure = translate_gateway_failure(exc)
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
                peer = await self._client.get_input_entity(entity) if isinstance(entity, int) else entity
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
