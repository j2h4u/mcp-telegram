"""A bounded, single-thread owner for runtime SQLite writes."""

import asyncio
import contextvars
import inspect
import logging
import queue
import sqlite3
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from pathlib import Path
from typing import cast

from .delivered_update import isolate_delivered_update
from .sync_db import open_runtime_sync_db

_STOP = None
_SLOW_QUEUE_SECONDS = 1.0
logger = logging.getLogger(__name__)


class SQLiteWriterContractError(RuntimeError):
    """A callback left the writer connection outside its guarded idle state."""


class SQLiteWriter:
    """Serialize bounded synchronous callbacks on a dedicated SQLite thread."""

    def __init__(self, path: Path, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self._path = path
        self._slots = asyncio.Semaphore(capacity)
        self._queue: queue.SimpleQueue[
            tuple[Callable[[sqlite3.Connection], object], Future[object], contextvars.Context, float] | None
        ] = queue.SimpleQueue()
        self._state_lock = threading.Lock()
        self._closed = False
        self._fatal_error: BaseException | None = None
        self._failure_future: Future[BaseException] = Future()
        self._closed_future: Future[None] = Future()
        self._thread = threading.Thread(target=self._work, name="sync-db-writer", daemon=True)
        self._loop = asyncio.get_running_loop()
        self._ready: Future[None] = Future()
        self._thread.start()

    @classmethod
    async def open(cls, path: Path, capacity: int = 128) -> SQLiteWriter:
        writer = cls(path, capacity)
        ready = asyncio.wrap_future(writer._ready)
        try:
            await asyncio.wait({ready})
        except asyncio.CancelledError:
            writer._request_close()
            closing = asyncio.wrap_future(writer._closed_future)
            while not closing.done():
                try:
                    await asyncio.wait({closing})
                except asyncio.CancelledError:
                    continue
            try:
                closing.result()
            except BaseException:  # noqa: BLE001 - cancellation remains the caller-visible result.
                pass
            try:
                ready.result()
            except BaseException:  # noqa: BLE001 - retrieve startup failure after teardown.
                pass
            raise
        ready.result()
        return writer

    async def run[T](self, callback: Callable[[sqlite3.Connection], T]) -> T:
        await self._slots.acquire()
        with self._state_lock:
            if self._closed or self._fatal_error is not None:
                self._slots.release()
                raise RuntimeError("SQLite writer is unavailable") from self._fatal_error
            result: Future[object] = Future()
            context = contextvars.copy_context()
            isolate_delivered_update(context)
            self._queue.put((callback, result, context, time.monotonic()))
        wrapped = asyncio.wrap_future(result)
        try:
            await asyncio.wait({wrapped})
            return wrapped.result()  # type: ignore[return-value]
        except asyncio.CancelledError:
            while not wrapped.done():
                try:
                    await asyncio.wait({wrapped})
                except asyncio.CancelledError:
                    continue
            try:
                wrapped.result()
            except BaseException:  # noqa: BLE001 - cancellation remains the caller-visible result.
                pass
            raise

    async def aclose(self) -> None:
        self._request_close()
        closed = asyncio.wrap_future(self._closed_future)
        cancelled = False
        while not closed.done():
            try:
                await asyncio.wait({closed})
            except asyncio.CancelledError:
                cancelled = True
        try:
            closed.result()
        except BaseException as error:
            if not cancelled:
                raise
            cancellation = asyncio.CancelledError()
            cancellation.add_note(f"SQLite writer shutdown also failed: {type(error).__name__}")
            raise cancellation from error
        if cancelled:
            raise asyncio.CancelledError

    @property
    def failure(self) -> BaseException | None:
        with self._state_lock:
            return self._fatal_error

    async def wait_failed(self) -> BaseException:
        """Wait until the worker retires and return its fatal cause."""
        return await asyncio.shield(asyncio.wrap_future(self._failure_future))

    def _request_close(self) -> None:
        with self._state_lock:
            if not self._closed:
                self._closed = True
                if self._fatal_error is None:
                    self._queue.put(_STOP)

    def _work(self) -> None:
        conn: sqlite3.Connection | None = None
        shutdown_error: BaseException | None = None
        try:
            conn = open_runtime_sync_db(self._path)
            conn.row_factory = sqlite3.Row
            self._ready.set_result(None)
            while True:
                item = self._queue.get()
                if item is None:
                    conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                    return
                callback, result, context, admitted_at = item
                queue_wait = max(0.0, time.monotonic() - admitted_at)
                if queue_wait >= _SLOW_QUEUE_SECONDS:
                    logger.warning("sqlite_writer_queue_slow queue_wait_seconds=%.3f", queue_wait)
                try:
                    if self._execute(conn, callback, context, result):
                        conn = None
                        return
                finally:
                    self._loop.call_soon_threadsafe(self._slots.release)
        except BaseException as error:  # noqa: BLE001 - retire the thread for any fatal worker failure.
            shutdown_error = error
            with self._state_lock:
                self._set_failure(error)
                if not self._ready.done():
                    self._ready.set_exception(error)
                self._fail_pending(error)
        finally:
            self._finish_worker(conn, shutdown_error)

    def _finish_worker(self, conn: sqlite3.Connection | None, error: BaseException | None) -> None:
        if conn is not None:
            try:
                conn.close()
            except BaseException as close_error:  # noqa: BLE001 - settle close after the cleanup attempt.
                if error is None:
                    error = close_error
                else:
                    error.add_note(f"Closing SQLite writer connection failed: {type(close_error).__name__}")
        if self._closed_future.done():
            return
        if error is None:
            self._closed_future.set_result(None)
        else:
            self._closed_future.set_exception(error)

    def _execute(
        self,
        conn: sqlite3.Connection,
        callback: Callable[[sqlite3.Connection], object],
        context: contextvars.Context,
        result: Future[object],
    ) -> bool:
        try:
            self._check_idle(conn)
            value = context.run(callback, conn)
            if inspect.isawaitable(value):
                if inspect.iscoroutine(value):
                    value.close()
                raise SQLiteWriterContractError("SQLite writer callback returned an awaitable")
            if isinstance(value, (sqlite3.Connection, sqlite3.Cursor)):
                raise SQLiteWriterContractError("SQLite writer callback returned a thread-bound value")
            self._check_idle(conn)
            result.set_result(value)
            return False
        except BaseException as error:  # noqa: BLE001 - rollback callback failures before returning them.
            fatal = self._recover(conn, error)
            result.set_exception(error)
            if fatal:
                self._retire(error)
            return fatal

    def _retire(self, error: BaseException) -> None:
        with self._state_lock:
            self._set_failure(error)
            self._fail_pending(error)

    def _set_failure(self, error: BaseException) -> None:
        self._fatal_error = error
        if not self._failure_future.done():
            self._failure_future.set_result(error)

    @staticmethod
    def _recover(conn: sqlite3.Connection, error: BaseException) -> bool:
        fatal = isinstance(error, SQLiteWriterContractError)
        try:
            if conn.in_transaction:
                fatal = True
                conn.rollback()
        except BaseException as rollback_error:  # noqa: BLE001 - failed rollback makes the connection unsafe.
            error.add_note(f"SQLite writer rollback failed: {type(rollback_error).__name__}")
            SQLiteWriter._close_quietly(conn)
            return True
        try:
            if not conn.execute("PRAGMA query_only").fetchone()[0]:
                fatal = True
            conn.execute("PRAGMA query_only = ON")
        except BaseException as guard_error:  # noqa: BLE001 - failed guard restore makes it unsafe.
            error.add_note(f"SQLite writer guard restore failed: {type(guard_error).__name__}")
            SQLiteWriter._close_quietly(conn)
            return True
        if fatal:
            SQLiteWriter._close_quietly(conn)
        return fatal

    @staticmethod
    def _close_quietly(conn: sqlite3.Connection) -> None:
        try:
            conn.close()
        except BaseException:  # noqa: BLE001 - preserve the operation failure while releasing the connection.
            pass

    @staticmethod
    def _check_idle(conn: sqlite3.Connection) -> None:
        if conn.in_transaction:
            raise SQLiteWriterContractError("SQLite writer callback left a transaction open")
        query_only = cast(tuple[int], conn.execute("PRAGMA query_only").fetchone())[0]
        if not query_only:
            raise SQLiteWriterContractError("SQLite writer callback left query_only disabled")

    def _fail_pending(self, error: BaseException) -> None:
        del error
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            if item is None:
                continue
            _, result, _, _ = item
            if not result.done():
                result.set_exception(RuntimeError("SQLite writer connection retired"))
            self._loop.call_soon_threadsafe(self._slots.release)
