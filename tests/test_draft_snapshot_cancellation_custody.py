"""Cancellation must not orphan a committed draft snapshot claim."""

import asyncio
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from mcp_telegram.config import DraftRecoveryConfig
from mcp_telegram.drafts import owner as owner_module
from mcp_telegram.drafts.contracts import SnapshotCoverage
from mcp_telegram.drafts.owner import DraftMessageOwner
from mcp_telegram.drafts.ports import DraftPreflightBusyError
from mcp_telegram.drafts.sqlite_projection import SQLiteDraftProjection
from mcp_telegram.event_handlers import UpdateProcessingBarrier
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_db import ensure_sync_schema
from tests.test_drafts_owner import _Gateway


class _DelayedWriter:
    def __init__(self, writer: SQLiteWriter) -> None:
        self.run_original = writer.run
        self.committed = asyncio.Event()
        self.return_claim = asyncio.Event()
        self.released = asyncio.Event()
        self.return_release = asyncio.Event()
        self.calls = 0

    async def run[T](self, callback: Callable[[sqlite3.Connection], T]) -> T:
        result = await self.run_original(callback)
        self.calls += 1
        if self.calls == 1:
            self.committed.set()
            await self.return_claim.wait()
        elif self.calls == 2:
            self.released.set()
            await self.return_release.wait()
        return result


async def test_cancelled_committed_claim_retains_lock_until_exact_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "drafts.sqlite"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    writer = await SQLiteWriter.open(path)
    gateway = _Gateway(SnapshotCoverage(42, True, 0, source_order_at=datetime(2026, 1, 1, tzinfo=UTC)), ())
    owner = DraftMessageOwner(
        object(),
        SQLiteDraftProjection(conn, DraftRecoveryConfig()),
        asyncio.Event(),
        UpdateProcessingBarrier(),
        writer=writer,
        snapshot_gateway_factory=lambda _client, _account: gateway,
    )
    await owner.bind_account(42)
    now = 1_700_000_000
    monkeypatch.setattr(owner_module, "time", SimpleNamespace(time=lambda: now))
    delayed = _DelayedWriter(writer)
    monkeypatch.setattr(writer, "run", delayed.run)
    task = asyncio.create_task(owner.refresh_for_mutation())
    try:
        await asyncio.wait_for(delayed.committed.wait(), 5)
        row = cast(
            tuple[int | None, int | None],
            conn.execute(
                "SELECT recovery_claimed_at, recovery_due_at FROM draft_projection_runtime WHERE singleton=1"
            ).fetchone(),
        )
        assert row == (now, None)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        assert not task.done() and owner._snapshot_lock.locked()
        with pytest.raises(DraftPreflightBusyError):
            await owner.refresh_for_mutation()
        assert gateway.calls == 0
        delayed.return_claim.set()
        await asyncio.wait_for(delayed.released.wait(), 5)
        assert conn.execute(
            "SELECT recovery_claimed_at, recovery_due_at FROM draft_projection_runtime WHERE singleton=1"
        ).fetchone() == (None, now)
        assert not task.done() and owner._snapshot_lock.locked()
        with pytest.raises(DraftPreflightBusyError):
            await owner.refresh_for_mutation()
        delayed.return_release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert not owner._snapshot_lock.locked()
        assert await owner.refresh_for_mutation()
        assert gateway.calls == 1
        assert conn.execute(
            "SELECT recovery_claimed_at FROM draft_projection_runtime WHERE singleton=1"
        ).fetchone() == (None,)
    finally:
        delayed.return_claim.set()
        delayed.return_release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await writer.aclose()
        conn.close()
