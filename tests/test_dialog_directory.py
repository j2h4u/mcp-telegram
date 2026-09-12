"""Focused raw-source and publication tests for the canonical dialog directory."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
from telethon.tl import functions, types  # type: ignore[import-untyped]

from mcp_telegram.dialog_directory import (
    CanonicalDialogDirectory,
    CanonicalDialogDirectoryDemandAdapter,
    _decode_cursor,
    _eligibility_category,
    _encode_input_peer,
    _mute_until,
    _three_valued_unread,
    apply_realtime_eligibility,
    apply_realtime_identity,
    clear_realtime_mute,
    sync_active_generation_pins_from_publication,
)
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
    users = [
        types.User(id=dialog.peer.user_id, first_name=f"User {dialog.peer.user_id}", access_hash=42)
        for dialog in dialogs
    ]
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
        users=[
            types.User(id=dialog.peer.user_id, first_name=f"User {dialog.peer.user_id}", access_hash=42)
            for dialog in dialogs
        ],
        state=types.updates.State(pts=0, qts=0, date=None, seq=0, unread_count=0),
    )


def _unread_receipt(conn: sqlite3.Connection) -> dict[str, str]:
    return dict(
        conn.execute(
            "SELECT key,value FROM daemon_state WHERE key IN "
            "('dialog_unread_sweep_status','dialog_unread_sweep_completed_at',"
            "'dialog_unread_sweep_observed_count','dialog_unread_sweep_last_visible_count')"
        ).fetchall()
    )


def _stored_unread_receipt(db_path: Path) -> dict[str, str]:
    conn = _open_sync_db(db_path)
    try:
        return _unread_receipt(conn)
    finally:
        conn.close()


async def _run_slices(directory: CanonicalDialogDirectory, count: int) -> None:
    for _ in range(count):
        await directory.run_slice()


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


def test_terminal_missing_optional_facts_completes_but_slice_stalls() -> None:
    dialog = _dialog(1, 8)
    response = types.messages.DialogsSlice(
        count=1,
        dialogs=[dialog],
        messages=[],
        chats=[],
        users=[types.User(id=1, first_name="One", access_hash=42)],
    )
    stalled = normalize_dialogs_response(response, None)
    assert (stalled.kind, stalled.reason) == ("incomplete", "stalled:missing_safe_cursor")

    valid = normalize_dialogs_response(_response([dialog], terminal=False), None)
    assert valid.cursor is not None
    repeated = normalize_dialogs_response(_response([dialog], terminal=False), valid.cursor)
    assert (repeated.kind, repeated.reason) == ("incomplete", "stalled:non_advancing_cursor")

    terminal = types.messages.Dialogs(dialogs=[dialog], messages=[], chats=[], users=[])
    completed = normalize_dialogs_response(terminal, valid.cursor)
    assert completed.kind == "terminal"
    assert completed.cursor is None
    assert len(completed.facts) == 1


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


@pytest.mark.asyncio
async def test_imperfect_slice_advances_and_retains_every_identifiable_row(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    missing = _dialog(1, 7)
    usable = _dialog(2, 8)
    imperfect = types.messages.DialogsSlice(
        count=2,
        dialogs=[missing, usable],
        messages=[types.Message(id=8, peer_id=usable.peer, date=datetime(2026, 1, 1, tzinfo=UTC))],
        chats=[],
        users=[types.User(id=2, first_name="Two", access_hash=42)],
    )
    directory = CanonicalDialogDirectory(
        _FakeClient([_pinned_response(), _pinned_response(), imperfect, _response([])]), db_path, asyncio.Event()
    )

    await directory.run_slice()
    await directory.run_slice()
    await directory.run_slice()
    conn = _open_sync_db(db_path)
    try:
        assert conn.execute("SELECT offset_id FROM dialog_directory_state").fetchone() == (8,)
        assert conn.execute("SELECT COUNT(*) FROM dialog_directory_staging").fetchone() == (2,)
    finally:
        conn.close()

    await directory.run_slice()
    conn = _open_sync_db(db_path)
    try:
        assert conn.execute("SELECT dialog_id,type FROM dialogs ORDER BY dialog_id").fetchall() == [
            (1, "unknown"),
            (2, "user"),
        ]
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_wholly_unpageable_slice_stages_then_retries_from_committed_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    monkeypatch.setattr("mcp_telegram.dialog_directory.time.time", lambda: 1_000)
    unpageable = types.messages.DialogsSlice(
        count=1,
        dialogs=[_dialog(1, 8)],
        messages=[],
        chats=[],
        users=[types.User(id=1, first_name="One", access_hash=42)],
    )
    directory = CanonicalDialogDirectory(
        _FakeClient([_pinned_response(), _pinned_response(), unpageable, _response([])]),
        db_path,
        asyncio.Event(),
    )

    await directory.run_slice()
    await directory.run_slice()
    await directory.run_slice()
    conn = _open_sync_db(db_path)
    try:
        assert conn.execute(
            "SELECT status,ordinary_status,offset_id,retry_at,reason FROM dialog_directory_state"
        ).fetchone() == (
            "incomplete",
            "incomplete",
            0,
            1900,
            "stalled:missing_safe_cursor",
        )
        assert conn.execute("SELECT COUNT(*) FROM dialog_directory_staging").fetchone() == (1,)
        conn.execute("UPDATE dialog_directory_state SET retry_at=0")
        conn.commit()
    finally:
        conn.close()

    await directory.run_slice()
    conn = _open_sync_db(db_path)
    try:
        assert conn.execute("SELECT status FROM dialog_directory_state").fetchone() == ("complete",)
        assert conn.execute("SELECT dialog_id,type FROM dialogs").fetchone() == (1, "user")
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_pinned_membership_keeps_source_order_when_entities_are_unknown(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    pinned = types.messages.PeerDialogs(
        dialogs=[_dialog(2, 8), _dialog(1, 7)],
        messages=[],
        chats=[],
        users=[],
        state=types.updates.State(pts=0, qts=0, date=None, seq=0, unread_count=0),
    )
    directory = CanonicalDialogDirectory(
        _FakeClient([pinned, _pinned_response(), _response([])]), db_path, asyncio.Event()
    )

    await directory.run_slice()
    await directory.run_slice()
    await directory.run_slice()
    conn = _open_sync_db(db_path)
    try:
        assert conn.execute(
            "SELECT dialog_id,position FROM dialog_directory_published_pins WHERE folder_id=0 ORDER BY position"
        ).fetchall() == [(2, 0), (1, 1)]
        assert conn.execute("SELECT dialog_id,type FROM dialogs ORDER BY dialog_id").fetchall() == [
            (1, "unknown"),
            (2, "unknown"),
        ]
    finally:
        conn.close()


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
        self.get_me_calls = 0

    async def __call__(self, request: object) -> object:
        self.requests.append(request)
        return self.responses.pop(0)

    async def get_me(self) -> object:
        self.get_me_calls += 1
        return types.User(id=100, is_self=True)


class _ThrottledClient(_FakeClient):
    async def __call__(self, request: object) -> object:
        del request
        raise TelegramRpcThrottled(30)


class _RealtimePinClient(_FakeClient):
    def __init__(self, responses: list[object], db_path: Path, *, folder_id: int, dialog_id: int) -> None:
        super().__init__(responses)
        self._db_path = db_path
        self._folder_id = folder_id
        self._dialog_id = dialog_id
        self._published = False

    async def __call__(self, request: object) -> object:
        if not self._published and getattr(request, "folder_id", None) == self._folder_id:
            self._published = True
            conn = _open_sync_db(self._db_path)
            try:
                with conn:
                    conn.execute("INSERT INTO dialogs(dialog_id,type,hidden) VALUES (?,'user',0)", (self._dialog_id,))
                    conn.execute(
                        "INSERT INTO dialog_directory_published_pins(folder_id,dialog_id,position) VALUES (?,?,0)",
                        (self._folder_id, self._dialog_id),
                    )
                    sync_active_generation_pins_from_publication(conn, self._folder_id)
            finally:
                conn.close()
        return await super().__call__(request)


@pytest.mark.asyncio
async def test_throttle_reaches_coordinator_policy(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    with pytest.raises(TelegramRpcThrottled):
        await CanonicalDialogDirectory(_ThrottledClient([]), db_path, asyncio.Event()).run_slice()


@pytest.mark.asyncio
@pytest.mark.parametrize(("folder_id", "realtime_id"), [(0, 42), (1, 43)])
async def test_realtime_pins_win_over_an_overlapping_pending_pinned_rpc(
    tmp_path: Path, folder_id: int, realtime_id: int
) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    client = _RealtimePinClient(
        [_pinned_response([_dialog(1, 8)]), _pinned_response([_dialog(2, 9)]), _response([])],
        db_path,
        folder_id=folder_id,
        dialog_id=realtime_id,
    )
    directory = CanonicalDialogDirectory(client, db_path, asyncio.Event())

    await _run_slices(directory, 3)

    conn = _open_sync_db(db_path)
    try:
        assert conn.execute("SELECT status,retry_at FROM dialog_directory_state").fetchone() == ("complete", None)
        assert conn.execute(
            "SELECT dialog_id,position FROM dialog_directory_published_pins WHERE folder_id=?",
            (folder_id,),
        ).fetchall() == [(realtime_id, 0)]
        assert conn.execute("SELECT COUNT(*) FROM dialog_directory_pins").fetchone() == (0,)
    finally:
        conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(("folder_id", "realtime_id"), [(0, 52), (1, 53)])
async def test_existing_realtime_active_pins_are_replaced_by_unchanged_pinned_rpc(
    tmp_path: Path, folder_id: int, realtime_id: int
) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    directory = CanonicalDialogDirectory(_FakeClient([_pinned_response([_dialog(1, 8)])]), db_path, asyncio.Event())
    directory.bind_account_id(100)
    conn = _open_sync_db(db_path)
    try:
        with conn:
            CanonicalDialogDirectory._start_generation(conn, 1)
            conn.execute("INSERT INTO dialogs(dialog_id,type,hidden) VALUES (?,'user',0)", (realtime_id,))
            conn.execute(
                "INSERT INTO dialog_directory_published_pins(folder_id,dialog_id,position) VALUES (?,?,0)",
                (folder_id, realtime_id),
            )
            if folder_id == 1:
                conn.execute("UPDATE dialog_directory_state SET pinned_main_status='complete'")
            sync_active_generation_pins_from_publication(conn, folder_id)
    finally:
        conn.close()

    await directory.run_slice()

    conn = _open_sync_db(db_path)
    try:
        assert conn.execute(
            "SELECT dialog_id,position FROM dialog_directory_pins WHERE generation=1 AND folder_id=?",
            (folder_id,),
        ).fetchall() == [(1, 0)]
    finally:
        conn.close()


def test_realtime_mute_clear_fences_staged_directory_eligibility(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = _open_sync_db(db_path)
    try:
        conn.execute("INSERT INTO dialogs(dialog_id,type,hidden,revision) VALUES (1,'user',0,0)")
        conn.execute("INSERT INTO dialog_directory_facts VALUES (1,'contact',0,1,500,10)")
        conn.execute("INSERT INTO dialog_directory_baseline VALUES (1,1,0,1)")
        conn.execute(
            "INSERT INTO dialog_directory_staging(generation,dialog_id,source,peer_kind,top_message,name,type,archived,pinned,"
            "unread_mentions_count,unread_reactions_count,snapshot_at,identity_complete,identity_source,"
            "eligibility_category,eligibility_archived,eligibility_unread,eligibility_mute_until,eligibility_observed_at) "
            "VALUES (1,1,'ordinary','PeerUser',1,'One','user',0,0,0,0,20,1,'directory','contact',0,1,900,20)"
        )
        assert clear_realtime_mute(conn, 1, observed_at=30) == 1
        assert conn.execute("SELECT revision FROM dialogs WHERE dialog_id=1").fetchone() == (1,)
        assert conn.execute(
            "SELECT mute_until,observed_at FROM dialog_directory_facts WHERE dialog_id=1"
        ).fetchone() == (None, 10)
        conn.execute(
            "UPDATE dialog_directory_facts AS current SET mute_until=staged.eligibility_mute_until "
            "FROM dialog_directory_staging AS staged JOIN dialogs AS dialog ON dialog.dialog_id=staged.dialog_id "
            "WHERE staged.generation=1 AND staged.dialog_id=current.dialog_id AND staged.baseline_revision=dialog.revision"
        )
        assert conn.execute("SELECT mute_until FROM dialog_directory_facts WHERE dialog_id=1").fetchone() == (None,)
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_first_ordinary_page_is_productive_when_every_dialog_was_pinned(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    client = _FakeClient(
        [
            _pinned_response([_dialog(1, 8)]),
            _pinned_response(),
            _response([_dialog(1, 9)], terminal=False),
            _response([]),
        ]
    )
    directory = CanonicalDialogDirectory(client, db_path, asyncio.Event())

    await _run_slices(directory, 3)

    conn = _open_sync_db(db_path)
    try:
        assert conn.execute(
            "SELECT status,ordinary_status,reason,retry_at,offset_id FROM dialog_directory_state"
        ).fetchone() == (
            "in_progress",
            "pending",
            None,
            None,
            9,
        )
    finally:
        conn.close()
    await directory.run_slice()


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
async def test_duplicate_page_cools_down_both_aliases_then_resumes_from_its_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    now = 1_000
    monkeypatch.setattr("mcp_telegram.dialog_directory.time.time", lambda: now)
    client = _FakeClient(
        [
            _pinned_response(),
            _pinned_response(),
            _response([_dialog(1, 8)], terminal=False),
            _response([_dialog(1, 9)], terminal=False),
            _response([_dialog(2, 10)], terminal=False),
            _response([]),
        ]
    )
    directory = CanonicalDialogDirectory(client, db_path, asyncio.Event())
    await _run_slices(directory, 4)

    conn = _open_sync_db(db_path)
    try:
        assert conn.execute(
            "SELECT status,ordinary_status,offset_id,retry_at,reason FROM dialog_directory_state"
        ).fetchone() == (
            "incomplete",
            "incomplete",
            9,
            1900,
            "pagination_no_new_dialogs",
        )
        assert conn.execute("SELECT top_message FROM dialog_directory_staging WHERE dialog_id=1").fetchone() == (9,)
        assert CanonicalDialogDirectoryDemandAdapter(directory, conn).status(1001.0).release_at == 1900.0
    finally:
        conn.close()

    now = 1900
    restarted = CanonicalDialogDirectory(client, db_path, asyncio.Event())
    await restarted.run_slice()
    conn = _open_sync_db(db_path)
    try:
        assert conn.execute(
            "SELECT status,ordinary_status,offset_id,retry_at FROM dialog_directory_state"
        ).fetchone() == (
            "in_progress",
            "incomplete",
            10,
            None,
        )
        assert isinstance(client.requests[-1], functions.messages.GetDialogsRequest)
        assert client.requests[-1].offset_id == 9
        assert conn.execute("SELECT COUNT(*) FROM dialog_directory_staging").fetchone() == (2,)
    finally:
        conn.close()

    await restarted.run_slice()
    conn = _open_sync_db(db_path)
    try:
        assert conn.execute("SELECT status,retry_at FROM dialog_directory_state").fetchone() == ("complete", None)
        assert client.get_me_calls == 1
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_terminal_duplicate_completes_without_pagination_cooldown(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    client = _FakeClient(
        [
            _pinned_response(),
            _pinned_response(),
            _response([_dialog(1, 8)], terminal=False),
            _response([_dialog(1, 9)]),
        ]
    )
    directory = CanonicalDialogDirectory(client, db_path, asyncio.Event())
    await _run_slices(directory, 4)

    conn = _open_sync_db(db_path)
    try:
        assert conn.execute("SELECT status,reason,retry_at FROM dialog_directory_state").fetchone() == (
            "complete",
            None,
            None,
        )
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_pagination_cooldown_does_not_change_the_prior_publication_age(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    now = 100
    monkeypatch.setattr("mcp_telegram.dialog_directory.time.time", lambda: now)
    client = _FakeClient(
        [
            _pinned_response(),
            _pinned_response(),
            _response([_dialog(1, 8)]),
            _pinned_response(),
            _pinned_response(),
            _response([_dialog(1, 8)], terminal=False),
            _response([_dialog(1, 9)], terminal=False),
        ]
    )
    directory = CanonicalDialogDirectory(client, db_path, asyncio.Event())
    await _run_slices(directory, 3)
    now = 200
    await _run_slices(directory, 4)

    conn = _open_sync_db(db_path)
    try:
        assert conn.execute(
            "SELECT observation_started_at,observation_completed_at FROM dialog_directory_publication"
        ).fetchone() == (100, 100)
        assert conn.execute("SELECT status,retry_at FROM dialog_directory_state").fetchone() == ("incomplete", 1100)
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_duplicate_page_rollback_keeps_prior_cursor_and_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    client = _FakeClient(
        [
            _pinned_response(),
            _pinned_response(),
            _response([_dialog(1, 8)], terminal=False),
            _response([_dialog(1, 9)], terminal=False),
        ]
    )
    directory = CanonicalDialogDirectory(client, db_path, asyncio.Event())
    await _run_slices(directory, 3)

    def fail_cursor(*_args: object) -> None:
        raise RuntimeError("duplicate cursor commit failed")

    monkeypatch.setattr(directory, "_save_cursor", fail_cursor)
    with pytest.raises(RuntimeError, match="duplicate cursor"):
        await directory.run_slice()
    conn = _open_sync_db(db_path)
    try:
        assert conn.execute("SELECT status,offset_id,retry_at FROM dialog_directory_state").fetchone() == (
            "in_progress",
            8,
            None,
        )
        assert conn.execute("SELECT top_message FROM dialog_directory_staging WHERE dialog_id=1").fetchone() == (8,)
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_many_productive_pages_never_cool_down_or_repeat_account_identity(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    pages = [_response([_dialog(index, index)], terminal=False) for index in range(1, 35)]
    client = _FakeClient([_pinned_response(), _pinned_response(), *pages, _response([])])
    directory = CanonicalDialogDirectory(client, db_path, asyncio.Event())
    await _run_slices(directory, 37)

    conn = _open_sync_db(db_path)
    try:
        assert conn.execute("SELECT status,retry_at FROM dialog_directory_state").fetchone() == ("complete", None)
        assert conn.execute("SELECT COUNT(*) FROM dialogs").fetchone() == (34,)
        assert client.get_me_calls == 1
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
async def test_corrupt_persisted_cursor_latches_invalid_without_rpc(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = _open_sync_db(db_path)
    try:
        conn.execute(
            "UPDATE dialog_directory_state SET status='in_progress',ordinary_status='pending',"
            "offset_date='not-a-date',offset_id=1,offset_peer='{}'"
        )
        conn.commit()
    finally:
        conn.close()
    client = _FakeClient([])
    directory = CanonicalDialogDirectory(client, db_path, asyncio.Event())
    await directory.run_slice()
    conn = _open_sync_db(db_path)
    try:
        assert conn.execute("SELECT status,ordinary_status,reason,retry_at FROM dialog_directory_state").fetchone() == (
            "invalid",
            "invalid",
            "ordinary:invalid:corrupt_cursor",
            None,
        )
        assert client.requests == []
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_semantic_invalid_latches_without_rpc_or_automatic_recovery(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    client = _FakeClient(
        [
            _pinned_response(),
            _pinned_response(),
            _response([_dialog(1, 8)], terminal=False),
            _response([_dialog(1, 9), _dialog(1, 10)], terminal=False),
        ]
    )
    directory = CanonicalDialogDirectory(client, db_path, asyncio.Event())
    await _run_slices(directory, 4)

    conn = _open_sync_db(db_path)
    try:
        assert conn.execute(
            "SELECT status,ordinary_status,offset_id,retry_at,reason FROM dialog_directory_state"
        ).fetchone() == (
            "invalid",
            "invalid",
            8,
            None,
            "ordinary:invalid:conflicting_duplicate_dialog",
        )
        assert conn.execute("SELECT COUNT(*) FROM dialog_directory_staging").fetchone() == (1,)
        assert CanonicalDialogDirectoryDemandAdapter(directory, conn).status(9_999.0) is None
    finally:
        conn.close()

    await directory.run_slice()
    restarted = CanonicalDialogDirectory(client, db_path, asyncio.Event())
    await restarted.run_slice()
    assert len(client.requests) == 4
    restarted.recover_invalid_generation()
    conn = _open_sync_db(db_path)
    try:
        assert conn.execute("SELECT generation,status,retry_at FROM dialog_directory_state").fetchone() == (
            2,
            "in_progress",
            None,
        )
        assert conn.execute("SELECT COUNT(*) FROM dialog_directory_staging").fetchone() == (0,)
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_pinned_semantic_invalid_latches_its_source(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    directory = CanonicalDialogDirectory(_FakeClient([_response([])]), db_path, asyncio.Event())
    await directory.run_slice()
    conn = _open_sync_db(db_path)
    try:
        assert conn.execute(
            "SELECT status,pinned_main_status,retry_at,reason FROM dialog_directory_state"
        ).fetchone() == (
            "invalid",
            "invalid",
            None,
            "pinned_0:invalid:unexpected_pinned_response:Dialogs",
        )
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_not_modified_without_cache_is_honest_incomplete_with_900_second_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    monkeypatch.setattr("mcp_telegram.dialog_directory.time.time", lambda: 100)
    client = _FakeClient([_pinned_response(), _pinned_response(), types.messages.DialogsNotModified(count=1)])
    directory = CanonicalDialogDirectory(client, db_path, asyncio.Event())
    await _run_slices(directory, 3)
    conn = _open_sync_db(db_path)
    try:
        assert conn.execute("SELECT status,ordinary_status,reason,retry_at FROM dialog_directory_state").fetchone() == (
            "incomplete",
            "incomplete",
            "ordinary:not_modified_without_cache",
            1_000,
        )
    finally:
        conn.close()
    await directory.run_slice()
    assert len(client.requests) == 3


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
        assert conn.execute(
            "SELECT dialog_id,position FROM dialog_directory_published_pins WHERE folder_id=0 ORDER BY position"
        ).fetchall() == [
            (1, 0),
            (2, 1),
        ]
        assert conn.execute("SELECT COUNT(*) FROM dialog_directory_published_pins WHERE folder_id=1").fetchone() == (0,)
        assert conn.execute(
            "SELECT value FROM daemon_state WHERE key='dialog_unread_sweep_observed_count'"
        ).fetchone() == ("2",)
        assert conn.execute(
            "SELECT value FROM daemon_state WHERE key='dialog_unread_sweep_last_visible_count'"
        ).fetchone() == ("2",)
    finally:
        conn.close()

    with pytest.raises(RuntimeError, match="account identity changed"):
        directory.bind_account_id(101)


@pytest.mark.asyncio
async def test_unread_observation_metadata_preserves_then_replaces_as_one_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    now = 1_000
    monkeypatch.setattr("mcp_telegram.dialog_directory.time.time", lambda: now)
    client = _FakeClient(
        [
            _pinned_response(),
            _pinned_response(),
            _response([_dialog(1, 8)]),
            _pinned_response(),
            _pinned_response(),
            _response([]),
            _pinned_response(),
            _pinned_response(),
            _response([]),
        ]
    )
    directory = CanonicalDialogDirectory(client, db_path, asyncio.Event())

    await _run_slices(directory, 3)
    first = _stored_unread_receipt(db_path)
    assert first == {
        "dialog_unread_sweep_status": "complete",
        "dialog_unread_sweep_completed_at": "1000",
        "dialog_unread_sweep_observed_count": "1",
        "dialog_unread_sweep_last_visible_count": "1",
    }

    now = 2_000
    await _run_slices(directory, 1)
    conn = _open_sync_db(db_path)
    try:
        assert _unread_receipt(conn) == first
        assert conn.execute(
            "SELECT value FROM daemon_state WHERE key='dialog_unread_sweep_attempted_at'"
        ).fetchone() == ("2000",)
    finally:
        conn.close()
    await _run_slices(directory, 2)

    second = _stored_unread_receipt(db_path)
    assert second == {
        "dialog_unread_sweep_status": "complete",
        "dialog_unread_sweep_completed_at": "2000",
        "dialog_unread_sweep_observed_count": "0",
        "dialog_unread_sweep_last_visible_count": "0",
    }

    now = 3_000
    await _run_slices(directory, 2)
    conn = _open_sync_db(db_path)
    try:
        conn.execute(
            "CREATE TRIGGER fail_unread_receipt BEFORE INSERT ON daemon_state "
            "WHEN NEW.key='dialog_unread_sweep_status' BEGIN SELECT RAISE(ABORT, 'metadata failure'); END"
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(sqlite3.IntegrityError, match="metadata failure"):
        await directory.run_slice()
    assert _stored_unread_receipt(db_path) == second


@pytest.mark.asyncio
async def test_cross_source_observations_replace_mutable_facts(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    dialog = _dialog(1, 8)
    pinned = types.messages.PeerDialogs(
        dialogs=[dialog],
        messages=[types.Message(id=8, peer_id=dialog.peer, date=datetime(2026, 1, 2, tzinfo=UTC))],
        chats=[],
        users=[types.User(id=1, first_name="One", access_hash=42)],
        state=types.updates.State(pts=0, qts=0, date=None, seq=0, unread_count=0),
    )
    later_dialog = _dialog(1, 7)
    ordinary = types.messages.Dialogs(
        dialogs=[later_dialog],
        messages=[],
        chats=[],
        users=[types.User(id=1, first_name="One", access_hash=42, bot=True)],
    )
    client = _FakeClient([pinned, _pinned_response(), ordinary])
    directory = CanonicalDialogDirectory(client, db_path, asyncio.Event())
    await directory.run_slice()
    await directory.run_slice()
    await directory.run_slice()

    conn = _open_sync_db(db_path)
    try:
        assert conn.execute("SELECT status FROM dialog_directory_state").fetchone() == ("complete",)
        assert conn.execute("SELECT type,last_message_at,pinned FROM dialogs WHERE dialog_id=1").fetchone() == (
            "bot",
            None,
            1,
        )
        assert conn.execute("SELECT dialog_id,position FROM dialog_directory_published_pins").fetchone() == (1, 0)
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
        assert status.is_ready(1001.0)
        assert status.overdue_seconds(1001.0) == 1.0
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_long_acquisition_and_restart_keep_the_original_freshness_age(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    now = 100
    monkeypatch.setattr("mcp_telegram.dialog_directory.time.time", lambda: now)
    directory = CanonicalDialogDirectory(
        _FakeClient([_pinned_response(), _pinned_response(), _response([])]), db_path, asyncio.Event()
    )

    await directory.run_slice()
    now = 1_001
    await directory.run_slice()
    await directory.run_slice()

    conn = _open_sync_db(db_path)
    try:
        status = directory.status(float(now), conn)
        assert status is not None
        assert status.release_at == 1000.0
        assert status.overdue_seconds(float(now)) == 1.0
        restarted = CanonicalDialogDirectory(_FakeClient([]), db_path, asyncio.Event())
        restarted_status = restarted.status(float(now), conn)
        assert restarted_status == status
    finally:
        conn.close()


def test_identity_and_eligibility_extract_only_authoritative_facts() -> None:
    assert _eligibility_category(types.User(id=1, bot=True, contact=True)) == "bot"
    assert _eligibility_category(types.User(id=2, contact=False)) == "non_contact"
    assert _eligibility_category(types.User(id=3, min=True, contact=True)) is None
    assert _eligibility_category(types.User(id=4)) is None
    assert (
        _eligibility_category(types.Chat(id=5, title="group", photo=None, participants_count=1, date=None, version=1))
        == "group"
    )
    assert _eligibility_category(types.Channel(id=6, title="group", photo=None, date=None, megagroup=True)) == "group"
    assert (
        _eligibility_category(types.Channel(id=7, title="feed", photo=None, date=None, broadcast=True)) == "broadcast"
    )
    assert _eligibility_category(types.Channel(id=8, title="unknown", photo=None, date=None)) is None

    assert (
        _three_valued_unread(
            types.Dialog(
                peer=types.PeerUser(1),
                top_message=1,
                read_inbox_max_id=0,
                read_outbox_max_id=0,
                unread_count=0,
                unread_mentions_count=0,
                unread_reactions_count=9,
                unread_poll_votes_count=0,
                notify_settings=None,
                unread_mark=False,
            )
        )
        == 0
    )
    assert (
        _three_valued_unread(
            types.Dialog(
                peer=types.PeerUser(1),
                top_message=1,
                read_inbox_max_id=0,
                read_outbox_max_id=0,
                unread_count=0,
                unread_mentions_count=1,
                unread_reactions_count=0,
                unread_poll_votes_count=0,
                notify_settings=None,
            )
        )
        == 1
    )
    assert _mute_until(None) is None
    assert _mute_until(types.PeerNotifySettings(mute_until=datetime(2026, 1, 1, tzinfo=UTC))) == 1_767_225_600
    assert _mute_until(type("Settings", (), {"mute_until": 0})()) == 0


def test_realtime_bundles_preserve_oldest_boundary_and_complete_identity_replaces(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = _open_sync_db(db_path)
    try:
        conn.execute(
            "INSERT INTO dialogs(dialog_id,name,type,username,identity_observed_at,identity_complete,identity_source) "
            "VALUES (1,'Old','user','old',10,1,'directory')"
        )
        with conn:
            assert apply_realtime_eligibility(conn, 1, category="contact", archived=0, observed_at=20) == 1
            assert apply_realtime_eligibility(conn, 1, unread=1, observed_at=30) == 1
            assert (
                apply_realtime_identity(
                    conn, 1, name="Renamed", username=None, dialog_type="user", observed_at=40, complete=True
                )
                == 1
            )
        assert conn.execute(
            "SELECT name,username,identity_observed_at,identity_complete,identity_source FROM dialogs WHERE dialog_id=1"
        ).fetchone() == ("Renamed", None, 40, 1, "realtime")
        assert conn.execute(
            "SELECT category,archived,unread,observed_at FROM dialog_directory_facts WHERE dialog_id=1"
        ).fetchone() == ("contact", 0, 1, 20)
    finally:
        conn.close()


def test_partial_realtime_identity_omission_preserves_values_and_explicit_removal_clears(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = _open_sync_db(db_path)
    try:
        conn.execute(
            "INSERT INTO dialogs(dialog_id,name,type,username,identity_observed_at,identity_complete,identity_source) "
            "VALUES (1,'Known','user','known',10,1,'directory')"
        )
        with conn:
            assert apply_realtime_identity(conn, 1, observed_at=20, complete=False) == 0
            assert (
                apply_realtime_identity(
                    conn, 1, name=None, username=None, dialog_type=None, observed_at=30, complete=False
                )
                == 1
            )
        assert conn.execute(
            "SELECT name,username,type,identity_observed_at,identity_source FROM dialogs WHERE dialog_id=1"
        ).fetchone() == (None, None, "user", 10, "mixed")
    finally:
        conn.close()


def test_realtime_identity_presence_unhides_catalog_row_without_reviving_access_loss(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = _open_sync_db(db_path)
    try:
        conn.executemany(
            "INSERT INTO dialogs(dialog_id,name,type,identity_complete,hidden) VALUES (?,?,?,?,1)",
            [(1, "Known", "user", 0), (2, "Lost", "user", 0)],
        )
        conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES (2,'access_lost')")
        with conn:
            assert apply_realtime_identity(conn, 1, name="Renamed", observed_at=20, complete=False) == 1
            assert apply_realtime_identity(conn, 2, name="Renamed", observed_at=20, complete=False) == 1
        assert conn.execute("SELECT hidden FROM dialogs WHERE dialog_id=1").fetchone() == (0,)
        assert conn.execute("SELECT hidden FROM dialogs WHERE dialog_id=2").fetchone() == (1,)
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_realtime_eligibility_fences_an_older_directory_publication(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = _open_sync_db(db_path)
    try:
        conn.execute("INSERT INTO dialogs(dialog_id,name,type,snapshot_at,hidden) VALUES (1,'Old','user',1,0)")
        conn.commit()
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_incomplete_directory_identity_keeps_known_username_and_oldest_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = _open_sync_db(db_path)
    try:
        conn.execute(
            "INSERT INTO dialogs(dialog_id,name,type,username,identity_observed_at,identity_complete,identity_source,hidden) "
            "VALUES (1,'Known','user','known',10,1,'directory',0)"
        )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr("mcp_telegram.dialog_directory.time.time", lambda: 20)
    dialog = _dialog(1, 8)
    response = types.messages.Dialogs(
        dialogs=[dialog],
        messages=[types.Message(id=8, peer_id=types.PeerUser(1), date=datetime(2026, 1, 1, tzinfo=UTC))],
        chats=[],
        users=[types.User(id=1, first_name="Partial", username="partial", min=True)],
    )
    directory = CanonicalDialogDirectory(
        _FakeClient([_pinned_response(), _pinned_response(), response]), db_path, asyncio.Event()
    )
    await _run_slices(directory, 3)
    conn = _open_sync_db(db_path)
    try:
        assert conn.execute(
            "SELECT name,username,identity_observed_at,identity_complete,identity_source FROM dialogs WHERE dialog_id=1"
        ).fetchone() == ("Known", "known", 10, 1, "mixed")
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_complete_directory_absence_hides_row_and_removes_current_eligibility(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    directory = CanonicalDialogDirectory(
        _FakeClient(
            [
                _pinned_response(),
                _pinned_response(),
                _response([_dialog(1, 8)]),
                _pinned_response(),
                _pinned_response(),
                _response([]),
            ]
        ),
        db_path,
        asyncio.Event(),
    )
    await _run_slices(directory, 3)
    conn = _open_sync_db(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM dialog_directory_facts WHERE dialog_id=1").fetchone() == (1,)
    finally:
        conn.close()
    await _run_slices(directory, 3)
    conn = _open_sync_db(db_path)
    try:
        assert conn.execute("SELECT hidden FROM dialogs WHERE dialog_id=1").fetchone() == (1,)
        assert conn.execute("SELECT COUNT(*) FROM dialog_directory_facts WHERE dialog_id=1").fetchone() == (0,)
    finally:
        conn.close()
    directory = CanonicalDialogDirectory(
        _FakeClient([_pinned_response(), _pinned_response(), _response([_dialog(1, 8)])]), db_path, asyncio.Event()
    )
    await directory.run_slice()
    await directory.run_slice()
    conn = _open_sync_db(db_path)
    try:
        with conn:
            apply_realtime_eligibility(conn, 1, category="bot", unread=1, observed_at=50)
    finally:
        conn.close()
    await directory.run_slice()
    conn = _open_sync_db(db_path)
    try:
        assert conn.execute(
            "SELECT category,unread,observed_at FROM dialog_directory_facts WHERE dialog_id=1"
        ).fetchone() == (
            "bot",
            1,
            50,
        )
    finally:
        conn.close()
