"""Read-at fact refresh must re-check v34 authorization at persistence time."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import cast

import pytest

from mcp_telegram.history_enrollment import disable_history
from mcp_telegram.message_fact_refresh import (
    MessageFactRefreshDeps,
    MessageFactRefreshPolicy,
    refresh_message_facts_once,
)
from mcp_telegram.reactions.refresh import ReactionFreshener
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema
from mcp_telegram.telegram_reading import ReadDateFetchResult, TelegramReadReceiptGateway

_SQLiteConnection = sqlite3.Connection


class _BlockingReadGateway:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls: list[tuple[object, int]] = []

    async def fetch_outbox_read_date(self, entity: object, message_id: int) -> ReadDateFetchResult:
        self.calls.append((entity, message_id))
        self.entered.set()
        await self.release.wait()
        return ReadDateFetchResult(read_at=1_700_000_000 + message_id, status="complete")


class _ImmediateReadGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[object, int]] = []

    async def fetch_outbox_read_date(self, entity: object, message_id: int) -> ReadDateFetchResult:
        self.calls.append((entity, message_id))
        return ReadDateFetchResult(read_at=1_700_000_000 + message_id, status="complete")


def _policy() -> MessageFactRefreshPolicy:
    return MessageFactRefreshPolicy(
        interval_seconds=600.0,
        reaction_max_messages_per_cycle=0,
        read_at_max_messages_per_cycle=1,
        pause_seconds=0.01,
        reaction_ttl_seconds=600,
        read_at_ttl_seconds=600,
    )


def _open_seeded_db(path: Path, dialog_id: int, message_id: int) -> tuple[_SQLiteConnection, _SQLiteConnection]:
    ensure_sync_schema(path)
    handler_conn = _open_sync_db(path)
    disable_conn = _open_sync_db(path)
    handler_conn.execute("INSERT INTO synced_dialogs(dialog_id, status) VALUES (?, 'synced')", (dialog_id,))
    handler_conn.execute(
        "INSERT INTO full_history_enrollment(dialog_id, enabled, source, updated_at) VALUES (?, 1, 'explicit', 1)",
        (dialog_id,),
    )
    handler_conn.execute("INSERT INTO entities(id, type, updated_at) VALUES (?, 'user', 1)", (dialog_id,))
    handler_conn.execute(
        "INSERT INTO messages(dialog_id, message_id, sent_at, out, text) VALUES (?, ?, 1000, 1, NULL)",
        (dialog_id, message_id),
    )
    handler_conn.commit()
    assert handler_conn.execute(
        "SELECT enabled FROM full_history_enrollment WHERE dialog_id = ?", (dialog_id,)
    ).fetchone() == (1,)
    return handler_conn, disable_conn


def _close_dbs(*connections: _SQLiteConnection) -> None:
    for connection in connections:
        connection.close()


@pytest.mark.asyncio
async def test_read_at_disable_during_fetch_skips_read_fact_and_freshness_write(tmp_path: Path) -> None:
    dialog_id, message_id = 5101, 11
    handler_conn, disable_conn = _open_seeded_db(tmp_path / "read-at-race.db", dialog_id, message_id)
    gateway = _BlockingReadGateway()
    try:
        task = asyncio.create_task(
            refresh_message_facts_once(
                MessageFactRefreshDeps(
                    handler_conn,
                    cast(ReactionFreshener, object()),
                    cast(TelegramReadReceiptGateway, gateway),
                ),
                _policy(),
                now=2_000,
            )
        )
        await gateway.entered.wait()
        disable_history(disable_conn, dialog_id, now=2)
        disable_conn.commit()
        gateway.release.set()
        result = await task

        assert result.read_at_candidates == 1
        assert gateway.calls == [(dialog_id, message_id)]
        assert handler_conn.execute("SELECT * FROM message_read_facts").fetchall() == []
        assert handler_conn.execute("SELECT * FROM message_reactions_freshness").fetchall() == []
        assert disable_conn.execute(
            "SELECT enabled FROM full_history_enrollment WHERE dialog_id = ?", (dialog_id,)
        ).fetchone() == (0,)
    finally:
        _close_dbs(handler_conn, disable_conn)


@pytest.mark.asyncio
async def test_read_at_enabled_path_persists_read_fact(tmp_path: Path) -> None:
    dialog_id, message_id = 5102, 12
    handler_conn, disable_conn = _open_seeded_db(tmp_path / "read-at-enabled.db", dialog_id, message_id)
    gateway = _ImmediateReadGateway()
    try:
        result = await refresh_message_facts_once(
            MessageFactRefreshDeps(
                handler_conn,
                cast(ReactionFreshener, object()),
                cast(TelegramReadReceiptGateway, gateway),
            ),
            _policy(),
            now=2_000,
        )

        assert result.read_at_candidates == 1
        assert handler_conn.execute(
            "SELECT read_at, checked_at, status FROM message_read_facts WHERE dialog_id = ? AND message_id = ?",
            (dialog_id, message_id),
        ).fetchall() == [(1_700_000_012, 2_000, "complete")]
    finally:
        _close_dbs(handler_conn, disable_conn)
