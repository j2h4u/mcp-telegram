"""Bounded structured runtime observations stored in sync.db."""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import sqlite3
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Protocol, cast

from .sqlite_writer import SQLiteWriter
from .sync_transactions import write_transaction

RUNTIME_INSTANCE_ID = uuid.uuid4().hex
MAX_PAYLOAD_BYTES = 1024
DEFAULT_ROW_CAP = 100_000
_PRUNE_BATCH_SIZE = 1_000
_WRITER_POLL_SECONDS = 0.05
_FLUSH_BATCH_SIZE = 32
ALLOWED_KINDS = frozenset(
    {
        "mcp.call",
        "telegram.inbox_read_received",
        "sync.inbox_read_finished",
        "sync.read_reconciliation",
        "runtime.started",
        "runtime.stopped",
        "runtime.connection_observed",
        "runtime.catch_up_requested",
        "runtime.catch_up_request_failed",
        "runtime.task_failed",
        "telegram.rpc_admission",
        "telegram.rpc_request",
        "telegram.demand",
        "daemon.request_timing",
        "entity_profile.pair",
        "sync.delta_gap_fill",
        "message_fact.read_at",
        "reaction.aggregate",
        "reaction.detail",
        "reaction.invalidation",
        "runtime.telemetry_loss",
        "draft.observed",
        "draft.recovery",
    }
)

logger = logging.getLogger(__name__)


class RuntimeObservationPolicy(Protocol):
    """Operator-owned limits required by the asynchronous observation sink."""

    @property
    def prune_every_writes(self) -> int: ...

    @property
    def writer_busy_timeout_ms(self) -> int: ...

    @property
    def queue_capacity(self) -> int: ...

    @property
    def writer_startup_wait_seconds(self) -> float: ...

    @property
    def shutdown_drain_grace_seconds(self) -> float: ...

    @property
    def row_cap(self) -> int: ...

    @property
    def rpc_summary_interval_seconds(self) -> float: ...


def tool_telemetry_identity(tool_name: str) -> tuple[str, int]:
    """Return the stable product capability and wire-contract generation."""
    if tool_name in {"get_sync_alerts", "list_important_events"}:
        return "conversation_changes", 0
    if tool_name == "list_conversation_changes":
        return "conversation_changes", 1
    return tool_name, 1


def encode_payload(payload: Mapping[str, object] | None) -> str:
    encoded = json.dumps(dict(payload or {}), ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    if len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ValueError("runtime event payload exceeds 1024 bytes")
    return encoded


def _normalize_tool_identity(
    kind: str,
    tool_name: str | None,
    tool_capability: str | None,
    contract_version: int | None,
) -> tuple[str | None, int | None]:
    if kind == "mcp.call" and tool_name is not None:
        default_capability, default_contract = tool_telemetry_identity(tool_name)
        return (
            default_capability if tool_capability is None else tool_capability,
            default_contract if contract_version is None else contract_version,
        )
    return tool_capability, contract_version


def record_runtime_observation(  # noqa: PLR0913
    conn: sqlite3.Connection,
    *,
    kind: str,
    dialog_id: int | None = None,
    operation_id: str | None = None,
    outcome: str | None = None,
    reason_code: str | None = None,
    duration_ms: float | None = None,
    tool_name: str | None = None,
    tool_capability: str | None = None,
    contract_version: int | None = None,
    result_count: int | None = None,
    has_cursor: bool | None = None,
    page_depth: int | None = None,
    has_filter: bool | None = None,
    error_type: str | None = None,
    payload: Mapping[str, object] | None = None,
    observed_at_ms: int | None = None,
) -> int:
    """Append one allowlisted observation without committing the caller's transaction."""
    if kind not in ALLOWED_KINDS:
        raise ValueError(f"unsupported runtime event kind: {kind}")
    if kind == "mcp.call" and tool_name is not None:
        default_capability, default_contract = tool_telemetry_identity(tool_name)
        tool_capability = default_capability if tool_capability is None else tool_capability
        contract_version = default_contract if contract_version is None else contract_version
    cursor = conn.execute(
        """INSERT INTO runtime_observations(
               observed_at_ms, kind, runtime_instance_id, operation_id, outcome,
               reason_code, dialog_id, duration_ms, tool_name, tool_capability, contract_version, result_count,
               has_cursor, page_depth, has_filter, error_type, payload_json
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            int(time.time() * 1000) if observed_at_ms is None else observed_at_ms,
            kind,
            RUNTIME_INSTANCE_ID,
            operation_id,
            outcome,
            reason_code,
            dialog_id,
            duration_ms,
            tool_name,
            tool_capability,
            contract_version,
            result_count,
            None if has_cursor is None else int(has_cursor),
            page_depth,
            None if has_filter is None else int(has_filter),
            error_type,
            encode_payload(payload),
        ),
    )
    if cursor.lastrowid is None:
        raise RuntimeError("runtime event insert did not return an id")
    return cursor.lastrowid


@dataclass(frozen=True, slots=True)
class _ObservationJob:
    """Immutable, content-free work item passed from a callback to the writer."""

    observed_at_ms: int
    kind: str
    dialog_id: int | None
    operation_id: str | None
    outcome: str | None
    reason_code: str | None
    duration_ms: float | None
    tool_name: str | None
    tool_capability: str | None
    contract_version: int | None
    result_count: int | None
    has_cursor: bool | None
    page_depth: int | None
    has_filter: bool | None
    error_type: str | None
    payload_json: str


@dataclass(frozen=True, slots=True)
class RuntimeObservationCounters:
    """Aggregate loss and writer counters, safe to inspect from any thread."""

    queue_full_drops: int = 0
    shutdown_grace_drops: int = 0
    busy_retries: int = 0
    permanent_failures: int = 0
    successful_writes: int = 0
    startup_failures: int = 0
    rejected_submissions: int = 0
    startup_drops: int = 0


@dataclass(frozen=True, slots=True)
class _PruneBatchResult:
    deleted: int
    cap_deleted: int
    has_more: bool


@dataclass(frozen=True, slots=True)
class _PrunePlan:
    ids: tuple[int, ...]
    cap: bool
    has_more: bool


class RuntimeObservationSink:
    """Accept bounded observations without doing SQLite work in callbacks."""

    def __init__(
        self,
        database: Path | str | sqlite3.Connection,
        *,
        writer: SQLiteWriter,
        retention_ttl_seconds: int,
        policy: RuntimeObservationPolicy,
    ) -> None:
        positive_values = (
            retention_ttl_seconds,
            policy.prune_every_writes,
            policy.writer_busy_timeout_ms,
            policy.queue_capacity,
            policy.writer_startup_wait_seconds,
            policy.shutdown_drain_grace_seconds,
            policy.row_cap,
        )
        if any(value <= 0 for value in positive_values):
            raise ValueError("runtime observation sink limits must be positive")
        self._database_path(database)
        self._writer = writer
        self._retention_ttl_seconds = retention_ttl_seconds
        self._prune_every_writes = policy.prune_every_writes
        self._shutdown_drain_grace_seconds = policy.shutdown_drain_grace_seconds
        self._row_cap = policy.row_cap
        self._jobs: queue.Queue[_ObservationJob] = queue.Queue(maxsize=policy.queue_capacity)
        self._close_requested = threading.Event()
        self._state_lock = threading.Lock()
        self._counter_lock = threading.Lock()
        self._log_lock = threading.Lock()
        self._last_log_at = 0.0
        self._last_logged_summary = self._summary_snapshot(RuntimeObservationCounters())
        self._accepting = True
        self._closed = False
        self._writer_error: BaseException | None = None
        self._counters = RuntimeObservationCounters()
        self._prune_pending = False
        self._prune_now_ms: int | None = None
        self._external_writes_since_prune = 0
        self._maintenance_requested = threading.Event()
        self._flush_task = asyncio.create_task(self._flush_loop(), name="runtime-observation-flush")

    @staticmethod
    def _database_path(database: Path | str | sqlite3.Connection) -> Path:
        if isinstance(database, sqlite3.Connection):
            rows = cast(list[tuple[object, ...]], database.execute("PRAGMA database_list").fetchall())
            main_path = next((str(row[2]) for row in rows if row[1] == "main"), "")
            if not main_path or main_path == ":memory:":
                raise ValueError("runtime observation sink requires a file-backed SQLite database")
            return Path(main_path)
        return Path(database)

    def record(  # noqa: PLR0913
        self,
        *,
        kind: str,
        dialog_id: int | None = None,
        operation_id: str | None = None,
        outcome: str | None = None,
        reason_code: str | None = None,
        duration_ms: float | None = None,
        tool_name: str | None = None,
        tool_capability: str | None = None,
        contract_version: int | None = None,
        result_count: int | None = None,
        has_cursor: bool | None = None,
        page_depth: int | None = None,
        has_filter: bool | None = None,
        error_type: str | None = None,
        payload: Mapping[str, object] | None = None,
        observed_at_ms: int | None = None,
    ) -> bool:
        """Enqueue one observation without waiting; return whether the queue accepted it."""
        if kind not in ALLOWED_KINDS:
            raise ValueError(f"unsupported runtime event kind: {kind}")
        tool_capability, contract_version = _normalize_tool_identity(
            kind,
            tool_name,
            tool_capability,
            contract_version,
        )
        job = _ObservationJob(
            observed_at_ms=int(time.time() * 1000) if observed_at_ms is None else observed_at_ms,
            kind=kind,
            dialog_id=dialog_id,
            operation_id=operation_id,
            outcome=outcome,
            reason_code=reason_code,
            duration_ms=duration_ms,
            tool_name=tool_name,
            tool_capability=tool_capability,
            contract_version=contract_version,
            result_count=result_count,
            has_cursor=has_cursor,
            page_depth=page_depth,
            has_filter=has_filter,
            error_type=error_type,
            payload_json=encode_payload(payload),
        )
        with self._state_lock:
            if self._writer_error is not None:
                self._increment("rejected_submissions")
                return False
            if self._closed or not self._accepting:
                self._increment("rejected_submissions")
                return False
            try:
                self._jobs.put_nowait(job)
            except queue.Full:
                self._increment("queue_full_drops")
                return False
        return True

    def note_external_write(self) -> None:
        """Request maintenance after committed observations written elsewhere."""
        with self._state_lock:
            if self._closed or not self._accepting or self._writer_error is not None:
                return
            self._external_writes_since_prune += 1
            if self._external_writes_since_prune >= self._prune_every_writes:
                self._external_writes_since_prune = 0
                self._maintenance_requested.set()

    def close(self) -> None:
        """Stop admission immediately; async callers must await aclose to drain."""
        with self._state_lock:
            self._accepting = False
            self._close_requested.set()

    async def aclose(self) -> None:
        """Drain briefly, discard unsubmitted jobs, then settle accepted owner work.

        The grace bounds buffered work, not an operation already accepted by
        the shared owner: SQLite and OS stalls cannot safely be interrupted here.
        """
        self.close()
        cancellation: asyncio.CancelledError | None = None
        try:
            _, pending = await asyncio.wait({self._flush_task}, timeout=self._shutdown_drain_grace_seconds)
        except asyncio.CancelledError as exc:
            cancellation = exc
            pending = {self._flush_task}
        if pending:
            self._drop_queued_jobs("shutdown_grace_drops")
        while not self._flush_task.done():
            try:
                await asyncio.shield(self._flush_task)
            except asyncio.CancelledError as exc:
                cancellation = exc
                self._drop_queued_jobs("shutdown_grace_drops")
        self._flush_task.result()
        self._closed = True
        self._maybe_log_summary()
        if cancellation is not None:
            raise cancellation

    @property
    def stats(self) -> RuntimeObservationCounters:
        with self._counter_lock:
            return self._counters

    @property
    def counters(self) -> RuntimeObservationCounters:
        return self.stats

    @property
    def queue_full_drops(self) -> int:
        return self.stats.queue_full_drops

    @property
    def shutdown_grace_drops(self) -> int:
        return self.stats.shutdown_grace_drops

    @property
    def busy_retries(self) -> int:
        return self.stats.busy_retries

    @property
    def permanent_failures(self) -> int:
        return self.stats.permanent_failures

    @property
    def startup_failures(self) -> int:
        return self.stats.startup_failures

    @property
    def rejected_submissions(self) -> int:
        return self.stats.rejected_submissions

    @property
    def startup_drops(self) -> int:
        return self.stats.startup_drops

    @property
    def writer_error(self) -> str | None:
        with self._state_lock:
            return None if self._writer_error is None else str(self._writer_error)

    def _increment(self, name: str, amount: int = 1) -> None:
        with self._counter_lock:
            current = self._counters
            self._counters = RuntimeObservationCounters(
                queue_full_drops=current.queue_full_drops + amount * (name == "queue_full_drops"),
                shutdown_grace_drops=current.shutdown_grace_drops + amount * (name == "shutdown_grace_drops"),
                busy_retries=current.busy_retries + amount * (name == "busy_retries"),
                permanent_failures=current.permanent_failures + amount * (name == "permanent_failures"),
                successful_writes=current.successful_writes + amount * (name == "successful_writes"),
                startup_failures=current.startup_failures + amount * (name == "startup_failures"),
                rejected_submissions=current.rejected_submissions + amount * (name == "rejected_submissions"),
                startup_drops=current.startup_drops + amount * (name == "startup_drops"),
            )

    def _maybe_log_summary(self) -> None:
        now = time.monotonic()
        with self._log_lock:
            if now - self._last_log_at < 1.0:
                return
            counters = self.stats
            if not any(
                (
                    counters.queue_full_drops,
                    counters.shutdown_grace_drops,
                    counters.busy_retries,
                    counters.permanent_failures,
                    counters.startup_failures,
                    counters.rejected_submissions,
                    counters.startup_drops,
                )
            ):
                return
            summary = self._summary_snapshot(counters)
            if summary == self._last_logged_summary:
                return
            self._last_log_at = now
            self._last_logged_summary = summary
        level = (
            logging.WARNING
            if any(
                (
                    counters.queue_full_drops,
                    counters.shutdown_grace_drops,
                    counters.permanent_failures,
                    counters.startup_failures,
                    counters.rejected_submissions,
                    counters.startup_drops,
                )
            )
            else logging.DEBUG
        )
        logger.log(
            level,
            "runtime_observation_sink_summary queue_full_drops=%d shutdown_grace_drops=%d "
            "busy_retries=%d permanent_failures=%d startup_failures=%d rejected_submissions=%d startup_drops=%d",
            counters.queue_full_drops,
            counters.shutdown_grace_drops,
            counters.busy_retries,
            counters.permanent_failures,
            counters.startup_failures,
            counters.rejected_submissions,
            counters.startup_drops,
        )

    @staticmethod
    def _summary_snapshot(counters: RuntimeObservationCounters) -> tuple[int, ...]:
        return (
            counters.queue_full_drops,
            counters.shutdown_grace_drops,
            counters.busy_retries,
            counters.permanent_failures,
            counters.startup_failures,
            counters.rejected_submissions,
            counters.startup_drops,
        )

    def _drop_queued_jobs(self, counter_name: str = "startup_drops") -> None:
        dropped = 0
        while True:
            try:
                self._jobs.get_nowait()
            except queue.Empty:
                break
            else:
                dropped += 1
                self._jobs.task_done()
        if dropped:
            self._increment(counter_name, dropped)

    async def _flush_loop(self) -> None:
        writes_since_prune = 0
        while True:
            self._consume_external_write_request()
            jobs: list[_ObservationJob] = []
            while len(jobs) < _FLUSH_BATCH_SIZE:
                try:
                    jobs.append(self._jobs.get_nowait())
                except queue.Empty:
                    break
            if jobs:
                writes_since_prune = await self._flush_batch(jobs, writes_since_prune)
            if self._close_requested.is_set() and self._jobs.empty():
                return
            if self._prune_pending and not self._close_requested.is_set():
                await self._run_prune_batch()
            self._maybe_log_summary()
            # Yield between bounded batches so core submissions reach the owner.
            await asyncio.sleep(0 if jobs else _WRITER_POLL_SECONDS)

    async def _flush_batch(self, jobs: list[_ObservationJob], writes_since_prune: int) -> int:
        try:
            await self._writer.run(partial(self._write_batch, jobs=jobs))
        except Exception as exc:  # noqa: BLE001 - diagnostics must never stop archive work
            self._increment("permanent_failures", len(jobs))
            logger.warning("runtime_observation_batch_failed error_type=%s", type(exc).__name__)
        else:
            self._increment("successful_writes", len(jobs))
            writes_since_prune += len(jobs)
            if writes_since_prune >= self._prune_every_writes:
                writes_since_prune = 0
                self._prune_pending = True
        finally:
            for _ in jobs:
                self._jobs.task_done()
        return writes_since_prune

    def _consume_external_write_request(self) -> None:
        with self._state_lock:
            if not self._maintenance_requested.is_set():
                return
            self._maintenance_requested.clear()
        self._prune_pending = True

    async def _run_prune_batch(self) -> None:
        effective_now = self._prune_now_ms or int(time.time() * 1000)
        self._prune_now_ms = effective_now
        try:
            # Planning holds no writer reservation; deletion is a separate small transaction.
            plan = await self._writer.run(
                lambda conn: _plan_runtime_observations_batch(
                    conn,
                    ttl_seconds=self._retention_ttl_seconds,
                    row_cap=self._row_cap,
                    now_ms=effective_now,
                )
            )
            if self._close_requested.is_set():
                return
            if plan.ids:
                await self._writer.run(lambda conn: self._delete_prune_plan(conn, plan, effective_now))
        except Exception as exc:  # noqa: BLE001 - committed observations remain committed
            self._prune_pending = False
            self._prune_now_ms = None
            logger.warning("runtime_observation_prune_failed error_type=%s", type(exc).__name__)
        else:
            self._prune_pending = plan.has_more
            if not plan.has_more:
                self._prune_now_ms = None

    @staticmethod
    def _delete_prune_plan(conn: sqlite3.Connection, plan: _PrunePlan, now_ms: int) -> None:
        with write_transaction(conn):
            _apply_runtime_observations_plan(conn, plan, now_ms=now_ms)

    @staticmethod
    def _write_batch(conn: sqlite3.Connection, jobs: list[_ObservationJob]) -> None:
        with write_transaction(conn):
            for job in jobs:
                record_runtime_observation(
                    conn,
                    kind=job.kind,
                    dialog_id=job.dialog_id,
                    operation_id=job.operation_id,
                    outcome=job.outcome,
                    reason_code=job.reason_code,
                    duration_ms=job.duration_ms,
                    tool_name=job.tool_name,
                    tool_capability=job.tool_capability,
                    contract_version=job.contract_version,
                    result_count=job.result_count,
                    has_cursor=job.has_cursor,
                    page_depth=job.page_depth,
                    has_filter=job.has_filter,
                    error_type=job.error_type,
                    payload=cast(Mapping[str, object], json.loads(job.payload_json)),
                    observed_at_ms=job.observed_at_ms,
                )


def _plan_runtime_observations_batch(
    conn: sqlite3.Connection,
    *,
    ttl_seconds: int,
    row_cap: int,
    now_ms: int,
) -> _PrunePlan:
    """Select immutable ids without reserving SQLite's writer."""
    if ttl_seconds < 1 or row_cap < 1:
        raise ValueError("runtime event retention limits must be positive")
    cutoff_ms = now_ms - ttl_seconds * 1000
    ttl_rows = cast(
        list[tuple[int]],
        conn.execute(
            "SELECT id FROM runtime_observations "
            "INDEXED BY idx_runtime_observations_time "
            "WHERE observed_at_ms < ? ORDER BY observed_at_ms, id LIMIT ?",
            (cutoff_ms, _PRUNE_BATCH_SIZE + 1),
        ).fetchall(),
    )
    if ttl_rows:
        return _PrunePlan(tuple(row[0] for row in ttl_rows[:_PRUNE_BATCH_SIZE]), False, True)

    cap_rows = cast(
        list[tuple[int]],
        conn.execute(
            "SELECT id FROM runtime_observations ORDER BY id LIMIT ("
            "SELECT MAX(COUNT(*) - ?, 0) FROM ("
            "SELECT 1 FROM runtime_observations INDEXED BY idx_runtime_observations_time LIMIT ?"
            "))",
            (row_cap, row_cap + _PRUNE_BATCH_SIZE + 1),
        ).fetchall(),
    )
    return _PrunePlan(
        tuple(row[0] for row in cap_rows[:_PRUNE_BATCH_SIZE]),
        True,
        len(cap_rows) > _PRUNE_BATCH_SIZE,
    )


def _apply_runtime_observations_plan(
    conn: sqlite3.Connection,
    plan: _PrunePlan,
    *,
    now_ms: int,
) -> _PruneBatchResult:
    """Apply exact planned ids in the caller-owned transaction."""
    if not plan.ids:
        return _PruneBatchResult(0, 0, plan.has_more)
    placeholders = ",".join("?" for _ in plan.ids)
    cursor = conn.execute(
        f"DELETE FROM runtime_observations WHERE id IN ({placeholders})",
        plan.ids,
    )
    deleted = max(cursor.rowcount, 0)
    cap_deleted = deleted if plan.cap else 0
    if cap_deleted:
        conn.execute(
            "INSERT OR REPLACE INTO daemon_state(key, value) VALUES ('runtime_observations_last_cap_truncation_ms', ?)",
            (str(now_ms),),
        )
    return _PruneBatchResult(deleted, cap_deleted, plan.has_more)


def _prune_runtime_observations_batch(
    conn: sqlite3.Connection,
    *,
    ttl_seconds: int,
    row_cap: int,
    now_ms: int,
) -> _PruneBatchResult:
    """Plan outside the lock, then apply one bounded deletion transaction."""
    plan = _plan_runtime_observations_batch(
        conn,
        ttl_seconds=ttl_seconds,
        row_cap=row_cap,
        now_ms=now_ms,
    )
    if not plan.ids:
        return _PruneBatchResult(0, 0, plan.has_more)
    with write_transaction(conn):
        return _apply_runtime_observations_plan(conn, plan, now_ms=now_ms)


def prune_runtime_observations(
    conn: sqlite3.Connection, *, ttl_seconds: int, row_cap: int = DEFAULT_ROW_CAP, now_ms: int | None = None
) -> int:
    """Prune by age and emergency cap; record the resulting coverage boundary."""
    if ttl_seconds < 1 or row_cap < 1:
        raise ValueError("runtime event retention limits must be positive")
    effective_now = int(time.time() * 1000) if now_ms is None else now_ms
    deleted = 0
    while True:
        plan = _plan_runtime_observations_batch(
            conn,
            ttl_seconds=ttl_seconds,
            row_cap=row_cap,
            now_ms=effective_now,
        )
        result = _apply_runtime_observations_plan(conn, plan, now_ms=effective_now)
        deleted += result.deleted
        if not result.has_more:
            return deleted


__all__ = [
    "ALLOWED_KINDS",
    "DEFAULT_ROW_CAP",
    "MAX_PAYLOAD_BYTES",
    "RUNTIME_INSTANCE_ID",
    "RuntimeObservationCounters",
    "RuntimeObservationPolicy",
    "RuntimeObservationSink",
    "encode_payload",
    "prune_runtime_observations",
    "record_runtime_observation",
    "tool_telemetry_identity",
]
