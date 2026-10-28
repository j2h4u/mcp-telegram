"""Finite, version-checked native draft changes through the shared Telegram gate."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Literal, Protocol, cast
from weakref import WeakValueDictionary

from telethon.errors import RPCError  # type: ignore[import-untyped]
from telethon.tl import types  # type: ignore[import-untyped]
from telethon.tl.functions.messages import SaveDraftRequest  # type: ignore[import-untyped]
from telethon.utils import get_peer_id  # type: ignore[import-untyped]

from mcp_telegram.drafts.contracts import MAX_DRAFT_TEXT_LENGTH, DraftScope
from mcp_telegram.drafts.history import (
    DraftOperationConflictError,
    DraftOperationReceipt,
    DraftRestoreUnavailableError,
    DraftState,
    DraftVersionConflictError,
    finish_draft_operation,
    get_draft_history,
    prepare_draft_operation,
    read_current_draft,
    read_draft_operation,
)
from mcp_telegram.drafts.ports import DraftPreflightBusyError
from mcp_telegram.drafts.snapshot import (
    DraftSnapshotError,
    build_clear_draft_request,
    build_save_draft_request,
    build_text_draft_request,
)
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.telegram_demand import RpcAttemptBudget, demand_context, require_execution_mode
from mcp_telegram.telegram_rpc_consumers import DemandKind, ExecutionMode
from mcp_telegram.telegram_rpc_error import describe_telegram_rpc_error
from mcp_telegram.telegram_rpc_scheduler import rpc_attempt_budget

logger = logging.getLogger(__name__)
_ACTIONS = frozenset({"create", "update", "clear", "restore"})
_OPERATION_KEY_LIMIT = 128
_VERSION_LIMIT = 256
_SNAPSHOT_FAILURE_MESSAGES = {
    "draft_formatting_conflict": "Changing this text invalidates its formatting; set drop_formatting=true to replace it with plain text.",
    "rich_text_update_unsupported": "This draft contains rich text blocks; edit that composition in Telegram.",
    "invalid_restore_snapshot": "The retained restoration payload is invalid; refresh the draft before changing it.",
    "media_dependencies_not_retained": "This draft depends on media that is not retained for restoration; edit it in Telegram.",
}


class _DraftClient(Protocol):
    async def __call__(self, request: object) -> object: ...


class DraftCommandError(ValueError):
    """Safe, actionable refusal before a draft mutation is dispatched."""

    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class _Command:
    action: Literal["create", "update", "clear", "restore"]
    scope: DraftScope
    operation_key: str
    expected_version: str | None
    text: str | None
    history_id: int | None
    drop_formatting: bool

    def fingerprint(self) -> str:
        payload = {
            "action": self.action,
            "account_id": self.scope.account_id,
            "dialog_id": self.scope.dialog_id,
            "topic_id": self.scope.top_message_id,
            "subdialog_peer_id": self.scope.subdialog_peer_id,
            "expected_version": self.expected_version,
            "text": self.text,
            "history_id": self.history_id,
            "drop_formatting": self.drop_formatting,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def _positive_optional(value: object, name: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value <= 0:
        raise DraftCommandError("invalid_arguments", f"{name} must be a positive integer.")
    return value


def _parse_scope(request: Mapping[str, object], account_id: int) -> DraftScope:
    selected_account = request.get("account_id", account_id)
    if selected_account is None:
        selected_account = account_id
    if type(selected_account) is not int or selected_account != account_id:
        raise DraftCommandError("account_mismatch", "The draft belongs to a different Telegram account.")
    dialog_id = request.get("dialog_id")
    if type(dialog_id) is not int or dialog_id == 0:
        raise DraftCommandError("invalid_arguments", "dialog_id must be an exact non-zero Telegram dialog ID.")
    topic_id = _positive_optional(request.get("topic_id"), "topic_id")
    if request.get("subdialog_peer_id") is not None:
        raise DraftCommandError("unsupported_scope", "Draft changes in saved-message subdialogs are not supported.")
    return DraftScope(account_id, dialog_id, topic_id)


def _expected_version(request: Mapping[str, object], action: str) -> str | None:
    expected = request.get("expected_version")
    if expected is not None and (not isinstance(expected, str) or not expected or len(expected) > _VERSION_LIMIT):
        raise DraftCommandError("invalid_arguments", "expected_version must be the version returned by get_draft.")
    if action != "create" and expected is None:
        raise DraftCommandError("version_required", "Read get_draft and supply its expected_version.")
    return expected


def _parse_command(request: Mapping[str, object], account_id: int) -> _Command:
    scope = _parse_scope(request, account_id)
    action = request.get("action")
    if not isinstance(action, str) or action not in _ACTIONS:
        raise DraftCommandError("invalid_arguments", "Choose create, update, clear, or restore.")
    operation_key = request.get("operation_key")
    if not isinstance(operation_key, str) or not operation_key.strip() or len(operation_key) > _OPERATION_KEY_LIMIT:
        raise DraftCommandError("invalid_arguments", "operation_key must contain between 1 and 128 characters.")
    expected = _expected_version(request, action)
    text, history_id, drop_formatting = _composition_arguments(request, action)
    return _Command(
        cast(Literal["create", "update", "clear", "restore"], action),
        scope,
        operation_key,
        expected,
        text,
        history_id,
        drop_formatting,
    )


def _draft_text(text: object, action: str) -> str | None:
    if action in {"create", "update"}:
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_DRAFT_TEXT_LENGTH:
            raise DraftCommandError("invalid_arguments", "Supply non-empty bounded text; use clear to remove a draft.")
        try:
            text.encode("utf-8")
        except UnicodeEncodeError:
            raise DraftCommandError("invalid_arguments", "Draft text must contain valid Unicode.") from None
    elif text is not None:
        raise DraftCommandError("invalid_arguments", "text is only accepted for create and update.")
    return cast(str | None, text)


def _composition_arguments(request: Mapping[str, object], action: str) -> tuple[str | None, int | None, bool]:
    text = _draft_text(request.get("text"), action)
    history_id = _positive_optional(request.get("history_id"), "history_id")
    if (action == "restore") != (history_id is not None):
        raise DraftCommandError("invalid_arguments", "history_id is required only for restore.")
    drop_formatting = request.get("drop_formatting", False)
    if type(drop_formatting) is not bool or (drop_formatting and action != "update"):
        raise DraftCommandError("invalid_arguments", "drop_formatting is a boolean for update only.")
    return text, history_id, drop_formatting


def _public_current(state: DraftState) -> dict[str, object]:
    return {
        "presence": state.presence,
        "expected_version": state.expected_version,
        "text": state.text,
        "observed_at": state.observed_at,
        "uncertain": state.uncertain,
    }


class DraftCommandOwner:
    """Serialize same-scope commands without a second queue or private recovery lock."""

    def __init__(  # noqa: PLR0913 - explicit existing-owner wiring
        self,
        client: object,
        reader_conn: sqlite3.Connection,
        *,
        writer: SQLiteWriter,
        account_id: int,
        resolve_peer: Callable[[DraftScope], Awaitable[object | None]],
        preflight: Callable[[], Awaitable[bool]],
        request_recovery: Callable[[str], Awaitable[None]],
    ) -> None:
        if type(account_id) is not int or account_id <= 0:
            raise ValueError("account_id must be positive")
        self._client = cast(_DraftClient, client)
        self._reader = reader_conn
        self._writer = writer
        self._account_id = account_id
        self._resolve_peer = resolve_peer
        self._preflight = preflight
        self._request_recovery = request_recovery
        self._locks: WeakValueDictionary[DraftScope, asyncio.Lock] = WeakValueDictionary()

    async def execute(self, request: Mapping[str, object]) -> dict[str, object]:  # noqa: PLR0911 - explicit safe error categories
        """Run one explicit draft command; duplicate keys never dispatch again."""
        try:
            command = _parse_command(request, self._account_id)
        except DraftCommandError as exc:
            return self._failure(exc)
        lock = self._locks.setdefault(command.scope, asyncio.Lock())
        async with lock:
            try:
                duplicate = self._duplicate(command)
                if duplicate is not None:
                    return duplicate
                with demand_context(DemandKind.DRAFT_MUTATION):
                    require_execution_mode(ExecutionMode.INLINE)
                    return await self._execute_new(command)
            except DraftCommandError as exc:
                return self._failure(exc, command)
            except DraftPreflightBusyError:
                return self._failure(
                    DraftCommandError(
                        "draft_reconciliation_busy",
                        "Draft reconciliation is already running; no change was dispatched.",
                    ),
                    command,
                )
            except DraftVersionConflictError:
                return self._failure(
                    DraftCommandError("version_conflict", "The draft changed; read its current version."), command
                )
            except DraftOperationConflictError:
                return self._failure(
                    DraftCommandError("operation_conflict", "operation_key was already used for another command."),
                    command,
                )
            except DraftRestoreUnavailableError:
                return self._failure(
                    DraftCommandError("restore_unavailable", "The current draft cannot be backed up for restoration."),
                    command,
                )
            except DraftSnapshotError as exc:
                return self._failure(
                    DraftCommandError(
                        exc.reason,
                        _SNAPSHOT_FAILURE_MESSAGES.get(
                            exc.reason, "This draft composition cannot be changed without losing fields."
                        ),
                    ),
                    command,
                )
            except Exception as exc:  # noqa: BLE001 - sanitize the pre-dispatch external boundary
                logger.warning("draft_command_rejected error_type=%s", type(exc).__name__)
                return self._failure(
                    DraftCommandError(
                        "preflight_unavailable", "Draft verification is unavailable; no change was dispatched."
                    ),
                    command,
                )

    def _duplicate(self, command: _Command) -> dict[str, object] | None:
        receipt = read_draft_operation(self._reader, self._account_id, command.operation_key)
        if receipt is None:
            return None
        if receipt.request_fingerprint != command.fingerprint():
            raise DraftOperationConflictError("operation key has another payload")
        if receipt.result:
            return dict(receipt.result)
        return self._result(command, "unknown", receipt.backup_history_id, dispatched=None, acknowledged=None)

    async def _execute_new(self, command: _Command) -> dict[str, object]:
        peer = await self._resolve_peer(command.scope)
        self._validate_peer(peer, command.scope)
        with rpc_attempt_budget(RpcAttemptBudget(1)):
            if not await self._preflight():
                raise DraftCommandError(
                    "draft_state_unknown", "Telegram did not provide a complete current draft snapshot."
                )
        current = read_current_draft(self._reader, command.scope)
        self._validate_current(command, current)
        request = self._build_request(command, current, peer)
        receipt = await self._prepare(command, current, backup=request is not None)
        if receipt.outcome != "pending":
            return dict(receipt.result)
        if request is None:
            result = self._result(command, "unchanged", receipt.backup_history_id, dispatched=False, acknowledged=None)
            await self._finish(command, result)
            return result
        return await self._dispatch(command, request, receipt.backup_history_id)

    @staticmethod
    def _validate_peer(peer: object | None, scope: DraftScope) -> None:
        if isinstance(peer, types.InputPeerSelf):
            matches = scope.dialog_id == scope.account_id
        elif isinstance(peer, (types.InputPeerUser, types.InputPeerChannel, types.InputPeerChat)):
            matches = get_peer_id(peer) == scope.dialog_id
        else:
            raise DraftCommandError(
                "peer_unavailable", "Exact peer access is not retained; wait for dialog synchronization."
            )
        if not matches:
            raise DraftCommandError("peer_mismatch", "Retained peer identity does not match the requested dialog.")

    @staticmethod
    def _validate_current(command: _Command, current: DraftState) -> None:
        if current.uncertain or current.presence == "unknown" or current.expected_version is None:
            raise DraftCommandError(
                "draft_state_unknown", "The current draft state is uncertain; wait for reconciliation."
            )
        if command.expected_version is not None and command.expected_version != current.expected_version:
            raise DraftVersionConflictError("draft version changed")
        if command.action == "create" and current.presence != "absent":
            raise DraftCommandError("draft_exists", "A draft already exists; read its version and use update.")
        if command.action == "update" and current.presence != "present":
            raise DraftCommandError("draft_absent", "The draft was cleared; use create for a new draft.")

    def _build_request(self, command: _Command, current: DraftState, peer: object) -> SaveDraftRequest | None:
        if command.action == "clear":
            return None if current.presence == "absent" else build_clear_draft_request(peer, command.scope)
        if command.action == "restore":
            return self._restore_request(command, current, peer)
        assert command.text is not None
        if command.action == "update" and command.text == current.text:
            return None
        return build_text_draft_request(
            peer,
            command.scope,
            command.text,
            existing=current.restore_snapshot if command.action == "update" else None,
            drop_formatting=command.drop_formatting,
        )

    def _restore_request(self, command: _Command, current: DraftState, peer: object) -> SaveDraftRequest | None:
        assert command.history_id is not None
        historical = get_draft_history(self._reader, command.scope, command.history_id)
        if historical is None:
            raise DraftCommandError("history_unavailable", "That draft version expired or belongs to another scope.")
        if historical.presence == "absent":
            return None if current.presence == "absent" else build_clear_draft_request(peer, command.scope)
        if historical.restore_snapshot is None:
            raise DraftCommandError("restore_unavailable", "That version has no complete restoration payload.")
        if (
            current.restore_snapshot is not None
            and historical.restore_snapshot.semantic_fingerprint == current.restore_snapshot.semantic_fingerprint
        ):
            return None
        return build_save_draft_request(peer, command.scope, historical.restore_snapshot)

    async def _prepare(self, command: _Command, current: DraftState, *, backup: bool) -> DraftOperationReceipt:
        expected_version = current.expected_version
        assert expected_version is not None

        def persist(conn: sqlite3.Connection) -> DraftOperationReceipt:
            with write_transaction(conn):
                return prepare_draft_operation(
                    conn,
                    command.scope,
                    command.operation_key,
                    command.fingerprint(),
                    expected_version,
                    backup=backup,
                )

        return await self._writer.run(persist)

    async def _dispatch(
        self, command: _Command, request: SaveDraftRequest, backup_history_id: int | None
    ) -> dict[str, object]:
        budget = RpcAttemptBudget(1)
        try:
            with rpc_attempt_budget(budget):
                response = await self._client(request)
        except BaseException as exc:
            result = self._dispatch_failure(command, exc, budget, backup_history_id)
            await self._finish(command, result)
            if not isinstance(exc, Exception):
                raise
            await self._recover()
            return result
        result = self._result(
            command,
            "acknowledged" if response is True else "unknown",
            backup_history_id,
            dispatched=bool(budget.attempts) or response is True,
            acknowledged=True if response is True else None,
        )
        await self._finish(command, result)
        await self._recover()
        return result

    def _dispatch_failure(
        self, command: _Command, error: BaseException, budget: RpcAttemptBudget, backup_history_id: int | None
    ) -> dict[str, object]:
        descriptor = describe_telegram_rpc_error(error)
        definite = (
            isinstance(error, RPCError) and descriptor.code is not None and 400 <= descriptor.code < 500  # noqa: PLR2004 - Telegram's definitive client-error range
        )
        if budget.attempts and not definite:
            return self._result(command, "unknown", backup_history_id, dispatched=True, acknowledged=None)
        result = self._failure(
            DraftCommandError(
                descriptor.symbol or "mutation_rejected",
                "Telegram rejected the change." if definite else "The change was not dispatched.",
            ),
            command,
        )
        result.update(
            mutation_dispatched=bool(budget.attempts), save_acknowledged=False, backup_history_id=backup_history_id
        )
        return result

    async def _finish(self, command: _Command, result: dict[str, object]) -> None:
        result["result_persisted"] = True

        def persist(conn: sqlite3.Connection) -> None:
            with write_transaction(conn):
                finish_draft_operation(conn, self._account_id, command.operation_key, str(result["outcome"]), result)

        try:
            await self._writer.run(persist)
        except Exception as exc:  # noqa: BLE001 - remote acknowledgement survives local receipt failure
            logger.error("draft_mutation_receipt_unavailable error_type=%s", type(exc).__name__)
            result["result_persisted"] = False
            result["next_action"] = (
                "The command result could not be saved. Do not repeat the write; inspect get_draft after recovery."
            )

    async def _recover(self) -> None:
        try:
            await self._request_recovery("draft_mutation")
        except Exception as exc:  # noqa: BLE001 - preserve the already persisted remote result
            logger.warning("draft_mutation_recovery_unavailable error_type=%s", type(exc).__name__)

    def _result(
        self,
        command: _Command,
        outcome: str,
        backup_history_id: int | None,
        *,
        dispatched: bool | None,
        acknowledged: bool | None,
    ) -> dict[str, object]:
        current = self._current_receipt(command.scope)
        return {
            "ok": True,
            "outcome": outcome,
            "operation_key": command.operation_key,
            "mutation_dispatched": dispatched,
            "save_acknowledged": acknowledged,
            "result_persisted": False,
            "backup_history_id": backup_history_id,
            "current": current,
            "current_is_operation_confirmation": (
                outcome == "unchanged"
                and current["presence"] != "unknown"
                and not current["uncertain"]
                and current["expected_version"] == command.expected_version
            ),
            "next_action": (
                "Do not repeat the write; read get_draft after reconciliation. Reuse operation_key only to inspect this result."
                if outcome == "unknown"
                else "Read get_draft for the current observed state; the draft remains unsent."
            ),
        }

    def _failure(self, error: DraftCommandError, command: _Command | None = None) -> dict[str, object]:
        action = (
            "Retry the same command with the same operation_key after draft reconciliation finishes."
            if error.reason == "draft_reconciliation_busy"
            else "Read get_draft and address the stated reason before a new command."
        )
        result: dict[str, object] = {
            "ok": False,
            "outcome": "conflict"
            if error.reason in {"version_conflict", "draft_exists", "operation_conflict"}
            else "rejected",
            "error": error.reason,
            "message": f"{error.message} Action: {action}",
        }
        if command is not None:
            result["operation_key"] = command.operation_key
            result["current"] = self._current_receipt(command.scope)
        return result

    def _current_receipt(self, scope: DraftScope) -> dict[str, object]:
        try:
            return _public_current(read_current_draft(self._reader, scope))
        except Exception as exc:  # noqa: BLE001 - a read-model failure cannot negate a completed remote mutation
            logger.warning("draft_command_current_unavailable error_type=%s", type(exc).__name__)
            return {
                "presence": "unknown",
                "expected_version": None,
                "text": None,
                "observed_at": None,
                "uncertain": True,
            }
