"""Bounded staging for decoded Telegram notification envelopes."""

from __future__ import annotations

import base64
import json
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from telethon.tl.tlobject import TLObject

from .telegram_update_store import UpdateRecord

MAX_RETAINED_BYTES = 64 * 1024 * 1024
MAX_ENTRY_BYTES = 4 * 1024 * 1024
MAX_ENTRIES = 65_536
MAX_TRACE_IDS = 2


def _json_value(value: object) -> str:
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"unsupported Telegram JSON value: {type(value).__name__}")


@dataclass(slots=True)
class _Entry:
    sequence: int
    observed_at_ms: int
    source: str
    constructor: str
    payload_json: str
    payload_bytes: int
    trace_ids: list[str] = field(default_factory=list)


class TelegramUpdateRing:
    """Stage complete decoded envelopes without delaying update admission."""

    def __init__(self) -> None:
        self.runtime_id = uuid.uuid4().hex
        self._account_id: int | None = None
        self._entries: deque[_Entry] = deque()
        self._sequence = 0
        self._bytes = 0
        self._inflight_through = 0
        self._inflight_bytes = 0
        self._orphaned_bytes = 0
        self.generation: int | None = None
        self.request_revision = 0
        self.desired_account: int | None = None
        self.logout_serial = 0
        self.completed_logout_serial = 0
        self._counters = {
            "evicted_capacity": 0,
            "rejected_oversize": 0,
            "serialization_failures": 0,
            "reset_evicted": 0,
        }

    def request_verified_account(self, account_id: int) -> None:
        if self.desired_account == account_id:
            return
        self.generation = None
        if self._account_id is not None and self._account_id != account_id:
            self.reset("account_changed")
        self.desired_account = account_id
        self.request_revision += 1

    def request_logout(self) -> None:
        self.generation = None
        self.reset("logout")
        self.desired_account = None
        self.request_revision += 1
        self.logout_serial += 1

    def complete_bind(self, account_id: int, revision: int, generation: int) -> None:
        if revision == self.request_revision and self.completed_logout_serial == self.logout_serial:
            self._account_id = account_id
            self.generation = generation

    def reset(self, reason: str) -> None:
        del reason
        self._counters["reset_evicted"] += len(self._entries)
        self._entries.clear()
        self._bytes = 0
        self._orphaned_bytes = self._inflight_bytes
        self._inflight_through = 0
        self._account_id = None

    def _drop_oldest(self, counter: str) -> None:
        self._bytes -= self._entries.popleft().payload_bytes
        self._counters[counter] += 1

    def capture(self, item: object, *, source: str) -> int:
        """Capture a complete TL value; failures are counted and isolated."""
        self._sequence += 1
        try:
            if not isinstance(item, TLObject):
                raise TypeError("not a Telegram TL object")
            payload = json.dumps(item.to_dict(), default=_json_value, ensure_ascii=False, separators=(",", ":"))
            size = len(payload.encode("utf-8"))
        except Exception:  # noqa: BLE001 - diagnostics must never reject an update
            self._counters["serialization_failures"] += 1
            return self._sequence
        if size > MAX_ENTRY_BYTES:
            self._counters["rejected_oversize"] += 1
            return self._sequence
        if not self._make_room(size):
            return self._sequence
        self._entries.append(
            _Entry(self._sequence, time.time_ns() // 1_000_000, source, type(item).__name__, payload, size)
        )
        self._bytes += size
        return self._sequence

    def _make_room(self, size: int) -> bool:
        while self._entries and (
            self._bytes + self._orphaned_bytes + size > MAX_RETAINED_BYTES or len(self._entries) >= MAX_ENTRIES
        ):
            if self._entries[0].sequence <= self._inflight_through:
                self._counters["evicted_capacity"] += 1
                return False
            self._drop_oldest("evicted_capacity")
        if self._bytes + self._orphaned_bytes + size > MAX_RETAINED_BYTES:
            self._counters["evicted_capacity"] += 1
            return False
        return True

    def associate_trace(self, sequence: int, trace_id: str) -> None:
        if self._entries and self._entries[-1].sequence == sequence:
            traces = self._entries[-1].trace_ids
            if trace_id not in traces and len(traces) < MAX_TRACE_IDS:
                traces.append(trace_id)

    def peek_batch(self, *, max_count: int, max_bytes: int) -> tuple[UpdateRecord, ...]:
        records: list[UpdateRecord] = []
        total = 0
        for entry in self._entries:
            if len(records) >= max_count or total + entry.payload_bytes > max_bytes:
                break
            records.append(
                UpdateRecord(
                    self.runtime_id,
                    entry.sequence,
                    entry.observed_at_ms,
                    entry.source,
                    entry.constructor,
                    entry.payload_json,
                    entry.payload_bytes,
                    json.dumps(entry.trace_ids),
                )
            )
            total += entry.payload_bytes
        if records:
            self._inflight_through = records[-1].sequence
            self._inflight_bytes = total
        return tuple(records)

    def ack_batch(self, records: tuple[UpdateRecord, ...]) -> None:
        if not records:
            return
        through = records[-1].sequence
        while self._entries and self._entries[0].sequence <= through:
            self._bytes -= self._entries.popleft().payload_bytes
        self._inflight_through = 0
        self._inflight_bytes = 0
        self._orphaned_bytes = 0

    def release_batch(self) -> None:
        self._inflight_through = 0
        self._inflight_bytes = 0
        self._orphaned_bytes = 0

    def discard_batch(self, records: tuple[UpdateRecord, ...]) -> None:
        before = len(self._entries)
        self.ack_batch(records)
        self._counters["reset_evicted"] += before - len(self._entries)

    def counters(self) -> dict[str, int]:
        return {**self._counters, "pending_records": len(self._entries), "pending_bytes": self._bytes}
