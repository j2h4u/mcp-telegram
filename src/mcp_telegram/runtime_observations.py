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
from pathlib import Path
from typing import Protocol, cast

RUNTIME_INSTANCE_ID = uuid.uuid4().hex
MAX_PAYLOAD_BYTES = 1024
DEFAULT_ROW_CAP = 100_000
_WRITER_POLL_SECONDS = 0.05
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
    }
)

logger = logging.getLogger(__name__)


def _seconds_from_milliseconds(milliseconds: int) -> float:
    return milliseconds / 1_000


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


class RuntimeObservationSink:
    """Accept bounded observations without doing SQLite work in callbacks."""

    def __init__(
        self,
        database: Path | str | sqlite3.Connection,
        *,
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
        database_path = self._database_path(database)
        self._database_path_value = database_path
        self._retention_ttl_seconds = retention_ttl_seconds
        self._prune_every_writes = policy.prune_every_writes
        self._busy_timeout_ms = policy.writer_busy_timeout_ms
        self._writer_startup_wait_seconds = policy.writer_startup_wait_seconds
        self._shutdown_drain_grace_seconds = policy.shutdown_drain_grace_seconds
        self._row_cap = policy.row_cap
        self._jobs: queue.Queue[_ObservationJob] = queue.Queue(maxsize=policy.queue_capacity)
        self._close_requested = threading.Event()
        self._abort_requested = threading.Event()
        self._writer_ready = threading.Event()
        self._state_lock = threading.Lock()
        self._connection_lock = threading.Lock()
        self._counter_lock = threading.Lock()
        self._log_lock = threading.Lock()
        self._last_log_at = 0.0
        self._last_logged_summary = self._summary_snapshot(RuntimeObservationCounters())
        self._accepting = True
        self._closed = False
        self._writer_error: BaseException | None = None
        self._writer_connection: sqlite3.Connection | None = None
        self._counters = RuntimeObservationCounters()
        self._writer = threading.Thread(
            target=self._writer_main,
            name="runtime-observation-writer",
            daemon=True,
        )
        self._writer.start()
        self._writer_ready.wait(self._writer_startup_wait_seconds)

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
    ) -> None:
        """Encode and enqueue one observation without waiting for the writer."""
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
                raise RuntimeError("runtime observation sink writer failed") from self._writer_error
            if self._closed or not self._accepting:
                raise RuntimeError("runtime observation sink is closed")
            try:
                self._jobs.put_nowait(job)
            except queue.Full:
                self._increment("queue_full_drops")

    def close(self) -> None:
        """Synchronously close the sink for non-async callers."""
        self._close_sync()

    async def aclose(self) -> None:
        """Stop accepting, then allow queued jobs a grace period to drain.

        The grace period does not bound method return: an arbitrary Python or
        operating-system stall can keep the writer alive.  SQLite work is
        interrupted after the grace period, and this method intentionally
        waits for the writer thread to exit before returning.

        """
        await asyncio.to_thread(self._close_sync)

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
        logger.warning(
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

    def _close_sync(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._accepting = False
            self._close_requested.set()
        drain_until = time.monotonic() + self._shutdown_drain_grace_seconds
        remaining = max(drain_until - time.monotonic(), 0)
        self._writer.join(remaining)
        if self._writer.is_alive():
            self._abort_requested.set()
            self._interrupt_writer()
            self._drop_queued_jobs("shutdown_grace_drops")
            self._writer.join()
        with self._state_lock:
            self._closed = True
        self._maybe_log_summary()

    def _writer_main(self) -> None:
        conn: sqlite3.Connection | None = None
        startup_complete = False
        try:
            conn = self._open_writer_connection()
            with self._connection_lock:
                self._writer_connection = conn
            startup_complete = True
            self._writer_ready.set()
            self._run_writer_loop(conn)
        except Exception as exc:  # noqa: BLE001 - writer must survive connection errors
            self._handle_writer_failure(exc, startup_complete=startup_complete)
        finally:
            with self._connection_lock:
                self._writer_connection = None
            if conn is not None:
                conn.close()

    def _open_writer_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self._database_path_value),
            timeout=_seconds_from_milliseconds(self._busy_timeout_ms),
        )
        conn.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.set_progress_handler(lambda: int(self._abort_requested.is_set()), 1_000)
        return conn

    def _interrupt_writer(self) -> None:
        with self._connection_lock:
            if self._writer_connection is not None:
                self._writer_connection.interrupt()

    def _run_writer_loop(self, conn: sqlite3.Connection) -> None:
        writes_since_prune = 0
        while not self._abort_requested.is_set():
            try:
                job = self._jobs.get(True, _WRITER_POLL_SECONDS)
            except queue.Empty:
                if self._close_requested.is_set():
                    return
                continue
            try:
                writes_since_prune = self._process_job(conn, job, writes_since_prune)
                self._maybe_log_summary()
            finally:
                self._jobs.task_done()

    def _process_job(self, conn: sqlite3.Connection, job: _ObservationJob, writes_since_prune: int) -> int:
        result = self._write_job(conn, job, writes_since_prune)
        if result is None:
            self._increment("shutdown_grace_drops")
        elif result:
            writes_since_prune += 1
            if writes_since_prune >= self._prune_every_writes:
                writes_since_prune = 0
        return writes_since_prune

    def _handle_writer_failure(self, exc: BaseException, *, startup_complete: bool) -> None:
        with self._state_lock:
            self._writer_error = exc
            self._accepting = False
            self._close_requested.set()
        self._writer_ready.set()
        self._increment("permanent_failures")
        if not startup_complete:
            self._increment("startup_failures")
        self._drop_queued_jobs()
        logger.error(
            "runtime_observation_writer_%s error=%s",
            "failed" if startup_complete else "startup_failed",
            exc,
        )
        self._maybe_log_summary()

    def _write_job(self, conn: sqlite3.Connection, job: _ObservationJob, writes_since_prune: int) -> bool | None:
        payload = cast(Mapping[str, object], json.loads(job.payload_json))
        attempt = 0
        while not self._abort_requested.is_set():
            try:
                with conn:
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
                        payload=payload,
                        observed_at_ms=job.observed_at_ms,
                    )
                    if writes_since_prune + 1 >= self._prune_every_writes and not self._close_requested.is_set():
                        prune_runtime_observations(
                            conn,
                            ttl_seconds=self._retention_ttl_seconds,
                            row_cap=self._row_cap,
                        )
                self._increment("successful_writes")
                return True
            except sqlite3.OperationalError as exc:
                if self._abort_requested.is_set():
                    return self._shutdown_drop(conn, "after interrupt")
                message = str(exc).lower()
                if "locked" not in message and "busy" not in message:
                    self._increment("permanent_failures")
                    return False
                self._increment("busy_retries")
                time.sleep(min(0.01 * (attempt + 1), 0.05))
                attempt += 1
            except Exception:  # noqa: BLE001 - one bad job must not stop FIFO draining
                if self._abort_requested.is_set():
                    return self._shutdown_drop(conn, "during shutdown")
                self._increment("permanent_failures")
                return False
        return None

    @staticmethod
    def _shutdown_drop(conn: sqlite3.Connection, reason: str) -> None:
        try:
            conn.rollback()
        except sqlite3.Error:
            logger.debug("runtime observation rollback %s failed", reason, exc_info=True)
        return


def prune_runtime_observations(
    conn: sqlite3.Connection, *, ttl_seconds: int, row_cap: int = DEFAULT_ROW_CAP, now_ms: int | None = None
) -> int:
    """Prune by age and emergency cap; record the resulting coverage boundary."""
    if ttl_seconds < 1 or row_cap < 1:
        raise ValueError("runtime event retention limits must be positive")
    effective_now = int(time.time() * 1000) if now_ms is None else now_ms
    deleted = conn.execute(
        "DELETE FROM runtime_observations WHERE observed_at_ms < ?", (effective_now - ttl_seconds * 1000,)
    ).rowcount
    count_row = cast(tuple[int] | None, conn.execute("SELECT COUNT(*) FROM runtime_observations").fetchone())
    event_count = int(count_row[0] or 0) if count_row is not None else 0
    excess = max(event_count - row_cap, 0)
    if excess:
        conn.execute(
            "DELETE FROM runtime_observations WHERE id IN (SELECT id FROM runtime_observations ORDER BY id LIMIT ?)",
            (excess,),
        )
        deleted += excess
        conn.execute(
            "INSERT OR REPLACE INTO daemon_state(key, value) VALUES ('runtime_observations_last_cap_truncation_ms', ?)",
            (str(effective_now),),
        )
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
