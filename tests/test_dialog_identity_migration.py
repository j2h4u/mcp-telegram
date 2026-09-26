from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from telethon.tl import functions, types  # type: ignore[import-untyped]

from mcp_telegram.dialog_directory import CanonicalDialogDirectory
from mcp_telegram.sync_db import (
    _DIALOG_DIRECTORY_STATE_DDL,
    _DIALOGS_V74_DDL,
    _apply_migration_78,
    _apply_migrations,
    _open_sync_db,
    ensure_sync_schema,
)


def _create_paused_v77_database(db_path: Path) -> None:
    ensure_sync_schema(db_path)
    conn = _open_sync_db(db_path)
    try:
        triggers = cast(
            list[tuple[str, str]],
            conn.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name='dialogs' AND sql IS NOT NULL"
            ).fetchall(),
        )
        for name, _ in triggers:
            conn.execute(f"DROP TRIGGER {name}")
        conn.execute(_DIALOGS_V74_DDL)
        column_rows = cast(list[tuple[object, str]], conn.execute("PRAGMA table_info(dialogs_v74)").fetchall())
        old_columns = [row[1] for row in column_rows]
        conn.execute(f"INSERT INTO dialogs_v74({','.join(old_columns)}) SELECT {','.join(old_columns)} FROM dialogs")
        conn.execute("PRAGMA legacy_alter_table=ON")
        conn.execute("DROP TABLE dialogs")
        conn.execute("ALTER TABLE dialogs_v74 RENAME TO dialogs")
        conn.execute("PRAGMA legacy_alter_table=OFF")
        for _, statement in triggers:
            conn.execute(statement)
        conn.execute("DROP TABLE dialog_directory_baseline")
        conn.execute(
            "CREATE TABLE dialog_directory_baseline(generation INTEGER,dialog_id INTEGER,"
            "baseline_revision INTEGER NOT NULL,seen INTEGER NOT NULL DEFAULT 0,"
            "PRIMARY KEY(generation,dialog_id)) WITHOUT ROWID"
        )
        conn.execute(
            "UPDATE dialog_directory_state SET account_id=100,generation=9,status='incomplete',"
            "ordinary_status='incomplete',pinned_main_status='complete',pinned_archive_status='complete',"
            "offset_id=55,offset_date=?,offset_peer=?,observed_count=1,retry_at=NULL",
            ("2025-01-01T00:00:00+00:00", '{"kind":"user","id":54,"access_hash":42}'),
        )
        conn.execute("INSERT INTO dialog_directory_baseline VALUES (9,99,0,1)")
        conn.execute(
            "INSERT INTO dialog_directory_staging(generation,dialog_id,source,peer_kind,top_message,type,"
            "archived,pinned,snapshot_at) VALUES (9,99,'ordinary','PeerUser',7,'user',0,0,123)"
        )
        conn.execute("INSERT INTO dialog_directory_pins(generation,folder_id,dialog_id,position) VALUES (9,0,99,0)")
        conn.execute("DELETE FROM schema_version WHERE version=78")
        conn.commit()
    finally:
        conn.close()


def _migrate_and_assert_fresh_directory_generation(db_path: Path) -> None:
    conn = _open_sync_db(db_path)
    try:
        assert _apply_migration_78(conn, 77) == 78
        state = cast(
            tuple[str, str, str, str, int, str | None],
            conn.execute(
                "SELECT status,ordinary_status,pinned_main_status,pinned_archive_status,offset_id,offset_peer "
                "FROM dialog_directory_state WHERE singleton=1"
            ).fetchone(),
        )
        assert state == ("pending", "pending", "pending", "pending", 0, None)
        for table in ("dialog_directory_staging", "dialog_directory_baseline", "dialog_directory_pins"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)
    finally:
        conn.close()


def test_fresh_schema_has_separate_identity_fence_and_complete_presence_trigger(request: pytest.FixtureRequest) -> None:
    conn = sqlite3.connect(":memory:")
    request.addfinalizer(conn.close)
    _apply_migrations(conn)
    version_row = cast(tuple[int], conn.execute("SELECT MAX(version) FROM schema_version").fetchone())
    assert version_row[0] == 78
    dialog_columns = cast(list[tuple[object, str]], conn.execute("PRAGMA table_info(dialogs)").fetchall())
    columns = {row[1] for row in dialog_columns}
    assert "identity_revision" in columns
    trigger_row = cast(
        tuple[str],
        conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='dialogs_revision_after_update'"
        ).fetchone(),
    )
    trigger = trigger_row[0]
    update_columns = trigger.split("UPDATE OF", 1)[1].split("ON dialogs", 1)[0]
    captured = {value.strip() for value in update_columns.replace("\n", "").split(",")}
    identity = {
        "name",
        "type",
        "username",
        "identity_observed_at",
        "identity_complete",
        "identity_source",
        "identity_revision",
    }
    mutable = columns - {"dialog_id", "revision"} - identity
    assert captured == mutable
    baseline_columns = cast(
        list[tuple[object, str]], conn.execute("PRAGMA table_info(dialog_directory_baseline)").fetchall()
    )
    staging_columns = cast(
        list[tuple[object, str]], conn.execute("PRAGMA table_info(dialog_directory_staging)").fetchall()
    )
    assert "baseline_identity_revision" in {row[1] for row in baseline_columns}
    assert "identity_fields_observed" in {row[1] for row in staging_columns}


def test_v77_upgrade_seeds_only_legacy_dialog_facts_and_restarts_incomplete_generation(
    request: pytest.FixtureRequest,
) -> None:
    conn = sqlite3.connect(":memory:")
    request.addfinalizer(conn.close)
    _apply_migrations(conn)
    conn.execute("DROP TRIGGER dialogs_revision_after_update")
    conn.execute(_DIALOGS_V74_DDL)
    conn.execute(
        "INSERT INTO dialogs_v74(dialog_id,name,type,username,identity_complete) VALUES (1,'Old','user','old',0)"
    )
    conn.execute("INSERT INTO dialogs_v74(dialog_id,name,type,username) VALUES (2,NULL,'unknown',NULL)")
    conn.execute("PRAGMA legacy_alter_table=ON")
    conn.execute("DROP TABLE dialogs")
    conn.execute("ALTER TABLE dialogs_v74 RENAME TO dialogs")
    conn.execute("PRAGMA legacy_alter_table=OFF")
    conn.execute("DROP TABLE dialog_directory_baseline")
    conn.execute(
        "CREATE TABLE dialog_directory_baseline(generation INTEGER,dialog_id INTEGER,baseline_revision INTEGER NOT NULL,seen INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(generation,dialog_id)) WITHOUT ROWID"
    )
    conn.execute(_DIALOG_DIRECTORY_STATE_DDL)
    conn.execute(
        "UPDATE dialog_directory_state SET generation=4,status='incomplete',ordinary_status='incomplete',"
        "pinned_main_status='complete',pinned_archive_status='complete',offset_id=6,offset_date=?,"
        "offset_peer=?,observed_count=2 WHERE singleton=1",
        ("2025-01-01T00:00:00+00:00", '{"kind":"user","id":6,"access_hash":42}'),
    )
    conn.execute("INSERT INTO dialog_directory_baseline VALUES (4,1,2,0)")
    conn.execute("INSERT INTO dialog_directory_pins VALUES (4,0,1,0)")
    conn.execute("DELETE FROM schema_version WHERE version=78")
    conn.commit()
    assert _apply_migration_78(conn, 77) == 78
    rows = cast(
        list[tuple[int, str | None, str | None, str | None, str | None, int, int | None, int]],
        conn.execute(
            "SELECT dialog_id,name,type,username,identity_source,identity_complete,identity_observed_at,identity_revision FROM dialogs ORDER BY dialog_id"
        ).fetchall(),
    )
    assert rows == [
        (1, "Old", "user", "old", "legacy", 0, None, 0),
        (2, None, "unknown", None, None, 0, None, 0),
    ]
    state = cast(
        tuple[str, str | None],
        conn.execute("SELECT status,reason FROM dialog_directory_state WHERE singleton=1").fetchone(),
    )
    assert state == ("pending", "identity_baseline_cutover")
    assert conn.execute("SELECT COUNT(*) FROM dialog_directory_baseline").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM dialog_directory_pins").fetchone()[0] == 0
    assert (
        "profile" in conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='dialogs'").fetchone()[0]
    )


class _PublicationClient:
    def __init__(self) -> None:
        self.requests: list[object] = []

    async def __call__(self, request: object) -> object:
        self.requests.append(request)
        if isinstance(request, functions.messages.GetPinnedDialogsRequest):
            return types.messages.PeerDialogs(
                dialogs=[],
                messages=[],
                chats=[],
                users=[],
                state=types.updates.State(pts=0, qts=0, date=None, seq=0, unread_count=0),  # pyright: ignore[reportArgumentType] - Telethon generated alias quirk
            )
        ordinary_request = cast(functions.messages.GetDialogsRequest, request)
        if ordinary_request.offset_id != 0:
            return types.messages.Dialogs(dialogs=[], messages=[], chats=[], users=[])
        dialog = types.Dialog(
            peer=types.PeerUser(99),
            top_message=7,
            read_inbox_max_id=0,
            read_outbox_max_id=0,
            unread_count=0,
            unread_mentions_count=0,
            unread_reactions_count=0,
            unread_poll_votes_count=0,
            notify_settings=types.PeerNotifySettings(),
        )
        message = types.Message(
            id=7,
            peer_id=dialog.peer,
            date=datetime(2026, 1, 1, tzinfo=UTC),
            message="",
        )
        return types.messages.Dialogs(
            dialogs=[dialog],
            messages=[message],
            chats=[],
            users=[types.User(id=99, first_name="Recovered", access_hash=42)],
        )

    async def get_me(self) -> types.User:
        return types.User(id=100, first_name="Account")


@pytest.mark.asyncio
async def test_v77_incomplete_generation_restarts_from_fresh_full_crawl_and_publishes(tmp_path: Path) -> None:
    db_path = tmp_path / "paused-v77.sqlite"
    _create_paused_v77_database(db_path)
    _migrate_and_assert_fresh_directory_generation(db_path)

    client = _PublicationClient()
    directory = CanonicalDialogDirectory(client, db_path, asyncio.Event())
    for _ in range(3):
        await directory.run_slice()
    _assert_fresh_crawl_was_published(client, db_path)


def _assert_fresh_crawl_was_published(client: _PublicationClient, db_path: Path) -> None:
    ordinary_requests = [
        request for request in client.requests if not isinstance(request, functions.messages.GetPinnedDialogsRequest)
    ]
    assert len(client.requests) == 3
    assert len(ordinary_requests) == 1
    ordinary_request = cast(functions.messages.GetDialogsRequest, ordinary_requests[0])
    assert ordinary_request.offset_id == 0
    assert isinstance(ordinary_request.offset_peer, types.InputPeerEmpty)
    conn = _open_sync_db(db_path)
    try:
        publication_state = cast(
            tuple[str, str], conn.execute("SELECT status,ordinary_status FROM dialog_directory_state").fetchone()
        )
        assert publication_state == (
            "complete",
            "complete",
        )
        assert conn.execute("SELECT dialog_id FROM dialogs WHERE hidden=0").fetchall() == [(99,)]
        assert conn.execute("SELECT observed_count FROM dialog_directory_state").fetchone() == (1,)
    finally:
        conn.close()
