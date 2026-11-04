"""Bounded, version-checked native private contact-note commands."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass
from typing import Literal, Protocol, cast

from telethon.tl import types
from telethon.tl.functions.contacts import UpdateContactNoteRequest

from ..auth_scope import TelegramAuthScope
from ..contacts.guard import ContactPeerGuard
from ..entity_profile.contracts import CommandProfileAcquisition
from ..native_write_fence import NativeWriteCheck, NativeWriteReceiptBinding, NoteWriteRejected, native_write_context
from ..sqlite_writer import SQLiteWriter
from ..sync_transactions import write_transaction
from ..telegram_demand import RpcAttemptBudget, UnclassifiedTelegramDemandError, create_demand_token, demand_context
from ..telegram_rpc_consumers import DemandKind, TelegramRpcSource
from ..telegram_rpc_scheduler import current_rpc_scope, rpc_attempt_budget, rpc_scope
from . import receipts
from .composition import NoteIntent, compose_note
from .intake import is_current, note_semantic_fingerprint, track_contact_note
from .state import note_observation_order, read_note_command_state

_MAX_KEY = 128
_VERSION_LENGTH = 64
_DEFAULT_LIMIT = 128
logger = logging.getLogger(__name__)


class NoteWatch(Protocol):
    def arm(self, binding: NativeWriteReceiptBinding) -> None: ...
    def intake_current(self) -> bool: ...
    def is_current(self) -> bool: ...


class NoteAuthority(Protocol):
    def watch(self, entity_id: int) -> AbstractContextManager[NoteWatch]: ...


@dataclass(frozen=True, slots=True)
class _Command:
    entity: int
    action: Literal["create", "update", "clear"]
    key: str
    version: str
    text: str | None
    drop: bool

    def fingerprint(self, account_id: int) -> str:
        data = [account_id, self.entity, self.action, self.version, self.text, self.drop]
        return hashlib.sha256(json.dumps(data, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


@contextmanager
def _phase(budget: RpcAttemptBudget) -> Iterator[None]:
    """Limit this phase without replacing or replenishing the shared budget."""
    original = budget.limit
    budget.limit = min(original, budget.attempts + 1)
    try:
        yield
    finally:
        budget.limit = original


def _command_deadline() -> float:
    try:
        inherited = current_rpc_scope().deadline
    except UnclassifiedTelegramDemandError:
        inherited = None
    return create_demand_token(DemandKind.NOTE_COMMAND, deadline=inherited).admission_deadline


class ContactNoteCommandOwner:
    """Serialize one account/peer command and retain its physical receipt."""

    def __init__(  # noqa: PLR0913 - existing owners are explicitly injected
        self,
        client: object,
        reader_conn: sqlite3.Connection,
        *,
        writer: SQLiteWriter,
        authority: NoteAuthority,
        identity: Callable[[], TelegramAuthScope | None],
        resolve_input_user: Callable[[int], types.InputUser | None],
        acquire_command_profile: Callable[[int], Awaitable[CommandProfileAcquisition]],
        acquire_config: Callable[[int], Awaitable[tuple[int, int | None]]],
        request_reconciliation: Callable[[int], Awaitable[bool]],
        peer_guard: ContactPeerGuard | None = None,
    ) -> None:
        self._client = cast(Callable[[object], Awaitable[object]], client)
        self._reader = reader_conn
        self._writer = writer
        self._authority = authority
        self._identity = identity
        self._resolve = resolve_input_user
        self._acquire = acquire_command_profile
        self._config = acquire_config
        self._request_reconciliation = request_reconciliation
        self._config_scope: TelegramAuthScope | None = None
        self._config_hash = 0
        self._limit = _DEFAULT_LIMIT
        self._peer_guard = peer_guard or ContactPeerGuard()

    async def execute(self, request: Mapping[str, object]) -> dict[str, object]:
        deadline = _command_deadline()
        scope = self._identity()
        if scope is None:
            return {"error": "account_unavailable"}
        command: _Command | None = None
        try:
            command = _parse(request, scope.account_id)
            async with asyncio.timeout(max(0, deadline - time.monotonic())):
                async with self._peer_guard.lock(scope.account_id, command.entity):
                    return await self._locked(scope, command, deadline)
        except (ValueError, NoteWriteRejected) as error:
            return {"error": error.code if isinstance(error, NoteWriteRejected) else str(error)}
        except TimeoutError:
            return self._timeout_result(scope, command) if command is not None else {"error": "command_timeout"}

    async def _locked(self, scope: TelegramAuthScope, command: _Command, deadline: float) -> dict[str, object]:
        existing = self._receipt(command.key)
        if existing is not None:
            if existing[0] != scope.account_id or existing[1] != command.fingerprint(scope.account_id):
                raise ValueError("operation_conflict")
            return _historical_result(existing)
        if command.entity == scope.account_id:
            raise ValueError("contact_required")
        if self._has_quarantine(scope.account_id, command.entity):
            raise ValueError("note_outcome_unresolved")
        return await self._bounded(scope, command, deadline)

    async def _bounded(self, scope: TelegramAuthScope, command: _Command, deadline: float) -> dict[str, object]:
        try:
            inherited = current_rpc_scope()
        except UnclassifiedTelegramDemandError:
            inherited = None
        if inherited is not None and inherited.demand_kind != DemandKind.NOTE_COMMAND:
            raise ValueError("invalid_demand_context")
        root = demand_context(DemandKind.NOTE_COMMAND, deadline=deadline) if inherited is None else nullcontext()
        with root, rpc_scope(TelegramRpcSource.NOTE_COMMAND, deadline=deadline):
            current = current_rpc_scope()
            budget = current.attempt_budget or RpcAttemptBudget(4)
            original_limit = budget.limit
            budget.limit = min(original_limit, budget.attempts + 4)
            try:
                with rpc_attempt_budget(budget):
                    return await self._execute(scope, command, budget)
            finally:
                budget.limit = original_limit

    async def _execute(
        self, scope: TelegramAuthScope, command: _Command, budget: RpcAttemptBudget
    ) -> dict[str, object]:
        await self._load_config(scope, budget)
        with self._authority.watch(command.entity) as watch:
            with _phase(budget):
                accepted = await self._acquire_profile(command.entity)
            if not accepted.accepted or not accepted.contact_current:
                raise ValueError("contact_required")
            with track_contact_note(command.entity) as marker:
                state = self._current(scope, command)
                if accepted.observation_order is None or state["observation_order"] != accepted.observation_order:
                    raise ValueError("note_state_changed")
                note = compose_note(
                    NoteIntent(command.action, command.text, command.drop),
                    str(state["state"]),
                    state["value"],
                    self._limit,
                )
                peer = self._peer(command.entity)
                binding = self._binding(scope, command, note or types.TextWithEntities("", []))
                if note is None:
                    watch.arm(binding)
                reserved, outcome = await self._reserve(
                    binding, command, scope, watch, accepted.observation_order, unchanged=note is None
                )
                if note is None:
                    return await self._unchanged_result(scope, binding, watch, outcome, accepted.observation_order)
                if not reserved:
                    return {"outcome": outcome, "verification": "pending"}
                try:
                    watch.arm(binding)
                except BaseException:
                    await self._reject_unclaimed(binding)
                    raise
                return await self._mutate(scope, command, peer, note, binding, marker, budget)

    async def _unchanged_result(
        self,
        scope: TelegramAuthScope,
        binding: NativeWriteReceiptBinding,
        watch: NoteWatch,
        outcome: str,
        order: int | None,
    ) -> dict[str, object]:
        if outcome != "unchanged":
            return {"outcome": outcome, "verification": "pending"}
        result: dict[str, object] = {"outcome": "unchanged", "verification": "verified", "verification_order": order}
        if not watch.is_current() or self._identity() != scope:
            result["current_applicability"] = "unknown"
        return result

    async def _acquire_profile(self, entity: int) -> CommandProfileAcquisition:
        try:
            return await self._acquire(entity)
        except ValueError as error:
            raise ValueError("contact_unavailable") from error

    async def _load_config(self, scope: TelegramAuthScope, budget: RpcAttemptBudget) -> None:
        if self._config_scope != scope:
            self._config_scope, self._config_hash, self._limit = scope, 0, _DEFAULT_LIMIT
        with _phase(budget):
            config_hash, limit = await self._config(self._config_hash)
        if self._identity() != scope:
            raise ValueError("account_mismatch")
        self._config_hash = config_hash
        if limit is not None:
            self._limit = limit

    def _current(self, scope: TelegramAuthScope, command: _Command) -> dict[str, object]:
        state = read_note_command_state(self._reader, scope.account_id, command.entity)
        if self._identity() != scope:
            raise ValueError("account_mismatch")
        if state["contact"] is not True:
            raise ValueError("contact_required")
        if state["state"] == "unknown":
            raise ValueError("note_state_unknown")
        if state["expected_version"] != command.version:
            raise ValueError("version_conflict")
        return state

    def _peer(self, entity: int) -> types.InputUser:
        peer = self._resolve(entity)
        if not isinstance(peer, types.InputUser) or peer.user_id != entity:
            raise ValueError("contact_unavailable")
        return peer

    @staticmethod
    def _binding(
        scope: TelegramAuthScope, command: _Command, note: types.TextWithEntities
    ) -> NativeWriteReceiptBinding:
        return NativeWriteReceiptBinding(
            "contact_note",
            scope.account_id,
            scope.dc_id,
            scope.auth_key_id,
            command.key,
            command.action,
            command.entity,
            note_semantic_fingerprint(note),
            1,
        )

    async def _reserve(  # noqa: PLR0913 - explicit no-op evidence
        self,
        binding: NativeWriteReceiptBinding,
        command: _Command,
        scope: TelegramAuthScope,
        watch: NoteWatch,
        order: int | None,
        *,
        unchanged: bool,
    ) -> tuple[bool, str]:
        def reserve(conn: sqlite3.Connection) -> tuple[bool, str]:
            return receipts.reserve_operation(
                conn,
                binding,
                command.fingerprint(binding.account_id),
                int(time.time()),
                expected_version=command.version,
                unchanged=unchanged,
                no_op_check=lambda db: self._no_op_current(db, scope, command, watch, order),
                no_op_order=order,
            )

        try:
            return await self._writer.run(reserve)
        except asyncio.CancelledError:
            if not unchanged:
                await self._reject_unclaimed(binding)
            raise

    def _no_op_current(
        self, conn: sqlite3.Connection, scope: TelegramAuthScope, command: _Command, watch: NoteWatch, order: int | None
    ) -> bool:
        state = read_note_command_state(conn, scope.account_id, command.entity)
        return (
            state["contact"] is True
            and state["expected_version"] == command.version
            and state["observation_order"] == order
            and state["state"] != "unknown"
            and self._identity() == scope
            and watch.is_current()
        )

    async def _mutate(  # noqa: PLR0913, PLR0917 - explicit write evidence
        self,
        scope: TelegramAuthScope,
        command: _Command,
        peer: types.InputUser,
        note: types.TextWithEntities,
        binding: NativeWriteReceiptBinding,
        marker: tuple[int, int],
        budget: RpcAttemptBudget,
    ) -> dict[str, object]:
        request = UpdateContactNoteRequest(peer, note)

        async def revalidate() -> Callable[[], bool]:
            try:
                self._current(scope, command)
            except ValueError as error:
                raise NoteWriteRejected(str(error)) from error
            return lambda: (
                self._identity() == scope
                and is_current(command.entity, marker)
                and self._resolve(command.entity) == peer
            )

        check = NativeWriteCheck(binding, bytes(request), revalidate)
        try:
            with native_write_context(check), _phase(budget):
                await self._client(request)
            row = self._receipt(command.key)
            if row is None or row[2] != "acknowledged":
                await self._offer_if_needed(scope, binding)
                return {"outcome": row[2] if row else "unknown", "verification": "pending"}
            result = await self._postflight(scope, command, note, binding, budget)
            if result["verification"] == "unavailable":
                await self._offer_if_needed(scope, binding)
            return result
        except BaseException:
            cleanup = asyncio.create_task(self._cleanup_mutation(scope, binding))
            while not cleanup.done():
                try:
                    await asyncio.wait({cleanup})
                except asyncio.CancelledError:
                    continue
            try:
                cleanup.result()
            except BaseException as error:  # noqa: BLE001 - preserve the original failure
                logger.warning("note_cleanup_failed error_type=%s", type(error).__name__)
            raise

    async def _cleanup_mutation(self, scope: TelegramAuthScope, binding: NativeWriteReceiptBinding) -> None:
        await self._reject_unclaimed(binding)
        await self._offer_if_needed(scope, binding)

    async def _offer_if_needed(self, scope: TelegramAuthScope, binding: NativeWriteReceiptBinding) -> None:
        if self._identity() != scope:
            return
        row = cast(
            tuple[str, str | None, str] | None,
            self._reader.execute(
                "SELECT outcome,attempt_id,verification FROM contact_note_operation_receipts "
                "WHERE account_id=? AND operation_key=?",
                (binding.account_id, binding.operation_key),
            ).fetchone(),
        )
        if row is None:
            return
        uncertain = (row[0] == "unknown" and row[2] in {"pending", "unavailable"}) or (
            row[0] == "pending" and row[1] is not None
        )
        unverified = row[0] == "acknowledged" and row[2] in {"pending", "unavailable"}
        if uncertain or unverified:
            try:
                await self._request_reconciliation(binding.target)
            except Exception as error:  # noqa: BLE001 - preserve the durable wire result
                logger.warning("note_reconciliation_offer_failed error_type=%s", type(error).__name__)

    async def _postflight(
        self,
        scope: TelegramAuthScope,
        command: _Command,
        note: types.TextWithEntities,
        binding: NativeWriteReceiptBinding,
        budget: RpcAttemptBudget,
    ) -> dict[str, object]:
        with self._authority.watch(command.entity) as watch:
            order: int | None = None
            try:
                with _phase(budget):
                    accepted = await self._acquire_profile(command.entity)
            except Exception:  # noqa: BLE001 - preserve the acknowledged receipt if readback fails
                verification = "unavailable"
            else:
                order = accepted.observation_order
                valid = accepted.accepted and watch.intake_current()
                verification = self._verification(scope, command, note, valid, order)
            verification, published, saved_order = await self._record_verification(binding, verification, watch, order)
            result: dict[str, object] = {"outcome": "acknowledged", "verification": verification}
            if saved_order is not None:
                result["verification_order"] = saved_order
            if not published:
                result["current_applicability"] = "unknown"
                return result
            if verification != "unavailable" and (
                not watch.intake_current()
                or self._identity() != scope
                or not self._order_current(self._reader, binding, order)
            ):
                result["current_applicability"] = "unknown"
            return result

    def _verification(
        self,
        scope: TelegramAuthScope,
        command: _Command,
        note: types.TextWithEntities,
        accepted: bool,
        order: int | None,
    ) -> str:
        if not accepted or self._identity() != scope:
            return "unavailable"
        current = read_note_command_state(self._reader, scope.account_id, command.entity)
        if order is None or current["observation_order"] != order:
            return "unavailable"
        return "verified" if self._note_matches(command, note, current) else "mismatch"

    @staticmethod
    def _note_matches(command: _Command, note: types.TextWithEntities, current: dict[str, object]) -> bool:
        value = current.get("value")
        return (command.action == "clear" and current["state"] == "absent") or (
            command.action != "clear"
            and isinstance(value, dict)
            and current["state"] == "retained"
            and value.get("text") == note.text
            and value.get("entities") == []
        )

    @staticmethod
    def _order_current(conn: sqlite3.Connection, binding: NativeWriteReceiptBinding, order: int | None) -> bool:
        return order is not None and note_observation_order(conn, binding.account_id, binding.target) == order

    async def _record_verification(
        self, binding: NativeWriteReceiptBinding, verification: str, watch: NoteWatch, order: int | None
    ) -> tuple[str, bool, int | None]:
        def update(conn: sqlite3.Connection) -> tuple[str, bool, int | None]:
            with write_transaction(conn):
                result = (
                    verification
                    if self._order_current(conn, binding, order) and watch.intake_current()
                    else "unavailable"
                )
                changed = conn.execute(
                    "UPDATE contact_note_operation_receipts SET verification=?,verification_order=?, "
                    "result_json=json_remove(result_json,'$.verification_reason') "
                    "WHERE account_id=? AND operation_key=? AND outcome='acknowledged' "
                    "AND verification IN ('pending','unavailable','inconclusive') "
                    "AND (verification!='inconclusive' OR (?!='unavailable' AND terminal_observation_order<?))",
                    (
                        result,
                        order if result != "unavailable" else None,
                        binding.account_id,
                        binding.operation_key,
                        result,
                        order,
                    ),
                )
                if changed.rowcount == 1:
                    return result, True, order if result != "unavailable" else None
                row = cast(
                    tuple[str, int | None] | None,
                    conn.execute(
                        "SELECT verification,verification_order FROM contact_note_operation_receipts "
                        "WHERE account_id=? AND operation_key=? AND outcome='acknowledged'",
                        (binding.account_id, binding.operation_key),
                    ).fetchone(),
                )
                if row is None or row[0] not in {"verified", "mismatch", "inconclusive"}:
                    raise RuntimeError("note verification receipt changed")
                return row[0], False, row[1]

        return await self._writer.run(update)

    async def _reject_unclaimed(self, binding: NativeWriteReceiptBinding) -> None:
        def update(conn: sqlite3.Connection) -> None:
            with write_transaction(conn):
                conn.execute(
                    "UPDATE contact_note_operation_receipts SET outcome='rejected' WHERE account_id=? "
                    "AND operation_key=? AND dc_id=? AND key_id=? AND action=? AND entity_id=? "
                    "AND semantic_fingerprint=? AND binding_revision=? AND outcome='pending' AND attempt_id IS NULL",
                    (
                        binding.account_id,
                        binding.operation_key,
                        binding.dc_id,
                        str(binding.key_id),
                        binding.action,
                        binding.target,
                        binding.semantic_fingerprint,
                        binding.binding_revision,
                    ),
                )

        await self._writer.run(update)

    def _receipt(self, key: str) -> sqlite3.Row | tuple[object, ...] | None:
        rows = self._reader.execute(
            "SELECT account_id,request_fingerprint,outcome,verification,verification_order FROM contact_note_operation_receipts WHERE operation_key=?",
            (key,),
        ).fetchall()
        if len(rows) > 1:
            raise ValueError("operation_conflict")
        return rows[0] if rows else None

    def _has_quarantine(self, account: int, entity: int) -> bool:
        return (
            self._reader.execute(
                "SELECT 1 FROM contact_note_operation_receipts WHERE account_id=? AND entity_id=? AND (outcome='pending' OR (outcome='unknown' AND verification!='observed_after_unknown')) LIMIT 1",
                (account, entity),
            ).fetchone()
            is not None
        )

    def _timeout_result(self, scope: TelegramAuthScope, command: _Command) -> dict[str, object]:
        row = self._receipt(command.key)
        if row is None:
            return {"error": "command_timeout"}
        if row[0] != scope.account_id or row[1] != command.fingerprint(scope.account_id):
            return {"error": "operation_conflict"}
        return _historical_result(row)


def _historical_result(row: sqlite3.Row | tuple[object, ...]) -> dict[str, object]:
    result = {"outcome": row[2], "verification": row[3]}
    if row[4] is not None:
        result["verification_order"] = row[4]
    if row[3] in {"verified", "mismatch"}:
        result["current_applicability"] = "unknown"
    return result


def _parse(request: Mapping[str, object], account_id: int) -> _Command:
    entity = request.get("dialog_id")
    action = request.get("action")
    selected = request.get("account_id", account_id)
    if selected is not None and (type(selected) is not int or selected != account_id):
        raise ValueError("account_mismatch")
    if (
        type(entity) is not int
        or entity <= 0
        or not isinstance(action, str)
        or action not in {"create", "update", "clear"}
    ):
        raise ValueError("invalid_arguments")
    key, version = _parse_identity(request)
    text, drop = _parse_composition(request, action)
    return _Command(entity, cast(Literal["create", "update", "clear"], action), key, version, text, drop)


def _parse_identity(request: Mapping[str, object]) -> tuple[str, str]:
    key, version = request.get("operation_key"), request.get("expected_version")
    if not isinstance(key, str) or not key.strip() or len(key) > _MAX_KEY:
        raise ValueError("invalid_arguments")
    if not isinstance(version, str) or len(version) != _VERSION_LENGTH:
        raise ValueError("invalid_arguments")
    return key, version


def _parse_composition(request: Mapping[str, object], action: str) -> tuple[str | None, bool]:
    text, drop = request.get("text"), request.get("drop_formatting", False)
    if type(drop) is not bool or (drop and action != "update") or (action == "clear" and text is not None):
        raise ValueError("invalid_arguments")
    if action != "clear" and not isinstance(text, str):
        raise ValueError("invalid_arguments")
    return cast(str | None, text), drop
