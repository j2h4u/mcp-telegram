"""Focused contracts for the bounded Telegram reading gateways."""

from __future__ import annotations

import ast
import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from telethon.errors import ChannelPrivateError, FloodWaitError

from mcp_telegram.sync_worker import ReactionRecord
from mcp_telegram.telegram_fragments import FragmentContextService, TelethonTelegramFragmentGateway
from mcp_telegram.telegram_reactions import ReactionFreshener, TelethonTelegramReactionGateway
from mcp_telegram.telegram_reading import (
    GatewayFailure,
    GatewayFailureKind,
    ReactionFetchResult,
    ReactionMessage,
    TelegramReactionGateway,
)


def _message(message_id: int, *, reaction: bool = True) -> SimpleNamespace:
    reactions = None
    if reaction:
        reactions = SimpleNamespace(
            results=[SimpleNamespace(reaction=SimpleNamespace(emoticon="👍"), count=2)],
        )
    return SimpleNamespace(
        id=message_id,
        date=datetime.fromtimestamp(1_700_000_000 + message_id, tz=UTC),
        message=f"message {message_id}",
        sender_id=101,
        sender=SimpleNamespace(first_name="Alice"),
        media=None,
        reply_to=None,
        reactions=reactions,
        edit_date=None,
        grouped_id=None,
        out=False,
        post_author=None,
    )


def _seed_synced(conn: sqlite3.Connection, dialog_id: int) -> None:
    conn.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, read_inbox_max_id) VALUES (?, 'synced', 0)",
        (dialog_id,),
    )
    conn.commit()


class _WarningLogger:
    def __init__(self, warnings: list[tuple[object, ...]]) -> None:
        self._warnings = warnings

    def warning(self, msg: str, *args: object) -> None:
        _ = msg
        self._warnings.append(args)


@pytest.mark.asyncio
async def test_fragment_gateway_preserves_fixed_window_and_normalized_persistence(
    make_synced_db: Callable[[], sqlite3.Connection],
) -> None:
    conn = make_synced_db()
    client = SimpleNamespace(
        get_input_entity=AsyncMock(return_value="entity"),
        get_messages=AsyncMock(return_value=[_message(10), None, _message(12)]),
    )

    result = await FragmentContextService(conn, TelethonTelegramFragmentGateway(client)).fetch(42, 10)

    assert result.ok is True
    cast(AsyncMock, client.get_messages).assert_awaited_once_with("entity", ids=[10, 11, 12, 13, 14, 15])
    assert conn.execute("SELECT status FROM synced_dialogs WHERE dialog_id=42").fetchone() == ("fragment",)
    assert conn.execute("SELECT message_id, text FROM messages ORDER BY message_id").fetchall() == [
        (10, "message 10"),
        (12, "message 12"),
    ]
    assert conn.execute("SELECT message_id, emoji, count FROM message_reactions ORDER BY message_id").fetchall() == [
        (10, "👍", 2),
        (12, "👍", 2),
    ]


@pytest.mark.asyncio
async def test_fragment_gateway_translates_floodwait_without_partial_persistence(
    make_synced_db: Callable[[], sqlite3.Connection],
) -> None:
    conn = make_synced_db()
    flood = FloodWaitError(request=None, capture=17)
    client = SimpleNamespace(
        get_input_entity=AsyncMock(side_effect=flood),
        get_messages=AsyncMock(),
    )

    result = await FragmentContextService(conn, TelethonTelegramFragmentGateway(client)).fetch(42, 10)

    assert result.ok is False
    assert result.failure == GatewayFailure(
        kind=GatewayFailureKind.FLOOD_WAIT,
        error_type="FloodWaitError",
        error_message=str(flood),
        retryable=True,
        retry_after=17,
    )
    assert conn.execute("SELECT status FROM synced_dialogs WHERE dialog_id=42").fetchone() == ("fragment",)
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone() == (0,)


@pytest.mark.asyncio
async def test_reaction_freshener_refreshes_only_stale_active_window(
    make_synced_db: Callable[[], sqlite3.Connection],
) -> None:
    conn = make_synced_db()
    dialog_id = 1001
    _seed_synced(conn, dialog_id)
    now = int(time.time())
    conn.executemany(
        "INSERT INTO message_reactions_freshness (dialog_id, message_id, checked_at) VALUES (?, ?, ?)",
        [(dialog_id, 1, now - 10), (dialog_id, 2, now - 10)],
    )
    conn.commit()
    fetch_reactions = AsyncMock(
        return_value=ReactionFetchResult(
            messages=(
                ReactionMessage(
                    3,
                    (ReactionRecord(dialog_id=0, message_id=3, emoji="🔥", count=4),),
                ),
            )
        )
    )
    gateway = SimpleNamespace(fetch_reactions=fetch_reactions)

    freshness = await ReactionFreshener(conn, cast(TelegramReactionGateway, gateway)).refresh(
        dialog_id, dialog_id, [1, 2, 3]
    )

    assert freshness.status == "refreshed"
    assert freshness.fresh_count == 2
    assert freshness.stale_count == 1
    assert freshness.refreshed_count == 1
    fetch_reactions.assert_awaited_once_with(dialog_id, [3])
    assert conn.execute(
        "SELECT message_id, emoji, count FROM message_reactions WHERE dialog_id=?", (dialog_id,)
    ).fetchall() == [(3, "🔥", 4)]


@pytest.mark.asyncio
async def test_reaction_freshener_access_lost_is_quiet_and_structured(
    make_synced_db: Callable[[], sqlite3.Connection],
) -> None:
    conn = make_synced_db()
    dialog_id = 1001
    _seed_synced(conn, dialog_id)
    fetch_reactions = AsyncMock(
        return_value=ReactionFetchResult(
            failure=GatewayFailure(
                kind=GatewayFailureKind.ACCESS_LOST,
                error_type="ChannelPrivateError",
                error_message="private",
                retryable=False,
            )
        )
    )
    gateway = SimpleNamespace(fetch_reactions=fetch_reactions)
    warnings: list[tuple[object, ...]] = []
    log = _WarningLogger(warnings)

    freshness = await ReactionFreshener(conn, cast(TelegramReactionGateway, gateway), log=log).refresh(
        dialog_id, dialog_id, [1, 2]
    )

    assert freshness.as_dict() == {
        "requested_count": 2,
        "fresh_count": 0,
        "stale_count": 2,
        "refreshed_count": 0,
        "status": "access_lost",
        "retry_after": None,
    }
    assert warnings == []


@pytest.mark.asyncio
async def test_reaction_gateway_translates_private_and_floodwait_failures() -> None:
    private_client = SimpleNamespace(get_messages=AsyncMock(side_effect=ChannelPrivateError(request=None)))
    private = await TelethonTelegramReactionGateway(private_client).fetch_reactions(1, [10])
    assert private.failure is not None
    assert private.failure.kind is GatewayFailureKind.ACCESS_LOST

    flood = FloodWaitError(request=None, capture=23)
    flood_client = SimpleNamespace(get_messages=AsyncMock(side_effect=flood))
    result = await TelethonTelegramReactionGateway(flood_client).fetch_reactions(1, [10])
    assert result.failure is not None
    assert result.failure.kind is GatewayFailureKind.FLOOD_WAIT
    assert result.failure.retry_after == 23


def test_reading_query_modules_have_no_telethon_or_client_calls() -> None:
    paths = [
        Path("src/mcp_telegram/daemon_reading.py"),
        Path("src/mcp_telegram/daemon_message_queries.py"),
        Path("src/mcp_telegram/daemon_dialog_queries.py"),
        Path("src/mcp_telegram/daemon_read_state_queries.py"),
        Path("src/mcp_telegram/telegram_reading.py"),
        Path("src/mcp_telegram/telegram_reaction_queries.py"),
    ]
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports = [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
        imports.extend(node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom))
        assert all("telethon" not in name.lower() and "floodwait" not in name.lower() for name in imports), path
        direct_calls = [
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        ]
        assert not {"get_messages", "iter_messages", "get_input_entity"} & set(direct_calls), path
