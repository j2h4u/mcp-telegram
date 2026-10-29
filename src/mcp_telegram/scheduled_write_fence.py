"""Narrow in-flight fence for one native scheduled-message mutation."""

from __future__ import annotations

import math
import threading
import time
import weakref
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Literal

from .scheduled_contracts import SCHEDULED_OPERATION_KEY_LIMIT


class ScheduledWriteRejectedError(RuntimeError):
    """Known pre-dispatch refusal; the packet must not reach Telegram."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


ScheduledWriteRejected = ScheduledWriteRejectedError


@dataclass(frozen=True, slots=True)
class ScheduledMutationIntent:
    account_id: int
    dialog_id: int
    action: Literal["create", "update", "cancel"]
    operation_key: str
    request_fingerprint: str
    expected_observation_order: int
    scheduled_message_id: int | None
    schedule_at_utc: float | None
    random_id: int | None

    def __post_init__(self) -> None:
        self._validate_target()
        self._validate_operation()
        self._validate_action_identity()
        self._validate_schedule()

    def _validate_target(self) -> None:
        if type(self.account_id) is not int or self.account_id <= 0:
            raise ValueError("account_id must be a positive integer")
        if type(self.dialog_id) is not int or self.dialog_id == 0:
            raise ValueError("dialog_id must be a nonzero integer")
        if self.action not in {"create", "update", "cancel"}:
            raise ValueError("unsupported scheduled mutation action")

    def _validate_operation(self) -> None:
        if (
            not isinstance(self.operation_key, str)
            or not self.operation_key.strip()
            or len(self.operation_key) > SCHEDULED_OPERATION_KEY_LIMIT
        ):
            raise ValueError("operation_key must contain between 1 and 128 characters")
        if not isinstance(self.request_fingerprint, str) or not self.request_fingerprint:
            raise ValueError("request_fingerprint must be nonempty")
        if type(self.expected_observation_order) is not int or self.expected_observation_order < 0:
            raise ValueError("expected_observation_order must be nonnegative")

    def _validate_action_identity(self) -> None:
        if self.action == "create":
            if self.scheduled_message_id is not None or type(self.random_id) is not int or self.random_id == 0:
                raise ValueError("create requires one random_id and no scheduled message ID")
        elif type(self.scheduled_message_id) is not int or self.scheduled_message_id <= 0 or self.random_id is not None:
            raise ValueError("update/cancel require one scheduled message ID and no random_id")

    def _validate_schedule(self) -> None:
        if self.action == "cancel":
            if self.schedule_at_utc is not None:
                raise ValueError("cancel has no schedule timestamp")
        elif (
            isinstance(self.schedule_at_utc, bool)
            or not isinstance(self.schedule_at_utc, (int, float))
            or not math.isfinite(self.schedule_at_utc)
            or self.schedule_at_utc <= 0
            or int(self.schedule_at_utc) != self.schedule_at_utc
        ):
            raise ValueError("create/update require a positive UTC timestamp at whole-second precision")


@dataclass(slots=True, eq=False, weakref_slot=True)
class ScheduledMutationToken:
    account_id: int
    dialog_id: int
    scheduled_message_id: int | None
    invalidated: threading.Event = field(default_factory=threading.Event, repr=False)

    def is_valid_for(self, intent: ScheduledMutationIntent) -> bool:
        return (
            not self.invalidated.is_set()
            and self.account_id == intent.account_id
            and self.dialog_id == intent.dialog_id
            and self.scheduled_message_id == intent.scheduled_message_id
        )


class ScheduledMutationFence:
    """Weak registry of permits invalidated by accepted canonical queue writes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tokens: dict[tuple[int, int, int | None], weakref.WeakSet[ScheduledMutationToken]] = {}

    def watch(self, account_id: int, dialog_id: int, scheduled_message_id: int | None) -> ScheduledMutationToken:
        token = ScheduledMutationToken(account_id, dialog_id, scheduled_message_id)
        key = (account_id, dialog_id, scheduled_message_id)
        with self._lock:
            self._prune()
            self._tokens.setdefault(key, weakref.WeakSet()).add(token)
        return token

    def invalidate_message(self, account_id: int, dialog_id: int, scheduled_message_id: int) -> None:
        self._invalidate((account_id, dialog_id, scheduled_message_id))

    def invalidate_dialog(self, account_id: int, dialog_id: int) -> None:
        with self._lock:
            keys = [key for key in self._tokens if key[0] == account_id and key[1] == dialog_id]
            for key in keys:
                self._invalidate_locked(key)
            self._prune()

    def invalidate_account(self, account_id: int) -> None:
        with self._lock:
            keys = [key for key in self._tokens if key[0] == account_id]
            for key in keys:
                self._invalidate_locked(key)
            self._prune()

    def _invalidate(self, key: tuple[int, int, int | None]) -> None:
        with self._lock:
            self._invalidate_locked(key)
            self._prune()

    def _invalidate_locked(self, key: tuple[int, int, int | None]) -> None:
        for token in tuple(self._tokens.get(key, ())):
            token.invalidated.set()

    def _prune(self) -> None:
        for key, tokens in tuple(self._tokens.items()):
            if not tokens:
                del self._tokens[key]


@dataclass(frozen=True, slots=True)
class ScheduledMutationCheck:
    intent: ScheduledMutationIntent
    revalidate: Callable[[], Awaitable[ScheduledMutationToken]]


_CHECK_CONTEXT: ContextVar[ScheduledMutationCheck | None] = ContextVar("scheduled_mutation_check", default=None)


def scheduled_admission_deadline(schedule_at_utc: float | None) -> float | None:
    """Translate the original UTC schedule boundary to a monotonic RPC deadline."""
    if schedule_at_utc is None:
        return None
    if (
        isinstance(schedule_at_utc, bool)
        or not isinstance(schedule_at_utc, (int, float))
        or not math.isfinite(schedule_at_utc)
        or schedule_at_utc <= 0
    ):
        raise ValueError("schedule_at_utc must be a finite positive Unix timestamp")
    monotonic_now = time.monotonic()
    return max(monotonic_now, monotonic_now + float(schedule_at_utc) - time.time() - 60.0)


@contextmanager
def scheduled_mutation_context(check: ScheduledMutationCheck) -> Iterator[ScheduledMutationCheck]:
    """Bind one command's immutable intent across the existing Telethon call."""
    if not isinstance(check, ScheduledMutationCheck) or not callable(check.revalidate):
        raise TypeError("check must be a ScheduledMutationCheck")
    reset = _CHECK_CONTEXT.set(check)
    try:
        yield check
    finally:
        _CHECK_CONTEXT.reset(reset)


def current_scheduled_mutation_check() -> ScheduledMutationCheck | None:
    return _CHECK_CONTEXT.get()
