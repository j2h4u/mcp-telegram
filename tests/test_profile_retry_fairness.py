"""Released profile retries share the oldest-work queue with pending pages."""

import sqlite3

from mcp_telegram.entity_profile.repository import EntityRefreshCursor
from mcp_telegram.sync_transactions import write_transaction
from tests.test_profile_collection_pages import Database, _commit, _item, _page
from tests.test_profile_collection_pages import database as database


def _pending(conn: sqlite3.Connection, entity_id: int, now: int) -> None:
    with write_transaction(conn):
        conn.execute("INSERT INTO entities(id,type,updated_at) VALUES(?,'user',?)", (entity_id, now))
        conn.execute(
            "INSERT INTO entity_profile_refresh_state(entity_id,status,updated_at,next_section,generation) "
            "VALUES(?,'pending',?,'avatar_history',1)",
            (entity_id, now),
        )


def test_released_retry_resumes_before_newer_pending_and_rotates_after_page(database: Database) -> None:
    _path, conn, repo = database
    with write_transaction(conn):
        assert _commit(repo, _page(_item("first"), count=3, cursor={"offset": 1}), 100)
    cursor = repo.next_due_refresh(now=110)
    assert cursor is not None
    assert repo.mark_section_failure(cursor, now=110, reason="timeout", retry_at=200)
    saved = repo.next_due_refresh(now=200)
    assert saved is not None
    assert saved.next_section == "avatar_history"
    assert saved.page_cursor_json is not None
    assert saved.generation == 1
    assert repo.next_due_refresh(now=199) is None
    assert repo.next_refresh_release_at() == 200

    _pending(conn, 99, 120)
    assert repo.next_due_refresh(now=199) == EntityRefreshCursor(99, "avatar_history", 0, None, generation=1)
    assert repo.next_refresh_release_at() == 0
    assert repo.next_due_refresh(now=200) == saved
    _pending(conn, 100, 201)
    assert repo.next_due_refresh(now=201) == saved

    with write_transaction(conn):
        assert _commit(repo, _page(_item("second"), count=3, cursor={"offset": 2}), 202)
    next_cursor = repo.next_due_refresh(now=202)
    assert next_cursor is not None and next_cursor.entity_id == 99
    assert repo.mark_section_failure(next_cursor, now=203, reason="timeout", retry_at=203)
    next_cursor = repo.next_due_refresh(now=203)
    assert next_cursor is not None and next_cursor.entity_id == 100
    repo.mark_refresh_not_found(100, now=204)
    resumed = repo.next_due_refresh(now=204)
    assert resumed is not None and resumed.entity_id == 42
    assert resumed.generation == saved.generation
    assert resumed.acquisition_cursor == saved.acquisition_cursor
    assert resumed.page_cursor_json != saved.page_cursor_json


def test_due_work_ties_use_entity_id_and_terminal_page_releases_turn(database: Database) -> None:
    _path, conn, repo = database
    _pending(conn, 99, 100)
    _pending(conn, 77, 100)
    assert repo.mark_section_failure(
        EntityRefreshCursor(42, "avatar_history", 0, None, generation=1), now=100, reason="timeout", retry_at=200
    )
    assert repo.next_due_refresh(now=199) == EntityRefreshCursor(77, "avatar_history", 0, None, generation=1)
    cursor = repo.next_due_refresh(now=200)
    assert cursor is not None and cursor.entity_id == 42
    with write_transaction(conn):
        assert _commit(repo, _page(terminal=True, count=0), 200)
    next_cursor = repo.next_due_refresh(now=200)
    assert next_cursor is not None and next_cursor.entity_id == 77
    assert repo.mark_section_failure(next_cursor, now=201, reason="timeout", retry_at=201)
    next_cursor = repo.next_due_refresh(now=201)
    assert next_cursor is not None and next_cursor.entity_id == 99
