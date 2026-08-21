"""Real-time body handlers must honor an explicit v34 disable at write time."""
# pyright: reportAny=false, reportArgumentType=false, reportAttributeAccessIssue=false, reportMissingParameterType=false

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

import pytest
from telethon.tl.types import PeerUser  # type: ignore[import-untyped]

from helpers import build_mock_message
from mcp_telegram.event_handlers import EventHandlerManager, _EditedMessageEvent
from mcp_telegram.history_enrollment import disable_history
from mcp_telegram.realtime_history_policy import RealtimeHistoryCoverage
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema

_SQLiteConnection = sqlite3.Connection
_Coverage = RealtimeHistoryCoverage


@pytest.fixture()
def mock_client() -> MagicMock:
    client = MagicMock()
    client.add_event_handler = MagicMock()
    client.remove_event_handler = MagicMock()
    return client


@pytest.fixture()
def shutdown_event() -> asyncio.Event:
    return asyncio.Event()


def _open_seeded_db(
    path: Path,
    *,
    dialog_id: int,
    message_id: int,
    text: str,
    fts_text: str = "old fts",
) -> tuple[_SQLiteConnection, _SQLiteConnection]:
    """Open two writer connections over a real current-schema database."""
    ensure_sync_schema(path)
    handler_conn = _open_sync_db(path)
    disable_conn = _open_sync_db(path)
    handler_conn.execute(
        "INSERT INTO synced_dialogs(dialog_id, status, last_event_at) VALUES (?, 'synced', 17)",
        (dialog_id,),
    )
    # This is the v34 durable authorization relation, deliberately seeded as
    # enabled rather than inferred from synced_dialogs.status.
    handler_conn.execute(
        "INSERT INTO full_history_enrollment(dialog_id, enabled, source, updated_at) VALUES (?, 1, 'explicit', 1)",
        (dialog_id,),
    )
    handler_conn.execute(
        "INSERT INTO messages(dialog_id, message_id, sent_at, text, sender_id, sender_first_name, is_deleted) "
        "VALUES (?, ?, 1704067200, ?, 42, 'Alice', 0)",
        (dialog_id, message_id, text),
    )
    handler_conn.execute(
        "INSERT INTO messages_fts(dialog_id, message_id, stemmed_text) VALUES (?, ?, ?)",
        (dialog_id, message_id, fts_text),
    )
    handler_conn.commit()
    assert handler_conn.execute(
        "SELECT enabled FROM full_history_enrollment WHERE dialog_id = ?", (dialog_id,)
    ).fetchone() == (1,)
    return handler_conn, disable_conn


def _close_dbs(*connections: _SQLiteConnection) -> None:
    for connection in connections:
        connection.close()


def _install_disable_hook(
    manager: EventHandlerManager,
    disable_conn: _SQLiteConnection,
    *,
    dialog_id: int,
    trigger_call: int,
) -> None:
    """Commit disable immediately after one allowed realtime precheck."""
    original = manager._realtime_coverage
    calls = 0

    def hooked(dialog: int) -> _Coverage:
        nonlocal calls
        coverage = original(dialog)
        calls += 1
        if calls == trigger_call:
            assert coverage is _Coverage.FULL_HISTORY
            disable_history(disable_conn, dialog_id, now=2)
            disable_conn.commit()
        return coverage

    manager._realtime_coverage = cast(Callable[[int], _Coverage], hooked)


def _message_text(conn: _SQLiteConnection, dialog_id: int, message_id: int) -> str | None:
    row = conn.execute(
        "SELECT text FROM messages WHERE dialog_id = ? AND message_id = ?", (dialog_id, message_id)
    ).fetchone()
    return None if row is None else cast(str | None, row[0])


def _fts_text(conn: _SQLiteConnection, dialog_id: int, message_id: int) -> str | None:
    row = conn.execute(
        "SELECT stemmed_text FROM messages_fts WHERE dialog_id = ? AND message_id = ?", (dialog_id, message_id)
    ).fetchone()
    return None if row is None else cast(str | None, row[0])


def _version_rows(conn: _SQLiteConnection, dialog_id: int, message_id: int) -> list[tuple[int, str | None]]:
    return [
        (int(version), cast(str | None, old_text))
        for version, old_text in conn.execute(
            "SELECT version, old_text FROM message_versions WHERE dialog_id = ? AND message_id = ? ORDER BY version",
            (dialog_id, message_id),
        ).fetchall()
    ]


@pytest.mark.asyncio
async def test_edit_disable_after_precheck_skips_body_fts_and_version(
    tmp_path: Path, mock_client: MagicMock, shutdown_event: asyncio.Event
) -> None:
    dialog_id, message_id = 4101, 7
    handler_conn, disable_conn = _open_seeded_db(
        tmp_path / "edit-race.db", dialog_id=dialog_id, message_id=message_id, text="before"
    )
    try:
        manager = EventHandlerManager(mock_client, handler_conn, shutdown_event, mock_client.get_input_entity)
        # EDIT has a second precheck after async extraction. Flip authorization
        # after that allowed result and before the handler's write transaction.
        _install_disable_hook(manager, disable_conn, dialog_id=dialog_id, trigger_call=2)
        msg = build_mock_message(
            id=message_id,
            text="after",
            edit_date=datetime(2024, 1, 1, 13, 0, tzinfo=UTC),
        )
        await manager.on_message_edited(cast(_EditedMessageEvent, SimpleNamespace(chat_id=dialog_id, message=msg)))

        assert _message_text(handler_conn, dialog_id, message_id) == "before"
        assert _fts_text(handler_conn, dialog_id, message_id) == "old fts"
        assert _version_rows(handler_conn, dialog_id, message_id) == []
        assert handler_conn.execute(
            "SELECT last_event_at FROM synced_dialogs WHERE dialog_id = ?", (dialog_id,)
        ).fetchone() == (17,)
        assert disable_conn.execute(
            "SELECT enabled FROM full_history_enrollment WHERE dialog_id = ?", (dialog_id,)
        ).fetchone() == (0,)
    finally:
        _close_dbs(handler_conn, disable_conn)


@pytest.mark.asyncio
async def test_edit_enabled_path_still_updates_body_fts_and_version(
    tmp_path: Path, mock_client: MagicMock, shutdown_event: asyncio.Event
) -> None:
    dialog_id, message_id = 4102, 8
    handler_conn, disable_conn = _open_seeded_db(
        tmp_path / "edit-enabled.db", dialog_id=dialog_id, message_id=message_id, text="before"
    )
    try:
        manager = EventHandlerManager(mock_client, handler_conn, shutdown_event, mock_client.get_input_entity)
        msg = build_mock_message(
            id=message_id,
            text="after",
            edit_date=datetime(2024, 1, 1, 13, 0, tzinfo=UTC),
        )
        await manager.on_message_edited(cast(_EditedMessageEvent, SimpleNamespace(chat_id=dialog_id, message=msg)))

        assert _message_text(handler_conn, dialog_id, message_id) == "after"
        assert _fts_text(handler_conn, dialog_id, message_id) != "old fts"
        assert _version_rows(handler_conn, dialog_id, message_id) == [(1, "before")]
        assert handler_conn.execute(
            "SELECT enabled FROM full_history_enrollment WHERE dialog_id = ?", (dialog_id,)
        ).fetchone() == (1,)
    finally:
        _close_dbs(handler_conn, disable_conn)


@pytest.mark.asyncio
async def test_transcription_disable_after_precheck_skips_body_fts_and_version(
    tmp_path: Path, mock_client: MagicMock, shutdown_event: asyncio.Event
) -> None:
    dialog_id, message_id = 4201, 9
    handler_conn, disable_conn = _open_seeded_db(
        tmp_path / "transcription-race.db", dialog_id=dialog_id, message_id=message_id, text="voice before"
    )
    try:
        manager = EventHandlerManager(mock_client, handler_conn, shutdown_event, mock_client.get_input_entity)
        # TRANSCRIPTION's initial precheck is immediately followed by reads;
        # flip authorization before its guarded write transaction starts.
        _install_disable_hook(manager, disable_conn, dialog_id=dialog_id, trigger_call=1)
        update = SimpleNamespace(peer=PeerUser(user_id=dialog_id), msg_id=message_id, text="voice after", pending=False)
        await manager.on_raw_transcribed_audio(update)

        assert _message_text(handler_conn, dialog_id, message_id) == "voice before"
        assert _fts_text(handler_conn, dialog_id, message_id) == "old fts"
        assert _version_rows(handler_conn, dialog_id, message_id) == []
        assert handler_conn.execute(
            "SELECT last_event_at FROM synced_dialogs WHERE dialog_id = ?", (dialog_id,)
        ).fetchone() == (17,)
        assert disable_conn.execute(
            "SELECT enabled FROM full_history_enrollment WHERE dialog_id = ?", (dialog_id,)
        ).fetchone() == (0,)
    finally:
        _close_dbs(handler_conn, disable_conn)


@pytest.mark.asyncio
async def test_transcription_enabled_path_still_updates_body_fts_and_version(
    tmp_path: Path, mock_client: MagicMock, shutdown_event: asyncio.Event
) -> None:
    dialog_id, message_id = 4202, 10
    handler_conn, disable_conn = _open_seeded_db(
        tmp_path / "transcription-enabled.db", dialog_id=dialog_id, message_id=message_id, text="voice before"
    )
    try:
        manager = EventHandlerManager(mock_client, handler_conn, shutdown_event, mock_client.get_input_entity)
        update = SimpleNamespace(peer=PeerUser(user_id=dialog_id), msg_id=message_id, text="voice after", pending=False)
        await manager.on_raw_transcribed_audio(update)

        assert _message_text(handler_conn, dialog_id, message_id) == "voice after"
        assert _fts_text(handler_conn, dialog_id, message_id) != "old fts"
        assert _version_rows(handler_conn, dialog_id, message_id) == [(1, "voice before")]
        assert handler_conn.execute(
            "SELECT enabled FROM full_history_enrollment WHERE dialog_id = ?", (dialog_id,)
        ).fetchone() == (1,)
    finally:
        _close_dbs(handler_conn, disable_conn)
