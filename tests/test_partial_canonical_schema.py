"""Forward normalization preserves folder and topic facts in the current schema."""

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import cast

import pytest

import mcp_telegram.sync_db as sync_db


@pytest.mark.parametrize("accepted", [False, True])
def test_partial_canonical_schema_preserves_facts_and_represents_unknowns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, accepted: bool
) -> None:
    path = tmp_path / "sync.db"
    with monkeypatch.context() as old:
        old.setattr(sync_db, "_CURRENT_SCHEMA_VERSION", 98)
        sync_db.ensure_sync_schema(path)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("UPDATE telegram_folder_projection_state SET account_id=42 WHERE singleton=1")
        if accepted:
            conn.execute(
                "UPDATE telegram_folder_projection_state SET coverage_status='complete', "
                "accepted_rule_token='accepted', rule_observation_started_at=10 WHERE singleton=1"
            )
        conn.execute("INSERT INTO telegram_folder_rules VALUES ('legacy',1,'Saved','filter',0,'{}')")
        conn.execute("INSERT INTO telegram_folder_local_members VALUES ('legacy',1,55,'present',NULL)")
        conn.execute(
            "INSERT INTO topic_metadata(dialog_id,topic_id,title,is_general,is_deleted,updated_at,pinned,hidden,observation_order,catalog_present) VALUES (1,1,'Saved',0,0,10,1,0,7,1),(1,2,'',0,1,10,0,0,0,0)"
        )
    sync_db.ensure_sync_schema(path)
    with closing(sqlite3.connect(path)) as conn, conn:
        assert conn.execute(
            "SELECT title,account_id,observation_order,deleted,catalog_member,membership_order FROM telegram_folder_rules"
        ).fetchall() == [("Saved", 42, 0, 0, 1 if accepted else None, 0)]
        assert conn.execute("SELECT dialog_id FROM telegram_folder_local_members").fetchall() == [(55,)]
        assert conn.execute(
            "SELECT filter_order_json,filter_order_observation_order,rule_enumeration_complete "
            "FROM telegram_folder_projection_state WHERE singleton=1"
        ).fetchone() == (None, 0, int(accepted))
        assert conn.execute(
            "SELECT title,pinned,hidden,title_order,pinned_order,hidden_order,membership_order,known_fields FROM topic_metadata ORDER BY topic_id"
        ).fetchall() == [("Saved", 1, 0, 0, 0, 0, 0, 9), ("", 0, 0, 0, 0, 0, 0, 0)]
        conn.execute(
            "INSERT INTO topic_metadata(dialog_id,topic_id,is_general,is_deleted,updated_at) VALUES (1,3,0,0,1)"
        )
        conn.execute(
            "INSERT INTO telegram_folder_rules(namespace,folder_id,account_id,observation_order,deleted) VALUES ('legacy',2,42,8,1)"
        )
        conn.execute(
            "INSERT INTO telegram_folder_rules(namespace,folder_id,title,rule_kind,rule_json,account_id) VALUES ('legacy',1,'Other','filter','{}',43)"
        )
        assert conn.execute(
            "SELECT count(*) FROM telegram_folder_rules WHERE namespace='legacy' AND folder_id=1"
        ).fetchone() == (2,)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO telegram_folder_rules(namespace,folder_id,title,rule_kind,rule_json,account_id) VALUES ('legacy',1,'Duplicate','filter','{}',43)"
            )
        assert conn.execute("SELECT title,pinned,hidden FROM topic_metadata WHERE topic_id=3").fetchone() == (
            None,
            None,
            None,
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO telegram_folder_rules(namespace,folder_id,deleted) VALUES ('legacy',3,0)")
        assert conn.execute("SELECT max(version) FROM schema_version").fetchone() == (99,)


def test_partial_schema_failure_rolls_back_both_domains(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "rollback.db"
    with monkeypatch.context() as old:
        old.setattr(sync_db, "_CURRENT_SCHEMA_VERSION", 98)
        sync_db.ensure_sync_schema(path)
    with closing(sqlite3.connect(path)) as conn:

        def reject_folder_drop(action: int, first: str | None, *_args: object) -> int:
            return (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_DROP_TABLE and first == "telegram_folder_rules"
                else sqlite3.SQLITE_OK
            )

        conn.set_authorizer(reject_folder_drop)
        with pytest.raises(sqlite3.DatabaseError):
            sync_db._apply_migration_99(conn, 98)
        conn.set_authorizer(None)
        assert conn.execute("SELECT max(version) FROM schema_version").fetchone() == (98,)
        topic_columns = cast(list[tuple[object, ...]], conn.execute("PRAGMA table_info(topic_metadata)").fetchall())
        folder_columns = cast(
            list[tuple[object, ...]], conn.execute("PRAGMA table_info(telegram_folder_rules)").fetchall()
        )
        assert "title_order" not in {row[1] for row in topic_columns}
        assert "deleted" not in {row[1] for row in folder_columns}
    sync_db.ensure_sync_schema(path)
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT max(version) FROM schema_version").fetchone() == (99,)


def test_missing_folder_owner_rolls_back_without_reassigning_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "unknown-owner.db"
    with monkeypatch.context() as old:
        old.setattr(sync_db, "_CURRENT_SCHEMA_VERSION", 98)
        sync_db.ensure_sync_schema(path)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("INSERT INTO telegram_folder_rules VALUES ('legacy',1,'Saved','filter',0,'{}')")
        conn.execute("UPDATE telegram_folder_projection_state SET account_id=NULL WHERE singleton=1")
    with pytest.raises(sqlite3.IntegrityError):
        sync_db.ensure_sync_schema(path)
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT max(version) FROM schema_version").fetchone() == (98,)
        assert conn.execute("SELECT title FROM telegram_folder_rules").fetchone() == ("Saved",)
        assert "title_order" not in {
            row[1]
            for row in cast(list[tuple[object, ...]], conn.execute("PRAGMA table_info(topic_metadata)").fetchall())
        }
