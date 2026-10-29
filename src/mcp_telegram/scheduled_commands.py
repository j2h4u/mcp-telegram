"""Finite, idempotent native scheduled-message changes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import sqlite3
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol, cast
from weakref import WeakValueDictionary

from telethon.errors import RPCError  # type: ignore[import-untyped]
from telethon.tl import types  # type: ignore[import-untyped]
from telethon.tl.functions.messages import (  # type: ignore[import-untyped]
    DeleteScheduledMessagesRequest,
    EditMessageRequest,
    SendMessageRequest,
)
from telethon.utils import get_peer_id  # type: ignore[import-untyped]

from .message_composition import decode_formatting_entities
from .observation_order import delivered_observation_order
from .scheduled_contracts import SCHEDULED_OPERATION_KEY_LIMIT, scheduled_message_version
from .scheduled_messages import mark_scheduled_messages_removed, upsert_scheduled_message
from .scheduled_receipts_schema import SCHEDULED_OPERATION_RECEIPT_DDL as _SCHEDULED_OPERATION_RECEIPT_DDL
from .scheduled_write_fence import (
    ScheduledMutationCheck,
    ScheduledMutationFence,
    ScheduledMutationIntent,
    ScheduledMutationToken,
    ScheduledWriteRejected,
    scheduled_admission_deadline,
    scheduled_mutation_context,
)
from .sqlite_writer import SQLiteWriter
from .sync_transactions import require_write_transaction, write_transaction
from .telegram_demand import RpcAttemptBudget, demand_context
from .telegram_rpc_consumers import DemandKind
from .telegram_rpc_error import describe_telegram_rpc_error
from .telegram_rpc_scheduler import rpc_attempt_budget
from .temporal import parse_utc_boundary

logger = logging.getLogger(__name__)

_ACTIONS = frozenset({"create", "update", "cancel"})
_ALLOWED_ARGUMENTS = frozenset(
    {
        "action",
        "dialog_id",
        "account_id",
        "topic_id",
        "operation_key",
        "scheduled_message_id",
        "expected_version",
        "text",
        "schedule_at_utc",
        "repeat_period_seconds",
        "drop_formatting",
    }
)
SCHEDULED_REPEAT_PERIODS = frozenset({86_400, 604_800, 1_209_600, 2_592_000, 7_862_400, 15_724_800, 31_536_000})
_MAX_TEXT_LENGTH = 65_536
_MAX_VERSION_LENGTH = 256
_MAX_RESULT_BYTES = 16_384
_TELEGRAM_CLIENT_ERROR_MIN = 400
_TELEGRAM_CLIENT_ERROR_MAX = 500


class ScheduledCommandError(ValueError):
    """Safe refusal before a scheduled-message mutation is dispatched."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class ScheduledOperationConflictError(ValueError):
    """An operation key is already bound to a different normalized intent."""


class ScheduledVersionConflictError(ValueError):
    """The selected scheduled row changed after the caller read it."""


@dataclass(frozen=True, slots=True)
class ScheduledCommand:
    action: Literal["create", "update", "cancel"]
    account_id: int
    dialog_id: int
    topic_id: int | None
    operation_key: str
    scheduled_message_id: int | None
    expected_version: str | None
    text: str | None
    schedule_at_utc: int | None
    repeat_period_seconds: int | None
    repeat_was_supplied: bool
    drop_formatting: bool

    def fingerprint(self) -> str:
        payload = {
            "action": self.action,
            "account_id": self.account_id,
            "dialog_id": self.dialog_id,
            "topic_id": self.topic_id,
            "operation_key": self.operation_key,
            "scheduled_message_id": self.scheduled_message_id,
            "expected_version": self.expected_version,
            "text": self.text,
            "schedule_at_utc": self.schedule_at_utc,
            "repeat_period_seconds": self.repeat_period_seconds,
            "repeat_was_supplied": self.repeat_was_supplied,
            "drop_formatting": self.drop_formatting,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ScheduledOperationReceipt:
    account_id: int
    operation_key: str
    request_fingerprint: str
    action: str
    dialog_id: int
    scheduled_message_id: int | None
    random_id: int | None
    expected_version: str | None
    outcome: str
    mutation_dispatched: bool | None
    result: dict[str, object]
    updated_at: int


class _ScheduledClient(Protocol):
    async def __call__(self, request: object) -> object: ...


def ensure_scheduled_receipt_schema(conn: sqlite3.Connection) -> None:
    """Create the isolated receipt table for focused tests and migration integration."""
    conn.execute(_SCHEDULED_OPERATION_RECEIPT_DDL)


def _receipt(row: sqlite3.Row | tuple[object, ...] | None) -> ScheduledOperationReceipt | None:
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        value = dict(row)
    else:
        names = (
            "account_id",
            "operation_key",
            "request_fingerprint",
            "action",
            "dialog_id",
            "scheduled_message_id",
            "random_id",
            "expected_version",
            "outcome",
            "mutation_dispatched",
            "result_json",
            "updated_at",
        )
        value = dict(zip(names, row, strict=True))
    result = cast(object, json.loads(cast(str, value["result_json"])))
    if not isinstance(result, dict):
        raise ValueError("scheduled receipt result is malformed")
    return ScheduledOperationReceipt(
        int(cast(int, value["account_id"])),
        cast(str, value["operation_key"]),
        cast(str, value["request_fingerprint"]),
        cast(str, value["action"]),
        int(cast(int, value["dialog_id"])),
        None if value["scheduled_message_id"] is None else int(cast(int, value["scheduled_message_id"])),
        None if value["random_id"] is None else int(cast(int, value["random_id"])),
        cast(str | None, value["expected_version"]),
        cast(str, value["outcome"]),
        None if value["mutation_dispatched"] is None else bool(value["mutation_dispatched"]),
        cast(dict[str, object], result),
        int(cast(int, value["updated_at"])),
    )


def read_scheduled_operation(
    conn: sqlite3.Connection, account_id: int, operation_key: str
) -> ScheduledOperationReceipt | None:
    row = cast(
        sqlite3.Row | tuple[object, ...] | None,
        conn.execute(
            "SELECT account_id,operation_key,request_fingerprint,action,dialog_id,scheduled_message_id,"
            "random_id,expected_version,outcome,mutation_dispatched,result_json,updated_at "
            "FROM scheduled_operation_receipts WHERE account_id=? AND operation_key=?",
            (account_id, operation_key),
        ).fetchone(),
    )
    return _receipt(row)


def prepare_scheduled_operation(
    conn: sqlite3.Connection, command: ScheduledCommand, random_id: int | None, *, now: int | None = None
) -> ScheduledOperationReceipt:
    require_write_transaction(conn)
    timestamp = int(time.time()) if now is None else now
    ensure_scheduled_receipt_schema(conn)
    existing = read_scheduled_operation(conn, command.account_id, command.operation_key)
    if existing is not None:
        if existing.request_fingerprint != command.fingerprint():
            raise ScheduledOperationConflictError("operation key is bound to another intent")
        return existing
    if command.action == "create" and (type(random_id) is not int or random_id == 0):
        raise ValueError("create receipt requires a stable nonzero random ID")
    if command.action != "create" and random_id is not None:
        raise ValueError("only create receipts have a random ID")
    conn.execute(
        "INSERT INTO scheduled_operation_receipts (account_id,operation_key,request_fingerprint,action,dialog_id,"
        "scheduled_message_id,random_id,expected_version,outcome,mutation_dispatched,result_json,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?, 'pending',NULL,'{}',?,?)",
        (
            command.account_id,
            command.operation_key,
            command.fingerprint(),
            command.action,
            command.dialog_id,
            command.scheduled_message_id,
            random_id,
            command.expected_version,
            timestamp,
            timestamp,
        ),
    )
    receipt = read_scheduled_operation(conn, command.account_id, command.operation_key)
    assert receipt is not None
    return receipt


def finish_scheduled_operation(  # noqa: PLR0913 - one receipt transition needs its exact result and identity.
    conn: sqlite3.Connection,
    account_id: int,
    operation_key: str,
    *,
    outcome: Literal["acknowledged", "unchanged", "unknown", "rejected"],
    mutation_dispatched: bool | None,
    result: Mapping[str, object],
    scheduled_message_id: int | None = None,
    now: int | None = None,
) -> ScheduledOperationReceipt:
    require_write_transaction(conn)
    result_json = json.dumps(dict(result), sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    if len(result_json.encode()) > _MAX_RESULT_BYTES:
        raise ValueError("scheduled operation result exceeds its bound")
    timestamp = int(time.time()) if now is None else now
    conn.execute(
        "UPDATE scheduled_operation_receipts SET outcome=?,mutation_dispatched=?,result_json=?,"
        "scheduled_message_id=COALESCE(?,scheduled_message_id),updated_at=? "
        "WHERE account_id=? AND operation_key=? AND outcome='pending'",
        (
            outcome,
            None if mutation_dispatched is None else int(mutation_dispatched),
            result_json,
            scheduled_message_id,
            timestamp,
            account_id,
            operation_key,
        ),
    )
    receipt = read_scheduled_operation(conn, account_id, operation_key)
    if receipt is None:
        raise LookupError("scheduled operation receipt disappeared")
    return receipt


def settle_interrupted_scheduled_operations(
    conn: sqlite3.Connection, account_id: int, *, now: int | None = None
) -> int:
    """Make prior-process pending operations unknown; they are never replayed."""
    require_write_transaction(conn)
    timestamp = int(time.time()) if now is None else now
    cursor = conn.execute(
        "UPDATE scheduled_operation_receipts SET outcome='unknown',mutation_dispatched=NULL,"
        "result_json='{}',updated_at=? WHERE account_id=? AND outcome='pending'",
        (timestamp, account_id),
    )
    return cursor.rowcount


def _parse_scope(request: Mapping[str, object], account_id: int) -> tuple[int, int | None]:
    selected_account = request.get("account_id", account_id)
    if type(selected_account) is not int or selected_account != account_id:
        raise ScheduledCommandError("account_mismatch", "The selected Telegram account does not match this session.")
    dialog_id = request.get("dialog_id")
    if type(dialog_id) is not int or dialog_id == 0:
        raise ScheduledCommandError("invalid_arguments", "A concrete nonzero dialog_id is required.")
    topic_id = request.get("topic_id")
    if topic_id is not None and (type(topic_id) is not int or topic_id <= 0):
        raise ScheduledCommandError("invalid_arguments", "topic_id must be a positive integer.")
    return dialog_id, cast(int | None, topic_id)


def _parse_operation_key(request: Mapping[str, object]) -> str:
    operation_key = request.get("operation_key")
    if (
        not isinstance(operation_key, str)
        or not operation_key.strip()
        or len(operation_key) > SCHEDULED_OPERATION_KEY_LIMIT
        or operation_key != operation_key.strip()
    ):
        raise ScheduledCommandError(
            "invalid_arguments", "operation_key must contain 1 to 128 non-space-padded characters."
        )
    return operation_key


def _parse_item_version(request: Mapping[str, object]) -> tuple[int | None, str | None]:
    message_id = request.get("scheduled_message_id")
    if message_id is not None and (type(message_id) is not int or message_id <= 0):
        raise ScheduledCommandError("invalid_arguments", "scheduled_message_id must be a positive queue ID.")
    expected = request.get("expected_version")
    if expected is not None and (not isinstance(expected, str) or not expected or len(expected) > _MAX_VERSION_LENGTH):
        raise ScheduledCommandError(
            "invalid_arguments", "expected_version must be copied from the scheduled queue read."
        )
    return cast(int | None, message_id), cast(str | None, expected)


def _parse_text(request: Mapping[str, object]) -> str | None:
    text = request.get("text")
    if text is not None and (not isinstance(text, str) or not text.strip() or len(text) > _MAX_TEXT_LENGTH):
        raise ScheduledCommandError("invalid_arguments", "text must be non-empty bounded text.")
    if isinstance(text, str):
        try:
            text.encode("utf-8")
        except UnicodeEncodeError:
            raise ScheduledCommandError("invalid_arguments", "text must contain valid Unicode.") from None
    return cast(str | None, text)


def _parse_schedule(request: Mapping[str, object]) -> int | None:
    schedule_raw = request.get("schedule_at_utc")
    schedule_at: int | None
    if schedule_raw is None:
        schedule_at = None
    elif isinstance(schedule_raw, str):
        try:
            schedule_at = parse_utc_boundary(schedule_raw, field="schedule_at_utc")
        except ValueError as error:
            raise ScheduledCommandError("invalid_arguments", str(error)) from None
    else:
        raise ScheduledCommandError("invalid_arguments", "schedule_at_utc must be an RFC3339 UTC string.")
    return schedule_at


def _parse_repeat(request: Mapping[str, object]) -> int | None:
    repeat_was_supplied = "repeat_period_seconds" in request
    repeat = request.get("repeat_period_seconds")
    if repeat_was_supplied and type(repeat) is not int:
        raise ScheduledCommandError("invalid_arguments", "repeat_period_seconds must be an exact integer.")
    if repeat is not None and repeat != 0 and repeat not in SCHEDULED_REPEAT_PERIODS:
        raise ScheduledCommandError("unsupported_repeat_period", "Choose a supported native repeat period in seconds.")
    return cast(int | None, repeat)


def _validate_create(command: ScheduledCommand) -> None:
    if (
        command.scheduled_message_id is not None
        or command.expected_version is not None
        or not isinstance(command.text, str)
        or command.schedule_at_utc is None
    ):
        raise ScheduledCommandError(
            "invalid_arguments", "Create requires text and schedule_at_utc, without a queue ID or version."
        )
    if command.drop_formatting:
        raise ScheduledCommandError("invalid_arguments", "drop_formatting is only used when updating formatted text.")


def _validate_update(command: ScheduledCommand) -> None:
    if command.scheduled_message_id is None or command.expected_version is None:
        raise ScheduledCommandError("version_required", "Update requires scheduled_message_id and expected_version.")
    if command.text is None and command.schedule_at_utc is None and not command.repeat_was_supplied:
        raise ScheduledCommandError("invalid_arguments", "Update requires at least one field change.")


def _validate_cancel(command: ScheduledCommand) -> None:
    if any(
        (
            command.scheduled_message_id is None,
            command.expected_version is None,
            command.text is not None,
            command.schedule_at_utc is not None,
            command.repeat_was_supplied,
            command.drop_formatting,
        )
    ):
        raise ScheduledCommandError(
            "invalid_arguments", "Cancel requires only the selected queue ID, version, and operation key."
        )


def _parse_command(request: Mapping[str, object], account_id: int) -> ScheduledCommand:
    if set(request) - _ALLOWED_ARGUMENTS:
        raise ScheduledCommandError("invalid_arguments", "The scheduled command contains unsupported fields.")
    action = request.get("action")
    if not isinstance(action, str) or action not in _ACTIONS:
        raise ScheduledCommandError("invalid_arguments", "Choose create, update, or cancel.")
    dialog_id, topic_id = _parse_scope(request, account_id)
    operation_key = _parse_operation_key(request)
    message_id, expected = _parse_item_version(request)
    text = _parse_text(request)
    schedule_at = _parse_schedule(request)
    repeat = _parse_repeat(request)
    drop_formatting = request.get("drop_formatting", False)
    if type(drop_formatting) is not bool:
        raise ScheduledCommandError("invalid_arguments", "drop_formatting must be a boolean.")
    command = ScheduledCommand(
        cast(Literal["create", "update", "cancel"], action),
        account_id,
        dialog_id,
        topic_id,
        operation_key,
        message_id,
        expected,
        text,
        schedule_at,
        repeat,
        "repeat_period_seconds" in request,
        drop_formatting,
    )
    if action == "create":
        _validate_create(command)
    elif action == "update":
        _validate_update(command)
    else:
        _validate_cancel(command)
    return command


def _current_state(
    conn: sqlite3.Connection, account_id: int, dialog_id: int, message_id: int | None
) -> tuple[Mapping[str, object] | None, int]:
    account = cast(
        tuple[object, ...] | None,
        conn.execute("SELECT account_id FROM scheduled_sync_state WHERE key='account'").fetchone(),
    )
    if account is None or account[0] != account_id:
        raise ScheduledCommandError("account_state_unknown", "Scheduled queue ownership is not bound to this account.")
    coverage = cast(
        tuple[object, ...] | None,
        conn.execute(
            "SELECT COALESCE(snapshot_observation_order,0),coverage_state FROM scheduled_reconciliation_state WHERE dialog_id=?",
            (dialog_id,),
        ).fetchone(),
    )
    if coverage is None or coverage[1] != "complete" or cast(int, coverage[0]) <= 0:
        raise ScheduledCommandError(
            "scheduled_state_unknown", "This dialog's scheduled queue has no current complete baseline."
        )
    row: Mapping[str, object] | None = None
    if message_id is not None:
        row = _read_mapping(
            conn,
            "SELECT * FROM scheduled_messages WHERE dialog_id=? AND message_id=?",
            (dialog_id, message_id),
        )
        if row is None or row["message_state"] != "scheduled" or row["observation_order"] is None:
            raise ScheduledCommandError(
                "scheduled_message_unavailable", "That scheduled queue item is no longer active locally."
            )
    return row, cast(int, coverage[0])


def _read_mapping(conn: sqlite3.Connection, sql: str, values: tuple[object, ...]) -> dict[str, object] | None:
    cursor = conn.execute(sql, values)
    row = cast(tuple[object, ...] | None, cursor.fetchone())
    if row is None:
        return None
    assert cursor.description is not None
    columns = cast(tuple[tuple[object, ...], ...], cursor.description)
    return dict(zip((cast(str, column[0]) for column in columns), row, strict=True))


def _peer_matches(peer: object, dialog_id: int, account_id: int) -> bool:
    if isinstance(peer, types.InputPeerSelf):
        return dialog_id == account_id
    if isinstance(peer, (types.InputPeerUser, types.InputPeerChat, types.InputPeerChannel)):
        return get_peer_id(peer) == dialog_id
    return False


def _has_stored_formatting(payload: object) -> bool:
    if not isinstance(payload, str):
        raise ScheduledCommandError(
            "formatting_unknown", "Formatting details are unavailable; cannot safely replace this text."
        )
    try:
        values = decode_formatting_entities(payload)
        if values is None:
            raise ValueError("unknown")
        return bool(values)
    except Exception as error:
        raise ScheduledCommandError(
            "formatting_unknown", "Formatting details are unavailable; cannot safely replace this text."
        ) from error


def _validate_caption_media(media_kind: object, payload: object) -> None:
    if media_kind not in {"animation", "audio", "document", "photo", "video", "voice"}:
        raise ScheduledCommandError(
            "unsupported_composition", "This media type cannot be safely preserved by a caption edit."
        )
    if not isinstance(payload, str):
        raise ScheduledCommandError(
            "unsupported_composition", "Stored media composition is incomplete; refresh before replacing text."
        )
    try:
        decoded_media = cast(object, json.loads(payload))
    except json.JSONDecodeError, TypeError:
        decoded_media = None
    if not isinstance(decoded_media, dict):
        raise ScheduledCommandError(
            "unsupported_composition", "Stored media composition is incomplete; refresh before replacing text."
        )
    if media_kind == "video" and type(decoded_media.get("round_message")) is not bool:
        raise ScheduledCommandError(
            "unsupported_composition", "Video composition is incomplete; refresh before replacing its caption."
        )
    if media_kind == "video" and decoded_media["round_message"]:
        raise ScheduledCommandError("unsupported_composition", "Video notes do not support caption replacement.")


def _caption_placement(row: Mapping[str, object]) -> bool:
    invert_media = row.get("invert_media")
    if not (type(invert_media) is int and invert_media in {0, 1}) and type(invert_media) is not bool:
        raise ScheduledCommandError(
            "unsupported_composition", "Media display options are unavailable; refresh before replacing text."
        )
    return bool(invert_media)


def _text_replacement_flags(row: Mapping[str, object]) -> tuple[bool | None, bool | None]:
    rich = row.get("has_rich_message")
    if not (rich is False or (type(rich) is int and rich == 0)):
        raise ScheduledCommandError(
            "unsupported_composition", "Rich or incomplete message composition cannot be safely replaced."
        )
    media_kind = row.get("media_kind")
    media_payload = row.get("media_payload")
    if media_kind == "link_preview":
        raise ScheduledCommandError(
            "unsupported_composition", "This link preview has options that cannot be preserved by a text edit."
        )
    if media_kind is None:
        if media_payload is not None:
            raise ScheduledCommandError(
                "unsupported_composition", "Stored media composition is incomplete; refresh before replacing text."
            )
        return None, True
    _validate_caption_media(media_kind, media_payload)
    return _caption_placement(row), None


def _build_create_request(
    command: ScheduledCommand, peer: object, random_id: int | None, premium: bool | None
) -> object:
    schedule_at = command.schedule_at_utc
    repeat = command.repeat_period_seconds
    assert schedule_at is not None and command.text is not None and random_id is not None
    if command.topic_id is not None and not isinstance(peer, types.InputPeerChannel):
        raise ScheduledCommandError(
            "topic_scope_unsupported", "Exact topic scheduling requires a retained forum channel."
        )
    if repeat is not None and repeat > 0 and premium is not True:
        raise ScheduledCommandError(
            "premium_required", "A Premium account is required for repeating scheduled messages."
        )
    return SendMessageRequest(
        peer=cast(types.TypeInputPeer, peer),
        message=command.text,
        random_id=random_id,
        reply_to=(
            types.InputReplyToMessage(reply_to_msg_id=command.topic_id, top_msg_id=command.topic_id)
            if command.topic_id is not None
            else None
        ),
        entities=[],
        schedule_date=datetime.fromtimestamp(schedule_at, UTC),
        schedule_repeat_period=repeat if repeat and repeat > 0 else None,
    )


def _update_repeat(command: ScheduledCommand, row: Mapping[str, object], premium: bool | None) -> tuple[int, int]:
    raw_repeat = row["schedule_repeat_period"]
    if raw_repeat is not None and type(raw_repeat) is not int:
        raise ScheduledCommandError("scheduled_repeat_unknown", "The current repeat setting is unavailable.")
    current_repeat = raw_repeat if type(raw_repeat) is int else 0
    effective_repeat = cast(int, command.repeat_period_seconds) if command.repeat_was_supplied else current_repeat
    if effective_repeat not in SCHEDULED_REPEAT_PERIODS | {0}:
        raise ScheduledCommandError(
            "unsupported_repeat_period", "The current repeat setting is unsupported for safe editing."
        )
    if effective_repeat and row["grouped_id"] is not None:
        raise ScheduledCommandError("repeat_album_unsupported", "Repeating scheduled albums are not supported.")
    if effective_repeat > 0 and premium is not True:
        raise ScheduledCommandError(
            "premium_required", "A Premium account is required to preserve or set a repeating schedule."
        )
    return current_repeat, effective_repeat


def _update_text_fields(
    command: ScheduledCommand, row: Mapping[str, object]
) -> tuple[str | None, list[types.TypeMessageEntity] | None, bool | None, bool | None]:
    if command.text is None:
        return None, None, None, None
    if command.text == row["text"] and not command.drop_formatting:
        return None, None, None, None
    if command.text != row["text"] and bool(row["is_service"]):
        raise ScheduledCommandError("unsupported_composition", "Service messages cannot be edited through this tool.")
    invert_media, no_webpage = _text_replacement_flags(row)
    if not command.drop_formatting and _has_stored_formatting(row["formatting_entities"]):
        raise ScheduledCommandError(
            "formatting_conflict",
            "Changing this text would invalidate its formatting; set drop_formatting=true.",
        )
    return command.text, [], invert_media, no_webpage


def _build_update_request(
    command: ScheduledCommand, row: Mapping[str, object], peer: object, premium: bool | None
) -> object:
    schedule_at = command.schedule_at_utc
    if row.get("video_processing_pending") == 1:
        raise ScheduledCommandError(
            "video_processing_pending", "Telegram is still processing this scheduled video; wait before editing it."
        )
    current_date = cast(int | None, row["scheduled_at"])
    effective_date = schedule_at if schedule_at is not None else current_date
    if effective_date is None:
        raise ScheduledCommandError(
            "scheduled_time_unknown", "The current scheduled time is unknown; refresh the queue before editing."
        )
    current_repeat, effective_repeat = _update_repeat(command, row, premium)
    message, entities, invert_media, no_webpage = _update_text_fields(command, row)
    if schedule_at == current_date:
        schedule_at = None
    if effective_repeat == current_repeat and schedule_at is None and message is None:
        return None
    return EditMessageRequest(
        peer=cast(types.TypeInputPeer, peer),
        id=cast(int, command.scheduled_message_id),
        message=message,
        entities=entities,
        no_webpage=no_webpage,
        invert_media=invert_media,
        schedule_date=datetime.fromtimestamp(int(effective_date), UTC),
        schedule_repeat_period=effective_repeat if effective_repeat > 0 else None,
    )


def _build_request(
    command: ScheduledCommand,
    row: Mapping[str, object] | None,
    peer: object,
    *,
    random_id: int | None,
    premium: bool | None,
) -> object:
    if command.action == "create":
        return _build_create_request(command, peer, random_id, premium)
    assert row is not None and command.scheduled_message_id is not None
    if command.action == "cancel":
        return DeleteScheduledMessagesRequest(cast(types.TypeInputPeer, peer), [command.scheduled_message_id])
    return _build_update_request(command, row, peer, premium)


def _updates(response: object) -> Sequence[object]:
    if isinstance(response, types.UpdateShort):
        return (response.update,)
    values = getattr(response, "updates", None)
    return values if isinstance(values, (list, tuple)) else ()


def _created_queue_id(updates: Sequence[object], random_id: int | None) -> int | None:
    mapped_id = next(
        (
            getattr(update, "id", None)
            for update in updates
            if isinstance(update, types.UpdateMessageID) and getattr(update, "random_id", None) == random_id
        ),
        None,
    )
    return mapped_id if type(mapped_id) is int else None


def _scheduled_response_message(updates: Sequence[object], dialog_id: int, queue_id: int | None) -> object | None:
    return next(
        (
            update.message
            for update in updates
            if isinstance(update, types.UpdateNewScheduledMessage)
            and getattr(getattr(update, "message", None), "id", None) == queue_id
            and getattr(update.message, "peer_id", None) is not None
            and _same_peer(update.message.peer_id, dialog_id)
        ),
        None,
    )


def _valid_sent_message_vector(queue_ids: object, sent_ids: object) -> bool:
    if sent_ids is None:
        return True
    if not isinstance(queue_ids, (list, tuple)) or not isinstance(sent_ids, (list, tuple)):
        return False
    if len(sent_ids) != len(queue_ids):
        return False
    return all(type(value) is int and value > 0 for value in sent_ids)


def _cancel_response(updates: Sequence[object], command: ScheduledCommand) -> object | None:
    match = next(
        (
            update
            for update in updates
            if isinstance(update, types.UpdateDeleteScheduledMessages)
            and _same_peer(getattr(update, "peer", None), command.dialog_id)
            and command.scheduled_message_id in (getattr(update, "messages", ()) or ())
        ),
        None,
    )
    if match is not None and not _valid_sent_message_vector(
        getattr(match, "messages", None), getattr(match, "sent_messages", None)
    ):
        return None
    return match


def _correlated_response(
    command: ScheduledCommand, response: object, *, random_id: int | None
) -> tuple[str, int | None, object | None]:
    updates = _updates(response)
    if command.action == "create":
        mapped_id = _created_queue_id(updates, random_id)
        if mapped_id is None:
            return "acknowledged", None, None
        match = _scheduled_response_message(updates, command.dialog_id, mapped_id)
        return "acknowledged", mapped_id if match is not None else None, match
    if command.action == "update":
        match = _scheduled_response_message(updates, command.dialog_id, command.scheduled_message_id)
        return "acknowledged", command.scheduled_message_id, match
    return "acknowledged", command.scheduled_message_id, _cancel_response(updates, command)


def _same_peer(peer: object | None, dialog_id: int) -> bool:
    try:
        return peer is not None and int(cast(int, get_peer_id(peer))) == dialog_id
    except TypeError, ValueError:
        return False


def _validate_current_item(command: ScheduledCommand, row: Mapping[str, object] | None) -> None:
    if command.action == "create":
        return
    assert row is not None and command.expected_version is not None
    actual = scheduled_message_version(
        command.account_id, command.dialog_id, command.scheduled_message_id or 0, cast(int, row["observation_order"])
    )
    if command.expected_version != actual:
        raise ScheduledVersionConflictError("scheduled row version changed")
    if command.topic_id is not None and row["forum_topic_id"] != command.topic_id:
        raise ScheduledCommandError("topic_mismatch", "The scheduled item does not belong to the selected topic.")


def _validated_schedule_time(command: ScheduledCommand, row: Mapping[str, object] | None) -> int | None:
    schedule_time = command.schedule_at_utc
    if schedule_time is None and command.action == "update" and row is not None:
        schedule_time = cast(int | None, row["scheduled_at"])
    if command.action != "cancel" and (schedule_time is None or schedule_time < int(time.time()) + 60):
        raise ScheduledCommandError(
            "schedule_too_soon", "The scheduled time must remain at least 60 seconds in the future at dispatch."
        )
    return schedule_time


def _mutation_intent(
    command: ScheduledCommand,
    row: Mapping[str, object] | None,
    coverage_order: int,
    random_id: int | None,
    schedule_time: int | None,
) -> ScheduledMutationIntent:
    return ScheduledMutationIntent(
        account_id=command.account_id,
        dialog_id=command.dialog_id,
        action=command.action,
        operation_key=command.operation_key,
        request_fingerprint=command.fingerprint(),
        expected_observation_order=cast(int, row["observation_order"]) if row is not None else coverage_order,
        scheduled_message_id=command.scheduled_message_id,
        schedule_at_utc=None if command.action == "cancel" else float(schedule_time or 0),
        random_id=random_id,
    )


def _response_row_confirmed(
    conn: sqlite3.Connection, dialog_id: int, queue_id: int | None, state: str, observation_order: int
) -> bool:
    row = _read_mapping(
        conn,
        "SELECT message_state,observation_order FROM scheduled_messages WHERE dialog_id=? AND message_id=?",
        (dialog_id, queue_id),
    )
    return row is not None and row["message_state"] == state and row["observation_order"] == observation_order


class ScheduledCommandOwner:
    """Own one account-bound scheduled mutation and its durable receipt."""

    def __init__(  # noqa: PLR0913 - explicit daemon-owned dependencies keep command ownership narrow.
        self,
        client: object,
        reader_conn: sqlite3.Connection,
        *,
        writer: SQLiteWriter,
        account_id: int,
        fence: ScheduledMutationFence,
        resolve_peer: Callable[[int, int | None], Awaitable[object | None]],
        account_is_premium: Callable[[], bool | None],
        request_recovery: Callable[[int, str], Awaitable[None]],
    ) -> None:
        if type(account_id) is not int or account_id <= 0:
            raise ValueError("account_id must be positive")
        self._client = cast(_ScheduledClient, client)
        self._reader = reader_conn
        self._writer = writer
        self._account_id = account_id
        self._fence = fence
        self._resolve_peer = resolve_peer
        self._account_is_premium = account_is_premium
        self._request_recovery = request_recovery
        self._locks: WeakValueDictionary[tuple[int, int], asyncio.Lock] = WeakValueDictionary()

    async def execute(self, request: Mapping[str, object]) -> dict[str, object]:
        try:
            command = _parse_command(request, self._account_id)
        except ScheduledCommandError as error:
            return self._error(error)
        lock = self._locks.setdefault((command.account_id, command.dialog_id), asyncio.Lock())
        async with lock:
            try:
                return await self._execute_command(command)
            except ScheduledVersionConflictError:
                return self._error(
                    ScheduledCommandError("version_conflict", "The scheduled item changed; read its current version.")
                )
            except ScheduledOperationConflictError:
                return self._error(
                    ScheduledCommandError("operation_conflict", "operation_key is already bound to a different intent.")
                )
            except ScheduledCommandError as error:
                return self._error(error)
            except Exception as error:  # noqa: BLE001 - do not expose peer, database, or Telegram payloads.
                logger.warning("scheduled_command_preflight_failed error_type=%s", type(error).__name__)
                return self._error(
                    ScheduledCommandError(
                        "preflight_unavailable",
                        "Scheduled queue verification is unavailable; no mutation was dispatched.",
                    )
                )

    async def _execute_command(self, command: ScheduledCommand) -> dict[str, object]:
        duplicate = read_scheduled_operation(self._reader, command.account_id, command.operation_key)
        if duplicate is not None:
            return self._duplicate(command, duplicate)
        peer = await self._resolve_peer(command.dialog_id, command.topic_id)
        if not _peer_matches(peer, command.dialog_id, command.account_id):
            raise ScheduledCommandError(
                "peer_unavailable", "Exact retained peer access is unavailable for this dialog."
            )
        row, coverage_order = await self._writer.run(
            lambda conn: _current_state(conn, command.account_id, command.dialog_id, command.scheduled_message_id)
        )
        _validate_current_item(command, row)
        return await self._prepare_dispatch(command, row, coverage_order, peer)

    async def _prepare_dispatch(
        self, command: ScheduledCommand, row: Mapping[str, object] | None, coverage_order: int, peer: object
    ) -> dict[str, object]:
        schedule_time = _validated_schedule_time(command, row)
        random_id = secrets.randbits(63) or 1 if command.action == "create" else None
        request_object = _build_request(command, row, peer, random_id=random_id, premium=self._account_is_premium())
        if request_object is None:
            result = self._result(command, "unchanged", False, acknowledged=None, row_confirmed=False)
            await self._persist_receipt(command, random_id, "unchanged", False, result)
            return result
        receipt, response_order, created = await self._prepare_receipt(command, random_id)
        if not created or receipt.outcome != "pending":
            return self._duplicate(command, receipt)
        if command.action == "create" and receipt.random_id != random_id:
            request_object = _build_request(
                command, row, peer, random_id=receipt.random_id, premium=self._account_is_premium()
            )
            if request_object is None:
                raise RuntimeError("created scheduled request unexpectedly disappeared")
        intent = _mutation_intent(command, row, coverage_order, receipt.random_id, schedule_time)
        return await self._dispatch(command, request_object, intent, response_order)

    def _revalidation_check(self, command: ScheduledCommand, intent: ScheduledMutationIntent) -> ScheduledMutationCheck:
        async def revalidate() -> ScheduledMutationToken:
            def validate(conn: sqlite3.Connection) -> ScheduledMutationToken:
                with write_transaction(conn):
                    live_receipt = read_scheduled_operation(conn, command.account_id, command.operation_key)
                    if (
                        live_receipt is None
                        or live_receipt.outcome != "pending"
                        or live_receipt.request_fingerprint != command.fingerprint()
                    ):
                        raise ScheduledWriteRejected("scheduled_receipt_changed")
                    try:
                        live_row, live_coverage = _current_state(
                            conn, command.account_id, command.dialog_id, command.scheduled_message_id
                        )
                    except ScheduledCommandError as error:
                        raise ScheduledWriteRejected(error.code) from error
                    live_order = cast(int, live_row["observation_order"]) if live_row is not None else live_coverage
                    if live_order != intent.expected_observation_order or (
                        live_row is not None and live_row["message_state"] != "scheduled"
                    ):
                        raise ScheduledWriteRejected("scheduled_version_changed")
                    return self._fence.watch(command.account_id, command.dialog_id, command.scheduled_message_id)

            return await self._writer.run(validate)

        return ScheduledMutationCheck(intent, revalidate)

    async def _dispatch(
        self, command: ScheduledCommand, request_object: object, intent: ScheduledMutationIntent, response_order: int
    ) -> dict[str, object]:
        check = self._revalidation_check(command, intent)
        budget = RpcAttemptBudget(1)
        try:
            with demand_context(
                DemandKind.SCHEDULED_MUTATION,
                deadline=scheduled_admission_deadline(intent.schedule_at_utc),
            ):
                with rpc_attempt_budget(budget):
                    with scheduled_mutation_context(check):
                        response = await self._client(request_object)
        except BaseException as error:
            result = await self._dispatch_error(command, error, bool(budget.attempts))
            if not isinstance(error, Exception):
                raise
            return result
        return await self._dispatch_success(command, response, intent.random_id, response_order, budget)

    async def _dispatch_error(
        self, command: ScheduledCommand, error: BaseException, dispatched: bool
    ) -> dict[str, object]:
        outcome: Literal["unknown", "rejected"] = (
            "unknown" if dispatched and not _definite_rejection(error) else "rejected"
        )
        if isinstance(error, ScheduledWriteRejected):
            dispatched = False
            outcome = "rejected"
        result = self._result(
            command,
            outcome,
            dispatched,
            acknowledged=False if not dispatched else None,
            row_confirmed=False,
        )
        if outcome == "unknown":
            result.update(
                error="mutation_outcome_unknown",
                message="Telegram may have applied this scheduled change, but no acknowledgment was received.",
                required_action="Read the local scheduled queue; do not retry this operation key.",
            )
        else:
            result.update(
                error=(error.code if isinstance(error, ScheduledWriteRejected) else "mutation_rejected"),
                message="The scheduled change was rejected before confirmation.",
                required_action="Read the local scheduled queue before starting a new operation.",
            )
        await self._finish_receipt(command, outcome, dispatched, result)
        if dispatched:
            await self._offer_recovery(command.dialog_id, "scheduled_mutation_unknown")
        return result

    async def _dispatch_success(
        self,
        command: ScheduledCommand,
        response: object,
        random_id: int | None,
        response_order: int,
        budget: RpcAttemptBudget,
    ) -> dict[str, object]:
        status, resulting_id, correlated = _correlated_response(command, response, random_id=random_id)
        confirmed = False
        if correlated is not None:
            try:
                confirmed = await self._apply_response(command, correlated, observation_order=response_order)
            except Exception as error:  # noqa: BLE001 - preserve known remote outcome if local projection fails.
                logger.warning("scheduled_response_projection_failed error_type=%s", type(error).__name__)
        dispatched = bool(budget.attempts) or response is not None
        result = self._result(
            command,
            status,
            dispatched,
            acknowledged=True,
            row_confirmed=confirmed,
            scheduled_message_id=resulting_id,
        )
        await self._finish_receipt(command, "acknowledged", dispatched, result, resulting_id)
        if not confirmed:
            await self._offer_recovery(command.dialog_id, "scheduled_mutation_unconfirmed")
        return result

    async def _offer_recovery(self, dialog_id: int, reason: str) -> None:
        try:
            await self._request_recovery(dialog_id, reason)
        except Exception as error:  # noqa: BLE001 - recovery scheduling cannot change the command outcome.
            logger.warning("scheduled_recovery_offer_failed error_type=%s", type(error).__name__)

    def _duplicate(self, command: ScheduledCommand, receipt: ScheduledOperationReceipt) -> dict[str, object]:
        if receipt.request_fingerprint != command.fingerprint():
            raise ScheduledOperationConflictError("operation key has a different intent")
        if receipt.result:
            return dict(receipt.result)
        return self._result(command, "unknown", None, acknowledged=None, row_confirmed=False)

    async def _prepare_receipt(
        self, command: ScheduledCommand, random_id: int | None
    ) -> tuple[ScheduledOperationReceipt, int, bool]:
        def prepare(conn: sqlite3.Connection) -> tuple[ScheduledOperationReceipt, int, bool]:
            with write_transaction(conn):
                existing = read_scheduled_operation(conn, command.account_id, command.operation_key)
                if existing is not None:
                    if existing.request_fingerprint != command.fingerprint():
                        raise ScheduledOperationConflictError("operation key has another intent")
                    return existing, 0, False
                receipt = prepare_scheduled_operation(conn, command, random_id)
                # Reserve before any RPC wait so a newer delivered event always wins.
                response_order = delivered_observation_order(conn)
                return receipt, response_order, True

        return await self._writer.run(prepare)

    async def _persist_receipt(
        self,
        command: ScheduledCommand,
        random_id: int | None,
        outcome: str,
        dispatched: bool,
        result: dict[str, object],
    ) -> None:
        await self._prepare_receipt(command, random_id)
        await self._finish_receipt(
            command, cast(Literal["acknowledged", "unchanged", "unknown", "rejected"], outcome), dispatched, result
        )

    async def _finish_receipt(
        self,
        command: ScheduledCommand,
        outcome: Literal["acknowledged", "unchanged", "unknown", "rejected"],
        dispatched: bool | None,
        result: dict[str, object],
        scheduled_message_id: int | None = None,
    ) -> bool:
        def finish(conn: sqlite3.Connection) -> None:
            with write_transaction(conn):
                finish_scheduled_operation(
                    conn,
                    command.account_id,
                    command.operation_key,
                    outcome=outcome,
                    mutation_dispatched=dispatched,
                    result=result,
                    scheduled_message_id=scheduled_message_id,
                )

        try:
            await self._writer.run(finish)
        except Exception as error:  # noqa: BLE001 - keep remote outcome separate from receipt durability.
            logger.warning("scheduled_receipt_finish_failed error_type=%s", type(error).__name__)
            result["receipt_persisted"] = False
            return False
        result["receipt_persisted"] = True
        return True

    async def _apply_response(self, command: ScheduledCommand, evidence: object, *, observation_order: int) -> bool:
        def persist(conn: sqlite3.Connection) -> bool:
            with write_transaction(conn):
                if command.action == "cancel":
                    return self._apply_cancellation(conn, command, evidence, observation_order)
                return self._apply_message(conn, command, evidence, observation_order)

        return await self._writer.run(persist)

    def _apply_cancellation(
        self, conn: sqlite3.Connection, command: ScheduledCommand, evidence: object, observation_order: int
    ) -> bool:
        update = evidence
        ids = cast(Sequence[int], getattr(update, "messages", ()))
        if command.scheduled_message_id not in ids:
            return False
        index = ids.index(command.scheduled_message_id)
        sent_ids = cast(Sequence[int] | None, getattr(update, "sent_messages", None))
        selected_sent_ids = [sent_ids[index]] if sent_ids is not None and index < len(sent_ids) else None
        mark_scheduled_messages_removed(
            conn,
            command.dialog_id,
            [command.scheduled_message_id or 0],
            selected_sent_ids,
            now=int(time.time()),
            observation_order=observation_order,
            mutation_fence=self._fence,
            account_id=command.account_id,
        )
        return _response_row_confirmed(
            conn, command.dialog_id, command.scheduled_message_id, "cancelled", observation_order
        )

    def _apply_message(
        self, conn: sqlite3.Connection, command: ScheduledCommand, evidence: object, observation_order: int
    ) -> bool:
        message = cast(types.Message, evidence)
        queue_id = message.id
        if command.action == "update" and queue_id != command.scheduled_message_id:
            return False
        upsert_scheduled_message(
            conn,
            command.dialog_id,
            message,
            now=int(time.time()),
            observation_order=observation_order,
            mutation_fence=self._fence,
            account_id=command.account_id,
        )
        return _response_row_confirmed(conn, command.dialog_id, queue_id, "scheduled", observation_order)

    @staticmethod
    def _result(  # noqa: PLR0913 - result projection takes the receipt and operation-confirmation fields.
        command: ScheduledCommand,
        outcome: str,
        dispatched: bool | None,
        *,
        acknowledged: bool | None,
        row_confirmed: bool,
        scheduled_message_id: int | None = None,
    ) -> dict[str, object]:
        return {
            "ok": outcome in {"acknowledged", "unchanged", "unknown"},
            "action": command.action,
            "operation_key": command.operation_key,
            "account_id": command.account_id,
            "dialog_id": command.dialog_id,
            "scheduled_message_id": scheduled_message_id or command.scheduled_message_id,
            "outcome": outcome,
            "mutation_dispatched": dispatched,
            "acknowledged": acknowledged,
            "receipt_persisted": True,
            "current_is_operation_confirmation": row_confirmed,
            "schedule_at_utc": (
                datetime.fromtimestamp(command.schedule_at_utc, UTC).isoformat().replace("+00:00", "Z")
                if command.schedule_at_utc is not None
                else None
            ),
        }

    @staticmethod
    def _error(error: ScheduledCommandError) -> dict[str, object]:
        return {"ok": False, "error": error.code, "message": error.message, "required_action": error.message}


def _definite_rejection(error: BaseException) -> bool:
    descriptor = describe_telegram_rpc_error(error)
    return (
        isinstance(error, RPCError)
        and descriptor.code is not None
        and _TELEGRAM_CLIENT_ERROR_MIN <= descriptor.code < _TELEGRAM_CLIENT_ERROR_MAX
    )


__all__ = [
    "SCHEDULED_REPEAT_PERIODS",
    "ScheduledCommand",
    "ScheduledCommandError",
    "ScheduledCommandOwner",
    "ScheduledOperationConflictError",
    "ScheduledOperationReceipt",
    "ScheduledVersionConflictError",
    "ensure_scheduled_receipt_schema",
    "finish_scheduled_operation",
    "prepare_scheduled_operation",
    "read_scheduled_operation",
    "settle_interrupted_scheduled_operations",
]
