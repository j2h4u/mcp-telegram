"""Feedback SQLite must not block the daemon loop or leak transaction ownership."""

import asyncio
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

import pytest

from mcp_telegram.daemon import FeedbackThreadOwner
from mcp_telegram.feedback_db import SQLiteFeedbackStore, ensure_feedback_schema


@pytest.mark.parametrize("cancel", [False, True])
async def test_blocked_feedback_drains_before_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    path = tmp_path / "feedback.db"
    owner = await FeedbackThreadOwner.open(path)
    blocker = sqlite3.connect(path)
    blocker.execute("BEGIN IMMEDIATE")
    started = threading.Event()
    original = SQLiteFeedbackStore.insert_feedback

    def observed_insert(  # noqa: PLR0913 - mirrors the storage callback contract
        self: SQLiteFeedbackStore,
        *,
        submitted_at: int,
        message: str,
        severity: str | None,
        context: object | None,
        model: object | None,
        harness: object | None,
    ) -> int | None:
        started.set()
        return original(
            self,
            submitted_at=submitted_at,
            message=message,
            severity=severity,
            context=context,
            model=model,
            harness=harness,
        )

    monkeypatch.setattr(SQLiteFeedbackStore, "insert_feedback", observed_insert)
    task = asyncio.create_task(owner.submit_feedback({"message": "test"}))
    close_task: asyncio.Task[None] | None = None
    try:
        assert await asyncio.to_thread(started.wait, 2)
        assert await asyncio.wait_for(asyncio.sleep(0), 1) is None
        assert not task.done()
        if cancel:
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
        close_task = asyncio.create_task(owner.aclose())
        await asyncio.sleep(0)
        assert not task.done() and not close_task.done()
        blocker.rollback()
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
        else:
            assert (await asyncio.wait_for(task, 5))["ok"] is True
        await asyncio.wait_for(close_task, 5)
        with closing(sqlite3.connect(path)) as reader:
            assert reader.execute("SELECT message FROM feedback").fetchone() == ("test",)
    finally:
        blocker.rollback()
        blocker.close()
        if close_task is not None:
            await asyncio.gather(close_task, return_exceptions=True)
        await owner.aclose()


def test_missing_status_releases_write_transaction(tmp_path: Path) -> None:
    path = tmp_path / "feedback.db"
    conn = ensure_feedback_schema(path)
    try:
        store = SQLiteFeedbackStore(conn)
        assert not store.update_feedback_status(feedback_id=42, status="done", status_changed_at=1, reason=None)
        assert not conn.in_transaction
        with closing(sqlite3.connect(path, timeout=0)) as competitor:
            competitor.execute("INSERT INTO feedback (submitted_at, message) VALUES (1, 'other')")
    finally:
        conn.close()


async def test_status_operations_use_the_owned_connection(tmp_path: Path) -> None:
    owner = await FeedbackThreadOwner.open(tmp_path / "feedback.db")
    try:
        created = await owner.submit_feedback({"message": "test"})
        assert created["ok"] is True
        assert (await owner.update_feedback_status({"id": 999, "status": "done"}))["error"] == "not_found"
        assert (await owner.update_feedback_status({"id": 1, "status": "done"}))["ok"] is True
    finally:
        await owner.aclose()
