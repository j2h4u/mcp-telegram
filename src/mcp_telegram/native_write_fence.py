"""Ephemeral admission evidence for the two native management writes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal

type NativeWriteDomain = Literal["folder", "contact_note", "contact"]
type NativeWireOutcome = Literal["acknowledged", "rejected", "unknown"]


class FolderWriteRejectedError(RuntimeError):
    """A folder packet was refused before its physical claim."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class NoteWriteRejectedError(RuntimeError):
    """A contact-note packet was refused before its physical claim."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ContactWriteRejectedError(RuntimeError):
    """A contact packet was refused before its physical claim."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


FolderWriteRejected = FolderWriteRejectedError
NoteWriteRejected = NoteWriteRejectedError
ContactWriteRejected = ContactWriteRejectedError


@dataclass(frozen=True, slots=True)
class NativeWriteReceiptBinding:
    domain: NativeWriteDomain
    account_id: int
    dc_id: int
    key_id: int
    operation_key: str
    action: str
    target: int
    semantic_fingerprint: str
    binding_revision: int

    def __post_init__(self) -> None:
        if self.domain not in {"folder", "contact_note", "contact"}:
            raise ValueError("invalid native write domain")
        self._validate_identity()
        self._validate_fields()

    def _validate_identity(self) -> None:
        identity = (self.account_id, self.dc_id, self.key_id, self.target, self.binding_revision)
        if any(type(value) is not int for value in identity):
            raise ValueError("native write identity must contain integers")
        if min(self.account_id, self.dc_id, self.target, self.binding_revision) <= 0:
            raise ValueError("native write identity must be positive")

    def _validate_fields(self) -> None:
        fields = (self.operation_key, self.action, self.semantic_fingerprint)
        if any(not isinstance(value, str) or not value for value in fields):
            raise ValueError("native write binding fields must be nonempty")


@dataclass(frozen=True, slots=True)
class NativeWriteCheck:
    binding: NativeWriteReceiptBinding
    body: bytes
    revalidate: Callable[[], Awaitable[Callable[[], bool]]]


@dataclass(frozen=True, slots=True)
class NativeWriteDomainOwner:
    verify_request: Callable[[NativeWriteReceiptBinding, object], Awaitable[bool]]
    bind_attempt: Callable[[NativeWriteReceiptBinding, str], Awaitable[None]]
    record_wire_outcome: Callable[[NativeWriteReceiptBinding, str, NativeWireOutcome], Awaitable[None]]
    validate_binding: Callable[[NativeWriteReceiptBinding, str], Awaitable[bool]]
    recover_binding: Callable[[NativeWriteReceiptBinding, str], Awaitable[bool]]
    preclaim: Callable[[NativeWriteReceiptBinding], Awaitable[Callable[[], bool]]]
    notify_terminal: Callable[[NativeWriteReceiptBinding, NativeWireOutcome], Awaitable[None]] | None = None


_CHECK: ContextVar[NativeWriteCheck | None] = ContextVar("native_write_check", default=None)


@contextmanager
def native_write_context(check: NativeWriteCheck) -> Iterator[NativeWriteCheck]:
    token = _CHECK.set(check)
    try:
        yield check
    finally:
        _CHECK.reset(token)


def current_native_write_check() -> NativeWriteCheck | None:
    return _CHECK.get()
