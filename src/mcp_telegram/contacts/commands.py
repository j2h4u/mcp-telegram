"""Bounded native contact commands using canonical account-owned facts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Literal, cast

from telethon.errors import RPCError
from telethon.tl import functions, types
from telethon.tl.tlobject import TLRequest

from ..auth_scope import TelegramAuthScope
from ..contact_notes.commands import _phase
from ..contact_notes.domain import _drain
from ..flood import TelegramRpcThrottled
from ..native_write_fence import ContactWriteRejected, NativeWriteCheck, NativeWriteReceiptBinding, native_write_context
from ..sqlite_writer import SQLiteWriter
from ..sync_transactions import write_transaction
from ..telegram_demand import (
    RpcAttemptBudget,
    RpcAttemptBudgetExhaustedError,
    UnclassifiedTelegramDemandError,
    create_demand_token,
    demand_context,
)
from ..telegram_rpc_consumers import DemandKind, TelegramRpcSource
from ..telegram_rpc_scheduler import (
    RpcAdmissionError,
    TelegramRpcMethodRecoveryPending,
    current_rpc_scope,
    rpc_attempt_budget,
    rpc_scope,
)
from . import receipts
from .arguments import ContactCommandArguments
from .composition import ContactIntent, compose_contact, contact_fingerprint
from .contracts import ContactCommandState, ContactProfileAcquisition
from .domain import ContactWriteAuthority, ContactWriteWatch
from .guard import ContactPeerGuard
from .state import read_contact_command_state

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _Command:
    entity: int
    action: Literal["create", "update", "delete"]
    key: str
    version: str
    first: str | None
    last: str | None

    def fingerprint(self, account_id: int) -> str:
        value = [account_id, self.entity, self.action, self.version, self.first, self.last]
        return hashlib.sha256(json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


def _parse(request: Mapping[str, object]) -> _Command:
    args = ContactCommandArguments.model_validate(request, strict=True)
    return _Command(
        args.entity_id, args.action, args.operation_key, args.expected_version, args.first_name, args.last_name
    )


class ContactCommandOwner:
    """Own one version-checked command, its receipt, and bounded readback."""

    def __init__(  # noqa: PLR0913 - existing owners are explicitly injected
        self,
        client: Callable[[object], Awaitable[object]],
        reader: sqlite3.Connection,
        *,
        writer: SQLiteWriter,
        authority: ContactWriteAuthority,
        guard: ContactPeerGuard,
        identity: Callable[[], TelegramAuthScope | None],
        resolve: Callable[[int], types.InputUser | None],
        acquire: Callable[[int], Awaitable[ContactProfileAcquisition]],
        reconcile: Callable[[int], Awaitable[bool]],
    ) -> None:
        self._client, self._reader, self._writer = client, reader, writer
        self._authority, self._guard, self._identity, self._resolve = authority, guard, identity, resolve
        self._acquire, self._reconcile = acquire, reconcile

    async def execute(self, request: Mapping[str, object]) -> dict[str, object]:
        scope = self._identity()
        if scope is None:
            return self._error("account_unavailable")
        try:
            command = _parse(request)
            try:
                inherited = current_rpc_scope()
            except UnclassifiedTelegramDemandError:
                inherited = None
            deadline = create_demand_token(
                DemandKind.CONTACT_COMMAND, deadline=inherited.deadline if inherited else None
            ).admission_deadline
            async with asyncio.timeout(max(0, deadline - time.monotonic())):
                async with self._guard.lock(scope.account_id, command.entity):
                    root = (
                        demand_context(DemandKind.CONTACT_COMMAND, deadline=deadline)
                        if inherited is None
                        else nullcontext()
                    )
                    with root, rpc_scope(TelegramRpcSource.CONTACT_COMMAND, deadline=deadline):
                        budget = current_rpc_scope().attempt_budget or RpcAttemptBudget(3)
                        with rpc_attempt_budget(budget):
                            result = await self._locked(scope, command, budget)
            return {
                "ok": True,
                "account_id": scope.account_id,
                "entity_id": command.entity,
                "operation_key": command.key,
                **result,
            }
        except (ValueError, ContactWriteRejected) as error:
            return self._error(error.code if isinstance(error, ContactWriteRejected) else str(error))
        except TimeoutError:
            return self._error("contact_completion_unconfirmed")

    @staticmethod
    def _error(code: str) -> dict[str, object]:
        return {
            "ok": False,
            "error": code,
            "required_action": "Read the current contact and reuse the operation key if completion is unconfirmed.",
        }

    def _receipt(self, command: _Command, scope: TelegramAuthScope) -> dict[str, object] | None:
        rows = self._reader.execute(
            "SELECT account_id,request_fingerprint,outcome,verification,verification_order "
            "FROM contact_operation_receipts WHERE operation_key=?",
            (command.key,),
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1 or rows[0][0] != scope.account_id or rows[0][1] != command.fingerprint(scope.account_id):
            raise ValueError("operation_conflict")
        result: dict[str, object] = {
            "outcome": rows[0][2],
            "verification": rows[0][3],
            "current_applicability": "unknown",
        }
        if rows[0][4] is not None:
            result["verification_order"] = rows[0][4]
        return result

    async def _locked(self, scope: TelegramAuthScope, command: _Command, budget: RpcAttemptBudget) -> dict[str, object]:
        prior = self._receipt(command, scope)
        if prior is not None:
            return prior
        if command.entity == scope.account_id:
            raise ValueError("contact_unavailable")
        if receipts.peer_quarantined(self._reader, scope.account_id, command.entity):
            raise ValueError("contact_outcome_unresolved")
        with self._authority.watch(command.entity) as watch:
            request, order = await self._preflight(scope, command, watch, budget)
            binding = self._binding(scope, command, request)
            try:
                reserved, outcome = await _drain(
                    self._writer.run(
                        lambda db: receipts.reserve_operation(
                            db,
                            binding,
                            command.fingerprint(scope.account_id),
                            int(time.time()),
                            expected_version=command.version,
                            verification_fingerprint=self._verification_fingerprint(command, request),
                            unchanged=request is None,
                            no_op_order=order,
                            no_op_check=lambda conn: self._current(conn, scope, command, watch, order),
                        )
                    )
                )
            except asyncio.CancelledError:
                if request is not None:
                    await self._cleanup(scope, binding)
                raise
            if not reserved or request is None:
                return {
                    "outcome": outcome,
                    "verification": "verified" if outcome == "unchanged" else "pending",
                    "current_applicability": "unknown",
                }
            return await self._claim_and_mutate(scope, command, watch, request, binding, budget, order)

    async def _preflight(
        self, scope: TelegramAuthScope, command: _Command, watch: ContactWriteWatch, budget: RpcAttemptBudget
    ) -> tuple[TLRequest | None, int]:
        with _phase(budget):
            acquired = await self._acquire(command.entity)
        state = read_contact_command_state(self._reader, scope.account_id, command.entity)
        order = acquired.observation_order
        if not acquired.accepted or order is None or state["observation_order"] != order or not watch.intake_current():
            raise ValueError("contact_state_changed")
        if self._identity() != scope or state["expected_version"] != command.version:
            raise ValueError("version_conflict")
        peer = self._resolve(command.entity)
        if not isinstance(peer, types.InputUser) or peer.user_id != command.entity:
            raise ValueError("contact_unavailable")
        return compose_contact(ContactIntent(command.action, command.first, command.last), state, peer), order

    async def _claim_and_mutate(  # noqa: PLR0913, PLR0917 - exact command owner context
        self,
        scope: TelegramAuthScope,
        command: _Command,
        watch: ContactWriteWatch,
        request: TLRequest,
        binding: NativeWriteReceiptBinding,
        budget: RpcAttemptBudget,
        order: int,
    ) -> dict[str, object]:
        try:
            watch.arm(binding, order)
            return await self._mutate(scope, command, watch, request, binding, budget, order)
        except RPCError as error:
            prior = await self._settled_failure(scope, command, binding, "rejected")
            if prior is not None:
                prior["telegram_error"] = {"type": type(error).__name__, "code": error.code}
                prior["required_action"] = "Review Telegram's refusal and the current contact before a new operation."
                return prior
            raise
        except ConnectionError, TimeoutError:
            prior = await self._settled_failure(scope, command, binding, "unknown")
            if prior is not None:
                return prior
            raise
        except BaseException:
            await self._cleanup(scope, binding)
            raise

    async def _settled_failure(
        self, scope: TelegramAuthScope, command: _Command, binding: NativeWriteReceiptBinding, outcome: str
    ) -> dict[str, object] | None:
        cancelled, cleanup_failed = await self._cleanup(scope, binding)
        if cancelled:
            raise asyncio.CancelledError
        if cleanup_failed or not self._claimed_outcome(binding, outcome):
            return None
        return self._receipt(command, scope)

    def _claimed_outcome(self, binding: NativeWriteReceiptBinding, outcome: str) -> bool:
        return (
            self._reader.execute(
                "SELECT 1 FROM contact_operation_receipts WHERE account_id=? AND operation_key=? "
                "AND outcome=? AND attempt_id IS NOT NULL",
                (binding.account_id, binding.operation_key, outcome),
            ).fetchone()
            is not None
        )

    async def _cleanup(self, scope: TelegramAuthScope, binding: NativeWriteReceiptBinding) -> tuple[bool, bool]:
        async def settle() -> None:
            await self._writer.run(lambda db: self._reject_unclaimed(db, binding))
            if self._identity() == scope:
                await self._reconcile(binding.target)

        task = asyncio.create_task(settle())
        cancelled = False
        while not task.done():
            try:
                await asyncio.wait({task})
            except asyncio.CancelledError:
                cancelled = True
                continue
        failed = False
        try:
            task.result()
        except BaseException as error:  # noqa: BLE001 - keep the original command failure
            failed = True
            logger.warning("contact_cleanup_failed error_type=%s", type(error).__name__)
        return cancelled, failed

    def _current(
        self,
        conn: sqlite3.Connection,
        scope: TelegramAuthScope,
        command: _Command,
        watch: ContactWriteWatch,
        order: int | None,
    ) -> bool:
        state = read_contact_command_state(conn, scope.account_id, command.entity)
        return (
            watch.intake_current()
            and self._identity() == scope
            and state["expected_version"] == command.version
            and state["observation_order"] == order
        )

    @staticmethod
    def _binding(scope: TelegramAuthScope, command: _Command, request: TLRequest | None) -> NativeWriteReceiptBinding:
        if isinstance(request, functions.contacts.AddContactRequest):
            semantic = contact_fingerprint(
                command.action, command.entity, request.first_name, request.last_name, request.phone
            )
        else:
            semantic = contact_fingerprint(command.action, command.entity, "", "", "")
        return NativeWriteReceiptBinding(
            "contact",
            scope.account_id,
            scope.dc_id,
            scope.auth_key_id,
            command.key,
            command.action,
            command.entity,
            semantic,
            1,
        )

    @staticmethod
    def _verification_fingerprint(command: _Command, request: TLRequest | None) -> str:
        if isinstance(request, functions.contacts.AddContactRequest):
            return contact_fingerprint(command.action, command.entity, request.first_name, request.last_name, "")
        return contact_fingerprint(command.action, command.entity, "", "", "")

    @staticmethod
    def _reject_unclaimed(conn: sqlite3.Connection, binding: NativeWriteReceiptBinding) -> None:
        with write_transaction(conn):
            conn.execute(
                "UPDATE contact_operation_receipts SET outcome='rejected',terminal_revision=terminal_revision+1 "
                "WHERE account_id=? AND operation_key=? AND outcome='pending' AND attempt_id IS NULL",
                (binding.account_id, binding.operation_key),
            )

    async def _mutate(  # noqa: PLR0913, PLR0917 - exact physical command context
        self,
        scope: TelegramAuthScope,
        command: _Command,
        watch: ContactWriteWatch,
        request: TLRequest,
        binding: NativeWriteReceiptBinding,
        budget: RpcAttemptBudget,
        preflight_order: int,
    ) -> dict[str, object]:
        async def revalidate() -> Callable[[], bool]:
            if not self._current(
                self._reader,
                scope,
                command,
                watch,
                preflight_order,
            ):
                raise ContactWriteRejected("contact_state_changed")
            return watch.is_current

        with native_write_context(NativeWriteCheck(binding, bytes(request), revalidate)), _phase(budget):
            await self._client(request)
        receipt = self._receipt(command, scope)
        if receipt is None or receipt["outcome"] != "acknowledged":
            await self._offer_reconciliation(scope, command.entity)
            return receipt or {"outcome": "unknown", "verification": "pending", "current_applicability": "unknown"}
        with self._authority.watch(command.entity) as postflight_watch:
            return await self._postflight(scope, command, postflight_watch, request, binding, budget)

    async def _offer_reconciliation(self, scope: TelegramAuthScope, entity_id: int) -> None:
        if self._identity() != scope:
            return
        try:
            await self._reconcile(entity_id)
        except Exception as error:  # noqa: BLE001 - reconcile owner supervises its own failure
            logger.warning("contact_reconciliation_failed error_type=%s", type(error).__name__)

    async def _postflight(  # noqa: PLR0913, PLR0917 - historical observation context
        self,
        scope: TelegramAuthScope,
        command: _Command,
        watch: ContactWriteWatch,
        request: TLRequest,
        binding: NativeWriteReceiptBinding,
        budget: RpcAttemptBudget,
    ) -> dict[str, object]:
        try:
            with _phase(budget):
                observed = await self._acquire(command.entity)
        except (
            ConnectionError,
            TimeoutError,
            TelegramRpcThrottled,
            RpcAdmissionError,
            TelegramRpcMethodRecoveryPending,
            RpcAttemptBudgetExhaustedError,
            RPCError,
            ValueError,
        ) as error:
            logger.warning("contact_readback_failed error_type=%s", type(error).__name__)
            await self._offer_reconciliation(scope, command.entity)
            return {"outcome": "acknowledged", "verification": "unavailable", "current_applicability": "unknown"}
        current = read_contact_command_state(self._reader, scope.account_id, command.entity)
        fresh = (
            observed.accepted
            and observed.observation_order is not None
            and observed.observation_order == current["observation_order"]
            and watch.intake_current()
        )
        verification = "unavailable"
        if fresh and observed.observation_order is not None:
            order = observed.observation_order
            candidate = "verified" if self._matches_readback(command, request, current) else "mismatch"
            verification = await self._publish_verification(scope, command, watch, binding, order, candidate)
        if verification == "unavailable":
            await self._offer_reconciliation(scope, command.entity)
        return {"outcome": "acknowledged", "verification": verification, "current_applicability": "unknown"}

    @staticmethod
    def _matches_readback(command: _Command, request: TLRequest, state: ContactCommandState) -> bool:
        if command.action == "delete":
            return state["membership"] is False
        return (
            isinstance(request, functions.contacts.AddContactRequest)
            and state["membership"] is True
            and state["first_name"] == request.first_name
            and state["last_name"] == request.last_name
        )

    async def _publish_verification(  # noqa: PLR0913, PLR0917 - exact accepted readback evidence
        self,
        scope: TelegramAuthScope,
        command: _Command,
        watch: ContactWriteWatch,
        binding: NativeWriteReceiptBinding,
        order: int,
        candidate: str,
    ) -> str:
        published = await _drain(
            self._writer.run(
                lambda db: receipts.record_verification(
                    db,
                    binding,
                    order,
                    candidate,
                    lambda: (
                        self._identity() == scope
                        and watch.intake_current()
                        and read_contact_command_state(db, scope.account_id, command.entity)["observation_order"]
                        == order
                    ),
                )
            )
        )
        if published:
            return candidate
        prior = self._receipt(command, scope)
        if prior is not None and prior["verification"] in {"verified", "mismatch"}:
            return cast(str, prior["verification"])
        return "unavailable"
