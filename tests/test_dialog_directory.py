"""Focused raw-source and publication tests for the canonical dialog directory."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
from telethon.tl import functions, types  # type: ignore[import-untyped]

from mcp_telegram.dialog_directory import CanonicalDialogDirectory, _decode_cursor, _encode_input_peer
from mcp_telegram.dialog_directory_tl import (
    DialogCursor,
    get_dialogs_request,
    normalize_dialogs_response,
    normalize_pinned_dialogs_response,
)
from mcp_telegram.flood import TelegramRpcThrottled
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema


def _dialog(peer_id: int, message_id: int) -> types.Dialog:
    return types.Dialog(
        peer=types.PeerUser(peer_id),
        top_message=message_id,
        read_inbox_max_id=0,
        read_outbox_max_id=0,
        unread_count=0,
        unread_mentions_count=0,
        unread_reactions_count=0,
        unread_poll_votes_count=0,
        notify_settings=None,
    )


def _response(dialogs: Sequence[types.Dialog], *, terminal: bool = True) -> object:
    users = [types.User(id=dialog.peer.user_id, first_name=f"User {dialog.peer.user_id}", access_hash=42) for dialog in dialogs]
    messages = [
        types.Message(
            id=dialog.top_message,
            peer_id=dialog.peer,
            date=datetime(2026, 1, 1, 0, 0, second, tzinfo=UTC),
        )
        for second, dialog in enumerate(dialogs)
    ]
    if terminal:
        return types.messages.Dialogs(dialogs=list(dialogs), messages=messages, chats=[], users=users)
    return types.messages.DialogsSlice(count=200, dialogs=list(dialogs), messages=messages, chats=[], users=users)


def _pinned_response(dialogs: Sequence[types.Dialog] = ()) -> types.messages.PeerDialogs:
    return types.messages.PeerDialogs(
        dialogs=list(dialogs),
        messages=[],
        chats=[],
        users=[types.User(id=dialog.peer.user_id, first_name=f"User {dialog.peer.user_id}", access_hash=42) for dialog in dialogs],
        state=types.updates.State(pts=0, qts=0, date=None, seq=0, unread_count=0),
    )


def test_raw_request_has_only_the_contract_arguments() -> None:
    request = get_dialogs_request(None)

    assert request.limit == 100
    assert request.exclude_pinned is True
    assert request.folder_id is None
    assert request.hash == 0


def test_slice_continues_and_uses_peer_matched_last_message_cursor() -> None:
    page = normalize_dialogs_response(_response([_dialog(1, 8), _dialog(2, 8)], terminal=False), None)

    assert page.kind == "page"
    assert page.cursor is not None
    assert page.cursor.offset_id == 8
    assert isinstance(page.cursor.offset_peer, types.InputPeerUser)
    assert page.cursor.offset_peer.user_id == 2


def test_missing_message_and_repeated_cursor_are_never_eof() -> None:
    dialog = _dialog(1, 8)
    response = types.messages.DialogsSlice(
        count=1,
        dialogs=[dialog],
        messages=[],
        chats=[],
        users=[types.User(id=1, first_name="One", access_hash=42)],
    )
    assert normalize_dialogs_response(response, None).kind == "incomplete"

    valid = normalize_dialogs_response(_response([dialog], terminal=False), None)
    assert valid.cursor is not None
    assert normalize_dialogs_response(_response([dialog], terminal=False), valid.cursor).kind == "invalid"


def test_missing_entity_markers_duplicates_and_not_modified_are_explicit_outcomes() -> None:
    dialog = _dialog(1, 8)
    missing_entity = types.messages.DialogsSlice(
        count=1,
        dialogs=[dialog],
        messages=[types.Message(id=8, peer_id=types.PeerUser(1), date=datetime(2026, 1, 1, tzinfo=UTC))],
        chats=[],
        users=[],
    )
    assert normalize_dialogs_response(missing_entity, None).kind == "incomplete"

    marker = types.DialogFolder(
        folder=types.Folder(id=2, title="marker"),
        peer=types.PeerUser(1),
        top_message=8,
        unread_muted_peers_count=0,
        unread_unmuted_peers_count=0,
        unread_muted_messages_count=0,
        unread_unmuted_messages_count=0,
    )
    marker_page = types.messages.DialogsSlice(count=1, dialogs=[marker], messages=[], chats=[], users=[])
    assert normalize_dialogs_response(marker_page, None).kind == "incomplete"

    assert normalize_dialogs_response(_response([dialog, _dialog(1, 9)], terminal=False), None).kind == "invalid"
    assert normalize_dialogs_response(types.messages.DialogsNotModified(count=1), None).kind == "not_modified"


def test_pinned_requires_real_peer_dialogs_constructor() -> None:
    dialog = _dialog(1, 8)
    pinned = types.messages.PeerDialogs(
        dialogs=[dialog],
        messages=[],
        chats=[],
        users=[types.User(id=1, first_name="One", access_hash=42)],
        state=types.updates.State(pts=0, qts=0, date=None, seq=0, unread_count=0),
    )
    assert normalize_pinned_dialogs_response(pinned).kind == "terminal"
    assert normalize_pinned_dialogs_response(_response([dialog])).kind == "invalid"


def test_min_entity_cannot_fabricate_a_cursor() -> None:
    dialog = _dialog(1, 8)
    response = types.messages.DialogsSlice(
        count=1,
        dialogs=[dialog],
        messages=[types.Message(id=8, peer_id=types.PeerUser(1), date=datetime(2026, 1, 1, tzinfo=UTC))],
        chats=[],
        users=[types.User(id=1, first_name="One", min=True, access_hash=42)],
    )
    assert normalize_dialogs_response(response, None).kind == "incomplete"


def test_cursor_round_trip_keeps_real_self_and_forbidden_peers() -> None:
    cursor = DialogCursor(
        datetime(2026, 1, 1, tzinfo=UTC),
        8,
        types.InputPeerSelf(),
    )
    restored = _decode_cursor(cursor.offset_date.isoformat(), cursor.offset_id, _encode_input_peer(cursor))
    assert restored is not None
    assert isinstance(restored.offset_peer, types.InputPeerSelf)

    dialog = types.Dialog(
        peer=types.PeerChannel(9),
        top_message=8,
        read_inbox_max_id=0,
        read_outbox_max_id=0,
        unread_count=0,
        unread_mentions_count=0,
        unread_reactions_count=0,
        unread_poll_votes_count=0,
        notify_settings=None,
    )
    response = types.messages.DialogsSlice(
        count=1,
        dialogs=[dialog],
        messages=[types.Message(id=8, peer_id=dialog.peer, date=datetime(2026, 1, 1, tzinfo=UTC))],
        chats=[types.ChannelForbidden(id=9, access_hash=99, title="Forbidden")],
        users=[],
    )
    page = normalize_dialogs_response(response, None)
    assert page.kind == "page"
    assert isinstance(page.cursor.offset_peer if page.cursor else None, types.InputPeerChannel)


class _FakeClient:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.requests: list[object] = []

    async def __call__(self, request: object) -> object:
        self.requests.append(request)
        return self.responses.pop(0)

    async def get_me(self) -> object:
        return types.User(id=100, is_self=True)


class _ThrottledClient(_FakeClient):
    async def __call__(self, request: object) -> object:
        del request
        raise TelegramRpcThrottled(30)


@pytest.mark.asyncio
async def test_throttle_reaches_coordinator_policy(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    with pytest.raises(TelegramRpcThrottled):
        await CanonicalDialogDirectory(_ThrottledClient([]), db_path, asyncio.Event()).run_slice()


@pytest.mark.asyncio
async def test_publication_is_atomic_and_preserves_realtime_revision(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = _open_sync_db(db_path)
    try:
        conn.execute("INSERT INTO dialogs(dialog_id,name,type,snapshot_at,hidden) VALUES (1,'Realtime','user',1,0)")
        conn.commit()
    finally:
        conn.close()
    client = _FakeClient(
        [
            _pinned_response(),
            _pinned_response(),
            _response([_dialog(1, 8)]),
        ]
    )
    directory = CanonicalDialogDirectory(client, db_path, asyncio.Event())

    await directory.run_slice()
    await directory.run_slice()
    conn = _open_sync_db(db_path)
    try:
        # Simulate the realtime writer after the baseline and before terminal publication.
        conn.execute("UPDATE dialogs SET name='New realtime fact' WHERE dialog_id=1")
        conn.commit()
    finally:
        conn.close()
    await directory.run_slice()

    conn = _open_sync_db(db_path)
    try:
        assert conn.execute("SELECT name, hidden FROM dialogs WHERE dialog_id=1").fetchone() == ("New realtime fact", 0)
        assert conn.execute("SELECT status FROM dialog_directory_state").fetchone() == ("complete",)
        assert conn.execute("SELECT COUNT(*) FROM dialog_directory_staging").fetchone() == (0,)
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_cursor_and_page_commit_together_and_absence_waits_for_eof(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = _open_sync_db(db_path)
    try:
        conn.execute("INSERT INTO dialogs(dialog_id,name,type,snapshot_at,hidden) VALUES (9,'Existing','user',1,0)")
        conn.commit()
    finally:
        conn.close()

    client = _FakeClient(
        [
            _pinned_response(),
            _pinned_response(),
            _response([_dialog(1, 8)], terminal=False),
            types.messages.Dialogs(dialogs=[], messages=[], chats=[], users=[]),
        ]
    )
    directory = CanonicalDialogDirectory(client, db_path, asyncio.Event())

    await directory.run_slice()
    await directory.run_slice()
    await directory.run_slice()
    conn = _open_sync_db(db_path)
    try:
        assert conn.execute("SELECT hidden FROM dialogs WHERE dialog_id=9").fetchone() == (0,)
        assert conn.execute("SELECT offset_id FROM dialog_directory_state").fetchone() == (8,)
        assert conn.execute("SELECT COUNT(*) FROM dialog_directory_staging").fetchone() == (1,)
        # An unseen row modified after the baseline cannot be hidden at EOF.
        conn.execute("UPDATE dialogs SET name='Event-created fact' WHERE dialog_id=9")
        conn.commit()
    finally:
        conn.close()
    await directory.run_slice()

    conn = _open_sync_db(db_path)
    try:
        assert conn.execute("SELECT name,hidden FROM dialogs WHERE dialog_id=9").fetchone() == ("Event-created fact", 0)
        assert isinstance(client.requests[0], functions.messages.GetPinnedDialogsRequest)
        assert client.requests[0].folder_id == 0
        assert isinstance(client.requests[1], functions.messages.GetPinnedDialogsRequest)
        assert client.requests[1].folder_id == 1
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_invalid_page_discards_only_that_page_and_retries_saved_cursor(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    client = _FakeClient(
        [
            _pinned_response(),
            _pinned_response(),
            _response([_dialog(1, 8)], terminal=False),
            types.messages.DialogsNotModified(count=1),
            types.messages.Dialogs(dialogs=[], messages=[], chats=[], users=[]),
        ]
    )
    directory = CanonicalDialogDirectory(client, db_path, asyncio.Event())
    await directory.run_slice()
    await directory.run_slice()
    await directory.run_slice()
    await directory.run_slice()

    conn = _open_sync_db(db_path)
    try:
        assert conn.execute("SELECT status,offset_id FROM dialog_directory_state").fetchone() == ("incomplete", 8)
        assert conn.execute("SELECT COUNT(*) FROM dialog_directory_staging").fetchone() == (1,)
    finally:
        conn.close()

    await directory.run_slice()
    assert isinstance(client.requests[-1], functions.messages.GetDialogsRequest)
    assert client.requests[-1].offset_id == 8


@pytest.mark.asyncio
async def test_account_fence_and_published_pin_receipt_survive_new_attempt(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    pins = _pinned_response([_dialog(1, 8), _dialog(2, 9)])
    client = _FakeClient([pins, _pinned_response(), _response([])])
    directory = CanonicalDialogDirectory(client, db_path, asyncio.Event())
    await directory.run_slice()
    await directory.run_slice()
    await directory.run_slice()

    conn = _open_sync_db(db_path)
    try:
        assert conn.execute("SELECT account_id,generation FROM dialog_directory_publication").fetchone() == (100, 1)
        assert conn.execute("SELECT status,observed_count FROM dialog_directory_state").fetchone() == ("complete", 2)
        assert conn.execute("SELECT dialog_id,position FROM dialog_directory_published_pins WHERE folder_id=0 ORDER BY position").fetchall() == [
            (1, 0),
            (2, 1),
        ]
        assert conn.execute("SELECT COUNT(*) FROM dialog_directory_published_pins WHERE folder_id=1").fetchone() == (0,)
        assert conn.execute("SELECT value FROM daemon_state WHERE key='dialog_unread_sweep_observed_count'").fetchone() == ("2",)
        assert conn.execute("SELECT value FROM daemon_state WHERE key='dialog_unread_sweep_last_visible_count'").fetchone() == ("2",)
    finally:
        conn.close()

    with pytest.raises(RuntimeError, match="account identity changed"):
        directory.bind_account_id(101)


@pytest.mark.asyncio
async def test_cross_source_type_conflict_restarts_from_pinned_main_after_retry(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    dialog = _dialog(1, 8)
    conflict = types.messages.Dialogs(
        dialogs=[dialog],
        messages=[types.Message(id=8, peer_id=dialog.peer, date=datetime(2026, 1, 1, tzinfo=UTC))],
        chats=[],
        users=[types.User(id=1, first_name="One", access_hash=42, bot=True)],
    )
    client = _FakeClient([_pinned_response([dialog]), _pinned_response(), conflict, _pinned_response()])
    directory = CanonicalDialogDirectory(client, db_path, asyncio.Event())
    await directory.run_slice()
    await directory.run_slice()
    await directory.run_slice()

    conn = _open_sync_db(db_path)
    try:
        generation, status, retry_at = conn.execute(
            "SELECT generation,status,retry_at FROM dialog_directory_state"
        ).fetchone()
        assert (generation, status) == (1, "invalid")
        assert isinstance(retry_at, int)
        assert conn.execute("SELECT COUNT(*) FROM dialog_directory_staging").fetchone() == (0,)
        assert directory.status(float(retry_at - 1), conn).release_at == float(retry_at)
        conn.execute("UPDATE dialog_directory_state SET retry_at=0")
        conn.commit()
    finally:
        conn.close()

    await directory.run_slice()
    assert isinstance(client.requests[-1], functions.messages.GetPinnedDialogsRequest)
    assert client.requests[-1].folder_id == 0
    conn = _open_sync_db(db_path)
    try:
        assert conn.execute("SELECT generation,status FROM dialog_directory_state").fetchone() == (2, "in_progress")
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_complete_detail_waits_for_receipt_commit(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    details: list[str] = []
    client = _FakeClient([_pinned_response(), _pinned_response(), _response([])])
    directory = CanonicalDialogDirectory(client, db_path, asyncio.Event(), startup_detail_setter=details.append)
    await directory.run_slice()
    await directory.run_slice()
    conn = _open_sync_db(db_path)
    try:
        conn.execute(
            "CREATE TRIGGER fail_directory_receipt BEFORE UPDATE ON dialog_directory_publication "
            "BEGIN SELECT RAISE(ABORT, 'receipt failure'); END"
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(sqlite3.IntegrityError, match="receipt failure"):
        await directory.run_slice()
    assert "canonical dialog directory: complete" not in details


@pytest.mark.asyncio
async def test_page_failure_rolls_back_staging_and_cursor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    client = _FakeClient(
        [
            _pinned_response(),
            _pinned_response(),
            _response([_dialog(1, 8)], terminal=False),
        ]
    )
    directory = CanonicalDialogDirectory(client, db_path, asyncio.Event())
    await directory.run_slice()
    await directory.run_slice()

    def fail_after_staging(*_args: object) -> None:
        raise RuntimeError("simulated crash before page commit")

    monkeypatch.setattr(directory, "_save_cursor", fail_after_staging)
    with pytest.raises(RuntimeError, match="simulated crash"):
        await directory.run_slice()
    conn = _open_sync_db(db_path)
    try:
        assert conn.execute("SELECT offset_id FROM dialog_directory_state").fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM dialog_directory_staging").fetchone() == (0,)
    finally:
        conn.close()


def test_sqlite_row_composition_does_not_depend_on_tuple_factory(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        directory = CanonicalDialogDirectory(_FakeClient([]), db_path, asyncio.Event())
        assert directory.status(0.0, conn).release_at == 0.0
    finally:
        conn.close()


def test_freshness_uses_acquisition_start_not_publication_time(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = _open_sync_db(db_path)
    try:
        conn.execute(
            "UPDATE dialog_directory_state SET status='complete',observation_started_at=100,observation_completed_at=999"
        )
        conn.commit()
        directory = CanonicalDialogDirectory(_FakeClient([]), db_path, asyncio.Event())
        status = directory.status(999.0, conn)
        assert status is not None
        assert status.release_at == 1000.0
        assert not status.is_ready(999.0)
        assert status.is_ready(1000.0)
    finally:
        conn.close()
