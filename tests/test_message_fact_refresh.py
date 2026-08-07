from __future__ import annotations

import asyncio
import sqlite3
from typing import cast

import pytest

from mcp_telegram.message_fact_refresh import (
    MessageFactRefreshDeps,
    MessageFactRefreshPolicy,
    refresh_message_facts_once,
)
from mcp_telegram.reactions.contracts import ReactionFreshness
from mcp_telegram.reactions.refresh import ReactionFreshener
from mcp_telegram.telegram_reading import ReadDateFetchResult, TelegramReadReceiptGateway


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE synced_dialogs (
            dialog_id INTEGER PRIMARY KEY,
            status TEXT NOT NULL
        );
        CREATE TABLE entities (
            id INTEGER PRIMARY KEY,
            type TEXT NOT NULL
        );
        CREATE TABLE messages (
            dialog_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            sent_at INTEGER NOT NULL,
            out INTEGER NOT NULL
        );
        CREATE TABLE message_reactions (
            dialog_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            emoji TEXT NOT NULL,
            count INTEGER NOT NULL
        );
        CREATE TABLE message_reactions_freshness (
            dialog_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            checked_at INTEGER NOT NULL,
            PRIMARY KEY (dialog_id, message_id)
        );
        CREATE TABLE message_read_facts (
            dialog_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            read_at INTEGER,
            checked_at INTEGER NOT NULL,
            status TEXT NOT NULL,
            PRIMARY KEY (dialog_id, message_id)
        );
        """
    )
    return conn


def _policy(*, reaction_max: int = 10, read_at_max: int = 10) -> MessageFactRefreshPolicy:
    return MessageFactRefreshPolicy(
        interval_seconds=600.0,
        reaction_max_messages_per_cycle=reaction_max,
        read_at_max_messages_per_cycle=read_at_max,
        pause_seconds=0.01,
        reaction_ttl_seconds=600,
        read_at_ttl_seconds=600,
    )


class _ReactionFreshener:
    def __init__(self) -> None:
        self.calls: list[tuple[int, object, list[int]]] = []

    async def refresh(self, dialog_id: int, entity: object, message_ids: list[int]) -> ReactionFreshness:
        self.calls.append((dialog_id, entity, message_ids))
        return ReactionFreshness(
            requested_count=len(message_ids),
            fresh_count=0,
            stale_count=len(message_ids),
            refreshed_count=len(message_ids),
            status="refreshed",
        )


class _ReadReceiptGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[object, int]] = []

    async def fetch_outbox_read_date(self, entity: object, message_id: int) -> ReadDateFetchResult:
        self.calls.append((entity, message_id))
        return ReadDateFetchResult(read_at=1_700_000_000 + message_id, status="complete")


@pytest.mark.asyncio
async def test_refresh_message_facts_once_refreshes_reactions_and_read_at() -> None:
    conn = _make_db()
    conn.executescript(
        """
        INSERT INTO synced_dialogs VALUES (10, 'synced'), (20, 'synced'), (30, 'access_lost');
        INSERT INTO entities VALUES (10, 'user'), (20, 'user'), (30, 'user');
        INSERT INTO messages VALUES
            (10, 1, 1000, 0),
            (20, 2, 1001, 1),
            (30, 3, 1002, 1);
        INSERT INTO message_reactions VALUES (10, 1, '👍', 1);
        """
    )
    reactions = _ReactionFreshener()
    read_receipts = _ReadReceiptGateway()

    try:
        result = await refresh_message_facts_once(
            MessageFactRefreshDeps(
                conn,
                cast(ReactionFreshener, reactions),
                cast(TelegramReadReceiptGateway, read_receipts),
            ),
            _policy(),
            now=2_000,
        )
        stored_read_facts = conn.execute("SELECT read_at, checked_at, status FROM message_read_facts").fetchall()
    finally:
        conn.close()

    assert result.reaction_candidates == 1
    assert result.reaction_refreshed == 1
    assert result.read_at_candidates == 1
    assert reactions.calls == [(10, 10, [1])]
    assert read_receipts.calls == [(20, 2)]
    assert stored_read_facts == [(1_700_000_002, 2_000, "complete")]


@pytest.mark.asyncio
async def test_refresh_message_facts_once_respects_zero_budget() -> None:
    conn = _make_db()
    reactions = _ReactionFreshener()
    read_receipts = _ReadReceiptGateway()

    try:
        result = await refresh_message_facts_once(
            MessageFactRefreshDeps(
                conn,
                cast(ReactionFreshener, reactions),
                cast(TelegramReadReceiptGateway, read_receipts),
            ),
            _policy(reaction_max=0, read_at_max=0),
            now=2_000,
            shutdown_event=asyncio.Event(),
        )
    finally:
        conn.close()

    assert result.reaction_candidates == 0
    assert result.reaction_refreshed == 0
    assert result.read_at_candidates == 0
    assert reactions.calls == []
    assert read_receipts.calls == []
