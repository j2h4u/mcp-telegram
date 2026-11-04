"""Gateway authority for account-scoped native contact attempts."""

from __future__ import annotations

import sqlite3
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from telethon.tl import functions, types

from ..auth_scope import TelegramAuthScope
from ..contact_notes.domain import _drain
from ..entity_profile.contracts import UserReference
from ..image_authorization import ScopeToken
from ..native_write_fence import (
    ContactWriteRejected,
    NativeWireOutcome,
    NativeWriteDomainOwner,
    NativeWriteReceiptBinding,
)
from ..sqlite_writer import SQLiteWriter
from . import receipts
from .composition import contact_fingerprint
from .intake import current_contact_marker, intake_marker, is_current, track_contact
from .state import read_contact_command_state


class ContactWriteAuthority:
    """Issue single-use current-auth permits and own durable receipt callbacks."""

    def __init__(
        self,
        writer: SQLiteWriter,
        identity: Callable[[], TelegramAuthScope | None],
        resolve: Callable[[int], UserReference | None],
        *,
        capture_scope: Callable[[], ScopeToken | None],
        claim_scope: Callable[[ScopeToken], bool],
    ) -> None:
        self._writer, self._identity, self._resolve = writer, identity, resolve
        self._capture_scope, self._claim_scope = capture_scope, claim_scope
        self._permits: dict[NativeWriteReceiptBinding, tuple[tuple[int, int], UserReference, ContactWriteWatch]] = {}
        self._reconciliation: Callable[[int], Awaitable[bool]] | None = None
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
        if self._reconciliation is not None:
            raise RuntimeError("contact reconciliation already bound")
        self._reconciliation = callback

    async def notify_terminal(self, binding: NativeWriteReceiptBinding, outcome: NativeWireOutcome) -> None:
        scope = self._identity()
        if outcome not in {"acknowledged", "unknown"} or scope is None or scope.account_id != binding.account_id:
            return
        if self._reconciliation is None:
            raise RuntimeError("contact reconciliation is not bound")
        await self._reconciliation(binding.target)

    @contextmanager
    def watch(self, entity_id: int) -> Iterator[ContactWriteWatch]:
        with track_contact(entity_id):
            watch = ContactWriteWatch(
                self, entity_id, intake_marker(entity_id), self._identity(), self._capture_scope()
            )
            try:
                yield watch
            finally:
                if watch.binding is not None:
                    self._permits.pop(watch.binding, None)

    def _scope_matches(self, binding: NativeWriteReceiptBinding) -> bool:
        scope = self._identity()
        return scope is not None and (scope.account_id, scope.dc_id, scope.auth_key_id) == (
            binding.account_id,
            binding.dc_id,
            binding.key_id,
        )

    async def verify_request(self, binding: NativeWriteReceiptBinding, request: object) -> bool:
        if binding.domain != "contact" or not self._scope_matches(binding):
            return False
        ref = self._resolve(binding.target)
        if ref is None or ref.is_self:
            return False
        peer = types.InputUser(ref.user_id, ref.access_hash)
        if isinstance(request, functions.contacts.AddContactRequest):
            return self._matches_add(binding, request, peer)
        if isinstance(request, functions.contacts.DeleteContactsRequest):
            return (
                binding.action == "delete"
                and request.id == [peer]
                and contact_fingerprint("delete", binding.target, "", "", "") == binding.semantic_fingerprint
            )
        return False

    @staticmethod
    def _matches_add(
        binding: NativeWriteReceiptBinding, request: functions.contacts.AddContactRequest, peer: types.InputUser
    ) -> bool:
        return (
            binding.action in {"create", "update"}
            and request.id == peer
            and request.note is None
            and not request.add_phone_privacy_exception
            and contact_fingerprint(
                binding.action, binding.target, request.first_name, request.last_name, request.phone
            )
            == binding.semantic_fingerprint
        )

    async def bind_attempt(self, binding: NativeWriteReceiptBinding, attempt: str) -> None:
        await _drain(self._writer.run(lambda db: receipts.bind_attempt(db, binding, attempt)))

    async def record_wire_outcome(
        self, binding: NativeWriteReceiptBinding, attempt: str, outcome: NativeWireOutcome
    ) -> None:
        await _drain(self._writer.run(lambda db: receipts.record_wire_outcome(db, binding, attempt, outcome)))

    async def validate_binding(self, binding: NativeWriteReceiptBinding, attempt: str) -> bool:
        return await _drain(self._writer.run(lambda db: receipts.validate_binding(db, binding, attempt)))

    async def recover_binding(self, binding: NativeWriteReceiptBinding, attempt: str) -> bool:
        return await _drain(self._writer.run(lambda db: receipts.recover_binding(db, binding, attempt)))

    async def preclaim(self, binding: NativeWriteReceiptBinding) -> Callable[[], bool]:
        permit = self._permits.get(binding)
        if permit is None:
            raise ContactWriteRejected("contact_permit_unavailable")
        marker, reference, watch = permit

        def inspect(db: sqlite3.Connection) -> bool:
            version = receipts.pending_expected_version(db, binding)
            state = read_contact_command_state(db, binding.account_id, binding.target)
            return (
                version is not None
                and state["expected_version"] == version
                and state["observation_order"] == watch.observation_order
                and watch.intake_current()
            )

        if not await _drain(self._writer.run(inspect)):
            raise ContactWriteRejected("contact_state_changed")
        return lambda: (
            self._permits.get(binding) == permit
            and watch.intake_current()
            and self._scope_matches(binding)
            and is_current(binding.target, marker)
            and self._resolve(binding.target) == reference
        )


@dataclass(slots=True)
class ContactWriteWatch:
    authority: ContactWriteAuthority
    entity_id: int
    intake: tuple[int, int] | None
    scope: TelegramAuthScope | None
    token: ScopeToken | None
    binding: NativeWriteReceiptBinding | None = None
    observation_order: int | None = None

    def intake_current(self) -> bool:
        before = intake_marker(self.entity_id)
        claimed = self.token is not None and self.authority._claim_scope(self.token)
        after = intake_marker(self.entity_id)
        return bool(
            self.scope is not None
            and self.token is not None
            and self.token.scope == self.scope
            and self.authority._identity() == self.scope
            and self.intake is not None
            and before == self.intake == after
            and claimed
        )

    def arm(self, binding: NativeWriteReceiptBinding, observation_order: int) -> None:
        marker = current_contact_marker(self.entity_id)
        ref = self.authority._resolve(self.entity_id)
        if (
            self.binding is not None
            or binding.domain != "contact"
            or binding.target != self.entity_id
            or binding.target == binding.account_id
            or not self.authority._scope_matches(binding)
        ):
            raise ContactWriteRejected("contact_permit_unavailable")
        if not self.intake_current() or marker is None or ref is None or ref.is_self:
            raise ContactWriteRejected("contact_permit_unavailable")
        self.authority._permits[binding] = marker, ref, self
        self.binding = binding
        self.observation_order = observation_order

    def is_current(self) -> bool:
        if self.binding is None or not self.intake_current():
            return False
        permit = self.authority._permits.get(self.binding)
        return bool(
            permit
            and permit[2] is self
            and is_current(self.entity_id, permit[0])
            and self.authority._resolve(self.entity_id) == permit[1]
        )
