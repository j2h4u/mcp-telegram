"""Scope and source claims around real canonical SQLite mutations."""

import threading
from dataclasses import replace
from pathlib import Path

from mcp_telegram.auth_scope import TelegramAuthScope
from mcp_telegram.image_authorization import ActiveSources, ScopeCell
from mcp_telegram.messages.sqlite_bundle import (
    insert_messages_with_fts,
    record_message_deletions,
    record_nonchannel_deletions,
)
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema
from mcp_telegram.sync_transactions import enable_runtime_writes, write_transaction
from tests.test_message_sqlite_persistence import _message


def test_scope_aba_nested_transitions_and_foreign_cell() -> None:
    scope = TelegramAuthScope(1, 42, 2, 17)
    cell = ScopeCell()
    assert cell.publish(scope)
    token = cell.capture()
    assert token is not None and cell.claim(token)
    other = ScopeCell()
    other.publish(scope)
    assert not other.claim(token)
    first, second = cell.begin_change(), cell.begin_change()
    assert not cell.claim(token) and cell.capture() is None
    cell.end_change(first)
    assert not cell.publish(scope) and cell.capture() is None
    cell.end_change(second)
    assert cell.publish(scope) and not cell.claim(token)
    fresh = cell.capture()
    assert fresh is not None and cell.claim(fresh)


def test_scope_claim_is_linearization_not_lock_held_through_work() -> None:
    cell = ScopeCell()
    scope = TelegramAuthScope(1, 42, 2, 17)
    cell.publish(scope)
    token = cell.capture()
    assert token is not None
    claimed, release = threading.Event(), threading.Event()
    result: list[bool] = []

    def writer() -> None:
        result.append(cell.claim(token))
        claimed.set()
        assert release.wait(2)

    thread = threading.Thread(target=writer)
    thread.start()
    assert claimed.wait(2)
    change = cell.begin_change()
    assert not cell.claim(token)
    cell.end_change(change)
    cell.publish(scope)
    release.set()
    thread.join(2)
    assert result == [True] and not thread.is_alive()
    assert not cell.claim(token)


def test_real_canonical_hooks_are_connection_scoped_and_ignore_rejected_edits(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = _open_sync_db(path)
    other = _open_sync_db(path)
    enable_runtime_writes(conn)
    sources = ActiveSources()
    sources.bind(conn)
    try:
        original = _message(5, text="current")
        original = replace(original, message=replace(original.message, edit_date=10))
        with write_transaction(conn):
            insert_messages_with_fts(conn, [original])
        stale = sources.register(42, 42, 5, 1)
        old = replace(original, message=replace(original.message, edit_date=9))
        with write_transaction(conn):
            assert insert_messages_with_fts(conn, [old]) == 0
        assert stale.is_current()
        with write_transaction(other):
            record_message_deletions(other, 42, [7])
        assert stale.is_current()
        with write_transaction(conn):
            insert_messages_with_fts(conn, [replace(original, message=replace(original.message, edit_date=11))])
            assert not stale.is_current()
        assert not stale.is_current()
        deleted = sources.register(42, 42, 5, 1)
        with write_transaction(conn):
            record_message_deletions(conn, 42, [5])
            assert not deleted.is_current()
        assert not deleted.is_current()
        nonchannel = sources.register(42, 42, 12, 1)
        channel = sources.register(42, -1000000000123, 12, 1)
        with write_transaction(conn):
            record_nonchannel_deletions(conn, [12], 100)
        assert not nonchannel.is_current() and channel.is_current()
    finally:
        sources.unbind(conn)
        conn.close()
        other.close()


def test_replacement_revoke_attempt_and_detach() -> None:
    import sqlite3

    conn = sqlite3.connect(":memory:")
    sources = ActiveSources()
    sources.bind(conn)
    first = sources.register(1, -1, 2, 3)
    second = sources.register(1, -1, 2, 4)
    assert not first.is_current() and second.is_current()
    sources.revoke(1, -1, 2, 3)
    assert second.is_current()
    sources.unbind(conn)
    assert not second.is_current()
    conn.close()


def test_rotation_before_queued_claim_rejects_writer() -> None:
    scope = TelegramAuthScope(1, 42, 2, 17)
    cell = ScopeCell()
    cell.publish(scope)
    token = cell.capture()
    assert token is not None
    queued, resume = threading.Event(), threading.Event()
    result: list[bool] = []

    def writer() -> None:
        queued.set()
        assert resume.wait(2)
        result.append(cell.claim(token))

    thread = threading.Thread(target=writer)
    thread.start()
    assert queued.wait(2)
    change = cell.begin_change()
    cell.end_change(change)
    cell.publish(scope)
    resume.set()
    thread.join(2)
    assert result == [False] and not thread.is_alive()


def test_hydrated_media_revokes_only_accepted_change_before_commit(tmp_path: Path) -> None:
    from mcp_telegram.messages.sqlite_hydration import apply_hydrated_media_fact

    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = _open_sync_db(path)
    enable_runtime_writes(conn)
    sources = ActiveSources()
    sources.bind(conn)
    try:
        with write_transaction(conn):
            conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES (42,'synced')")
            conn.execute(
                "INSERT INTO full_history_enrollment(dialog_id,enabled,source,updated_at) VALUES (42,1,'explicit',1)"
            )
            insert_messages_with_fts(
                conn, [replace(_message(8, text=None, media_kind="other", media_payload="{}"), observation_order=20)]
            )
        lease = sources.register(42, 42, 8, 1)
        with write_transaction(conn):
            assert not apply_hydrated_media_fact(conn, 42, 8, "photo", "{}", observation_order=10)
            assert lease.is_current()
            assert apply_hydrated_media_fact(conn, 42, 8, "other", "{}", observation_order=20)
            assert lease.is_current()
            assert apply_hydrated_media_fact(conn, 42, 8, "photo", "{}", observation_order=20)
            assert not lease.is_current()
        assert conn.execute("SELECT media_kind FROM messages WHERE dialog_id=42 AND message_id=8").fetchone() == (
            "photo",
        )
    finally:
        sources.unbind(conn)
        conn.close()
