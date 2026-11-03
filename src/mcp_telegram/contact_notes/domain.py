"""Authoritative gateway callbacks for contact-note receipt ownership."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from telethon.tl import types
from telethon.tl.functions.contacts import UpdateContactNoteRequest

from ..auth_scope import TelegramAuthScope
from ..entity_profile.contracts import UserReference
from ..image_authorization import ScopeToken
from ..native_write_fence import (
    NativeWireOutcome,
    NativeWriteDomainOwner,
    NativeWriteReceiptBinding,
    NoteWriteRejected,
)
from ..sqlite_writer import SQLiteWriter
from . import receipts
from .intake import (
    current_note_marker,
    intake_marker,
    is_current,
    track_contact_note,
    validate_note_wire_request,
)
from .state import read_note_command_state


class NoteWriteAuthority:
    """Own ephemeral permits and durable callbacks for one gateway."""

    def __init__(
        self,
        writer: SQLiteWriter,
        identity: Callable[[], TelegramAuthScope | None],
        resolve_reference: Callable[[int], UserReference | None],
        *,
        capture_scope: Callable[[], ScopeToken | None] | None = None,
        claim_scope: Callable[[ScopeToken], bool] | None = None,
    ) -> None:
        self._writer = writer
        self._identity = identity
        self._resolve = resolve_reference
        self._capture_scope = capture_scope
        self._claim_scope = claim_scope
        self._reconciliation: Callable[[int], Awaitable[bool]] | None = None
        self._permits: dict[NativeWriteReceiptBinding, tuple[tuple[int, int], UserReference, NoteWriteWatch]] = {}
        self.domain = NativeWriteDomainOwner(
            self.verify_request,
            self.bind_attempt,
            self.record_wire_outcome,
            self.validate_binding,
            self.recover_binding,
            self.preclaim,
            self.notify_terminal,
        )

    def bind_reconciliation(self, callback: Callable[[int], Awaitable[bool]]) -> None:
        """Attach the existing bounded profile owner before command admission."""
        if self._reconciliation is not None:
            raise RuntimeError("note reconciliation already bound")
        self._reconciliation = callback

    async def notify_terminal(self, binding: NativeWriteReceiptBinding, outcome: NativeWireOutcome) -> None:
        """Offer late settled evidence to the existing profile owner, without RPC."""
        scope = self._identity()
        callback = self._reconciliation
        if outcome not in {"acknowledged", "unknown"} or scope is None or scope.account_id != binding.account_id:
            return
        if callback is None:
            raise RuntimeError("note reconciliation is not bound")
        await callback(binding.target)

    @contextmanager
    def watch(self, entity_id: int) -> Iterator[NoteWriteWatch]:
        """Track delivered intake before the command's preflight begins."""
        with track_contact_note(entity_id):
            scope_token = self._capture_scope() if self._capture_scope is not None else None
            watch = NoteWriteWatch(self, entity_id, intake_marker(entity_id), self._identity(), scope_token)
            try:
                yield watch
            finally:
                if watch.binding is not None:
                    self._permits.pop(watch.binding, None)

    async def verify_request(self, binding: NativeWriteReceiptBinding, request: object) -> bool:
        if not self._scope_matches(binding):
            return False
        if not validate_note_wire_request(binding, request) or not isinstance(request, UpdateContactNoteRequest):
            return False
        reference = self._resolve(binding.target)
        if reference is None or reference.is_self:
            return False
        return request.id == types.InputUser(reference.user_id, reference.access_hash)

    def _scope_matches(self, binding: NativeWriteReceiptBinding) -> bool:
        scope = self._identity()
        return scope is not None and (scope.account_id, scope.dc_id, scope.auth_key_id) == (
            binding.account_id,
            binding.dc_id,
            binding.key_id,
        )

    async def bind_attempt(self, binding: NativeWriteReceiptBinding, attempt: str) -> None:
        await _drain(self._writer.run(lambda conn: receipts.bind_attempt(conn, binding, attempt)))

    async def record_wire_outcome(
        self, binding: NativeWriteReceiptBinding, attempt: str, outcome: NativeWireOutcome
    ) -> None:
        await _drain(self._writer.run(lambda conn: receipts.record_wire_outcome(conn, binding, attempt, outcome)))

    async def validate_binding(self, binding: NativeWriteReceiptBinding, attempt: str) -> bool:
        return await _drain(self._writer.run(lambda conn: receipts.validate_binding(conn, binding, attempt)))

    async def recover_binding(self, binding: NativeWriteReceiptBinding, attempt: str) -> bool:
        return await _drain(self._writer.run(lambda conn: receipts.recover_binding(conn, binding, attempt)))

    async def preclaim(self, binding: NativeWriteReceiptBinding) -> Callable[[], bool]:
        permit = self._permits.get(binding)
        if permit is None or not self._scope_matches(binding):
            raise NoteWriteRejected("note_permit_unavailable")
        marker, reference, watch = permit

        def validate(conn: object) -> bool:
            import sqlite3

            if not isinstance(conn, sqlite3.Connection) or not self._scope_matches(binding):
                return False
            expected = receipts.pending_expected_version(conn, binding)
            state = read_note_command_state(conn, binding.account_id, binding.target)
            return expected is not None and state["expected_version"] == expected and state["contact"] is True

        if not await self._writer.run(validate):
            raise NoteWriteRejected("note_state_changed")
        return lambda: (
            self._permits.get(binding) == permit
            and watch.intake_current()
            and self._scope_matches(binding)
            and is_current(binding.target, marker)
            and self._resolve(binding.target) == reference
        )


@dataclass(slots=True)
class NoteWriteWatch:
    authority: NoteWriteAuthority
    entity_id: int
    intake: tuple[int, int] | None
    scope: TelegramAuthScope | None
    scope_token: ScopeToken | None
    binding: NativeWriteReceiptBinding | None = None

    def intake_current(self) -> bool:
        claim = self.authority._claim_scope
        token = self.scope_token
        before = intake_marker(self.entity_id)
        claimed = claim(token) if claim is not None and token is not None else False
        after = intake_marker(self.entity_id)
        return bool(
            self.scope is not None
            and token is not None
            and token.scope == self.scope
            and self.authority._identity() == self.scope
            and self.intake is not None
            and before == self.intake == after
            and claimed
        )

    def is_current(self) -> bool:
        if not self.intake_current() or self.binding is None:
            return False
        permit = self.authority._permits.get(self.binding)
        return bool(
            permit
            and permit[2] is self
            and is_current(self.entity_id, permit[0])
            and self.authority._resolve(self.entity_id) == permit[1]
        )

    def arm(self, binding: NativeWriteReceiptBinding) -> None:
        if (
            self.binding is not None
            or binding.domain != "contact_note"
            or binding.target != self.entity_id
            or binding.target == binding.account_id
            or not self.authority._scope_matches(binding)
        ):
            raise NoteWriteRejected("note_permit_unavailable")
        marker = current_note_marker(self.entity_id)
        reference = self.authority._resolve(self.entity_id)
        if not self.intake_current() or marker is None:
            raise NoteWriteRejected("note_intake_changed")
        if reference is None or reference.is_self:
            raise NoteWriteRejected("contact_unavailable")
        self.authority._permits[binding] = (marker, reference, self)
        self.binding = binding


async def _drain[T](operation: Awaitable[T]) -> T:
    task = asyncio.ensure_future(operation)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:  # noqa: BLE001 - inspect the settled writer task below
            break
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result
