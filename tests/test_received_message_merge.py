"""A bounded response cannot outrun current canonical message evidence."""

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from telethon.tl import types

from mcp_telegram.auth_scope import AUTH_SCOPE_VERSION, TelegramAuthScope
from mcp_telegram.image_authorization import ScopeCell
from mcp_telegram.messages.sqlite_bundle import insert_messages_with_fts, persist_received_messages
from mcp_telegram.messages.telegram_adapter import extract_message_row, extract_received_messages
from mcp_telegram.observation_order import allocate_observation_order
from mcp_telegram.reactions.contracts import ReactionAggregate, ReactionAggregateSource
from mcp_telegram.reactions.persistence import allocate_observation_boundary, replace_reaction_aggregates
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.sync_transactions import write_transaction


def message(text: str, edit: int | None) -> types.Message:
    return types.Message(
        id=7,
        peer_id=types.PeerUser(55),
        date=datetime.fromtimestamp(10, UTC),
        message=text,
        edit_date=None if edit is None else datetime.fromtimestamp(edit, UTC),
    )


@pytest.mark.parametrize(
    ("event_edit", "response_edit", "expected", "independent_media"),
    [(None, 40, "new-event", True), (20, 40, "new-response", True), (20, 40, "new-response", False)],
)
def test_received_body_respects_later_event_and_comparable_version(
    tmp_path: Path, event_edit: int | None, response_edit: int, expected: str, independent_media: bool
) -> None:
    path = tmp_path / "state.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    cell = ScopeCell()
    assert cell.publish(TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99))
    token = cell.capture()
    assert token is not None
    with write_transaction(conn):
        conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
        conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES (55,'synced')")
        conn.execute("INSERT INTO full_history_enrollment(dialog_id,enabled,updated_at) VALUES (55,1,0)")
        initial = replace(
            extract_message_row(55, message("old", None if event_edit is None else 10)),
            observation_order=allocate_observation_order(conn),
        )
        insert_messages_with_fts(conn, [initial])
        request_order = allocate_observation_order(conn)
        reaction_boundary = allocate_observation_boundary(conn, ReactionAggregateSource.HISTORY)
        event_message = message("new-event", event_edit)
        event_message.media = types.MessageMediaContact("123", "Name", "", "", 7)
        event = replace(
            extract_message_row(55, event_message),
            observation_order=allocate_observation_order(conn),
        )
        insert_messages_with_fts(conn, [event], reaction_source="message_edit")
        later_media_order = allocate_observation_order(conn) if independent_media else event.observation_order
        if independent_media:
            conn.execute(
                "UPDATE message_image_sources SET observation_order=? WHERE dialog_id=55 AND message_id=7",
                (later_media_order,),
            )
        replace_reaction_aggregates(
            conn, 55, 7, [ReactionAggregate("👍", 3)], source=ReactionAggregateSource.RAW_UPDATE
        )
        response = message("new-response", response_edit)
        response.reactions = types.MessageReactions([types.ReactionCount(types.ReactionEmoji("🔥"), 1)])
        persist_received_messages(
            conn,
            extract_received_messages([response]),
            scope=token,
            claim_scope=cell.claim,
            observation_order=request_order,
            expected_dialog_id=55,
            reaction_boundary=reaction_boundary,
        )
    assert conn.execute("SELECT text FROM messages WHERE dialog_id=55 AND message_id=7").fetchone() == (expected,)
    assert conn.execute(
        "SELECT observation_order FROM message_observations WHERE dialog_id=55 AND message_id=7"
    ).fetchone() == (event.observation_order,)
    assert conn.execute("SELECT media_kind FROM messages WHERE dialog_id=55 AND message_id=7").fetchone() == (
        "contact" if independent_media else None,
    )
    assert conn.execute(
        "SELECT observation_order FROM message_image_sources WHERE dialog_id=55 AND message_id=7"
    ).fetchone() == (later_media_order,)
    assert conn.execute("SELECT emoji,count FROM message_reactions WHERE dialog_id=55 AND message_id=7").fetchall() == [
        ("👍", 3)
    ]
    conn.close()


@pytest.mark.parametrize("direction", [False, None, True])
def test_own_only_never_imports_incoming_or_reclassifies_existing_row(tmp_path: Path, direction: bool | None) -> None:
    path = tmp_path / "scope.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    cell = ScopeCell()
    assert cell.publish(TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99))
    token = cell.capture()
    assert token is not None
    raw = message("response", 20)
    raw.out = direction
    with write_transaction(conn):
        conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
        conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES (55,'own_only')")
        conn.execute("INSERT INTO messages(dialog_id,message_id,sent_at,text,out) VALUES(55,7,10,'incoming',0)")
        order = allocate_observation_order(conn)
        assert (
            persist_received_messages(
                conn,
                extract_received_messages([raw]),
                scope=token,
                claim_scope=cell.claim,
                observation_order=order,
                expected_dialog_id=55,
            )
            == 0
        )
    assert conn.execute("SELECT text,out FROM messages WHERE dialog_id=55 AND message_id=7").fetchone() == (
        "incoming",
        0,
    )
    conn.close()


def test_original_scope_generation_fences_entire_response_transaction(tmp_path: Path) -> None:
    from mcp_telegram.messages.sqlite_bundle import ReceivedMessageScopeChangedError

    path = tmp_path / "scope.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    cell = ScopeCell()
    scope = TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99)
    assert cell.publish(scope)
    token = cell.capture()
    assert token is not None
    with write_transaction(conn):
        conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
        conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES (55,'synced')")
        conn.execute("INSERT INTO full_history_enrollment(dialog_id,enabled,updated_at) VALUES(55,1,0)")
        order = allocate_observation_order(conn)
    calls = 0

    def claim(value: object) -> bool:
        nonlocal calls
        calls += 1
        if calls == 2:
            change = cell.begin_change()
            cell.end_change(change)
            assert cell.publish(scope)
        return cell.claim(token)

    with pytest.raises(ReceivedMessageScopeChangedError), write_transaction(conn):
        persist_received_messages(
            conn,
            extract_received_messages([message("not committed", 20)]),
            scope=token,
            claim_scope=claim,
            observation_order=order,
            expected_dialog_id=55,
        )
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone() == (0,)
    conn.close()


def test_access_lost_accepts_only_exact_committed_recovery_witness(tmp_path: Path) -> None:
    from mcp_telegram.access_lifecycle import record_access_recovery_observation

    path = tmp_path / "access.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    cell = ScopeCell()
    assert cell.publish(TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99))
    token = cell.capture()
    assert token is not None
    with write_transaction(conn):
        conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
        conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES(55,'access_lost')")
        conn.execute("INSERT INTO full_history_enrollment(dialog_id,enabled,updated_at) VALUES(55,1,0)")
        order = allocate_observation_order(conn)
        normalized = extract_received_messages([message("revalidated", 20)])
        assert (
            persist_received_messages(
                conn,
                normalized,
                scope=token,
                claim_scope=cell.claim,
                observation_order=order,
                expected_dialog_id=55,
                revalidated_access=True,
            )
            == 0
        )
        assert record_access_recovery_observation(conn, 55, order)
        assert (
            persist_received_messages(
                conn,
                normalized,
                scope=token,
                claim_scope=cell.claim,
                observation_order=order,
                expected_dialog_id=55,
                revalidated_access=True,
            )
            == 1
        )
    assert conn.execute("SELECT text FROM messages WHERE dialog_id=55").fetchone() == ("revalidated",)
    assert conn.execute("SELECT status FROM synced_dialogs WHERE dialog_id=55").fetchone() == ("access_lost",)
    conn.close()


def test_response_batch_keeps_valid_service_while_rejecting_wrong_peer(tmp_path: Path) -> None:
    path = tmp_path / "batch.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    cell = ScopeCell()
    assert cell.publish(TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99))
    token = cell.capture()
    assert token is not None
    service = types.MessageService(
        id=9,
        peer_id=types.PeerUser(55),
        date=datetime.fromtimestamp(10, UTC),
        action=types.MessageActionChatCreate("group", []),
    )
    wrong = types.Message(id=10, peer_id=types.PeerUser(56), date=datetime.fromtimestamp(10, UTC), message="wrong")
    with write_transaction(conn):
        conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
        conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES (55,'synced'),(56,'synced')")
        conn.execute("INSERT INTO full_history_enrollment(dialog_id,enabled,updated_at) VALUES(55,1,0),(56,1,0)")
        order = allocate_observation_order(conn)
        boundary = allocate_observation_boundary(conn, ReactionAggregateSource.HISTORY)
        count = persist_received_messages(
            conn,
            extract_received_messages([wrong, service, types.MessageEmpty(11, types.PeerUser(55))]),
            scope=token,
            claim_scope=cell.claim,
            observation_order=order,
            expected_dialog_id=55,
            reaction_boundary=boundary,
        )
    assert count == 1
    assert conn.execute("SELECT dialog_id,message_id,is_service FROM messages").fetchall() == [(55, 9, 1)]
    conn.close()


def test_own_only_accepts_new_canonical_outgoing_message(tmp_path: Path) -> None:
    path = tmp_path / "outgoing.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    cell = ScopeCell()
    assert cell.publish(TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99))
    token = cell.capture()
    assert token is not None
    raw = message("outgoing", 20)
    raw.out = True
    with write_transaction(conn):
        conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
        conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES (55,'own_only')")
        order = allocate_observation_order(conn)
        boundary = allocate_observation_boundary(conn, ReactionAggregateSource.HISTORY)
        assert (
            persist_received_messages(
                conn,
                extract_received_messages([raw]),
                scope=token,
                claim_scope=cell.claim,
                observation_order=order,
                expected_dialog_id=55,
                reaction_boundary=boundary,
            )
            == 1
        )
    assert conn.execute("SELECT text,out FROM messages WHERE dialog_id=55 AND message_id=7").fetchone() == (
        "outgoing",
        1,
    )
    conn.close()
