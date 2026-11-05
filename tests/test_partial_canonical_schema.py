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
        assert conn.execute("SELECT max(version) FROM schema_version").fetchone() == (100,)


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
        assert conn.execute("SELECT max(version) FROM schema_version").fetchone() == (100,)


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


def test_image_job_revision_normalization_is_fenced_and_preserves_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "image-jobs.db"
    with monkeypatch.context() as old:
        old.setattr(sync_db, "_CURRENT_SCHEMA_VERSION", 98)
        sync_db.ensure_sync_schema(path)
    # id, account, state, body generation, source generation, source photo, source eligible,
    # job generation, job photo, message deleted, observation deleted, tombstone, kind, payload
    cases = [
        (1, 42, "queued", 7, None, None, None, 7, None, 0, 0, False, "other", "{}"),
        (2, 42, "running", 7, 3, None, 1, 7, None, 0, 0, False, "photo", '{"ttl_seconds":0,"live_photo":false}'),
        (3, 42, "blocked", 7, 3, None, 1, 7, None, 0, 0, False, "photo", '{"ttl_seconds":[],"live_photo":{}}'),
        (4, 42, "failed", 7, 3, None, 1, 7, None, 0, 0, False, "photo", "{}"),
        (5, 43, "queued", 7, 3, None, 1, 7, None, 0, 0, False, "photo", "{}"),
        (6, 42, "queued", 7, 3, None, 1, 6, None, 0, 0, False, "photo", "{}"),
        (7, 42, "queued", 7, 3, None, 1, 7, None, 1, 0, False, "photo", "{}"),
        (8, 42, "queued", 7, 3, None, 1, 7, None, 0, 1, False, "photo", "{}"),
        (9, 42, "queued", 7, 3, None, 1, 7, None, 0, 0, True, "photo", "{}"),
        (10, 42, "queued", 7, 3, 55, 1, 7, None, 0, 0, False, "photo", "{}"),
        (11, 42, "queued", 7, 3, None, 0, 7, None, 0, 0, False, "photo", "{}"),
        (12, 42, "queued", 7, 3, None, 1, 7, 55, 0, 0, False, "photo", "{}"),
        (13, 42, "complete", 7, 3, None, 1, 7, None, 0, 0, False, "photo", "{}"),
        (14, 42, "queued", 7, 3, None, 1, 7, None, 0, 0, False, "other", '{"x":1}'),
        (15, 42, "queued", 7, 3, None, 1, 7, None, 0, 0, False, "photo", '{"ttl_seconds":1}'),
        (16, 42, "queued", 7, 3, None, 1, 7, None, 0, 0, False, "photo", '{"live_photo":[1]}'),
        (17, 42, "queued", 7, 3, None, 1, 7, None, 0, 0, False, "photo", '{"ttl_seconds":"not-json"}'),
    ]
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
        for (
            mid,
            account,
            state,
            body,
            source,
            photo,
            eligible,
            generation,
            job_photo,
            deleted,
            observed_deleted,
            tombstone,
            kind,
            payload,
        ) in cases:
            conn.execute(
                "INSERT INTO messages(dialog_id,message_id,sent_at,is_deleted,media_kind,media_payload) VALUES(10,?,1,?,?,?)",
                (mid, deleted, kind, payload),
            )
            conn.execute(
                "INSERT INTO message_observations(dialog_id,message_id,observation_order,is_deleted) VALUES(10,?,?,?)",
                (mid, body, observed_deleted),
            )
            if source is not None:
                conn.execute("INSERT INTO message_image_sources VALUES(10,?,?,?,?)", (mid, photo, source, eligible))
            if tombstone:
                conn.execute("INSERT INTO nonchannel_message_deletions VALUES(?,1)", (mid,))
            conn.execute(
                "INSERT INTO message_image_jobs VALUES(?,10,?,?,9,?,?,3,2,17,1,4,'unsupported_photo')",
                (account, mid, state, generation, job_photo),
            )
        conn.execute("INSERT INTO message_image_artifacts VALUES (42,10,1,55,7,'saved','hash',10,2,3)")
        before = cast(
            list[tuple[int, int, int, str, int, int, int | None, int, int, int, int, int | None, str | None]],
            conn.execute("SELECT * FROM message_image_jobs ORDER BY message_id").fetchall(),
        )
    sync_db.ensure_sync_schema(path)
    sync_db.ensure_sync_schema(path)
    with closing(sqlite3.connect(path)) as conn:
        after = cast(
            list[tuple[int, int, int, str, int, int, int | None, int, int, int, int, int | None, str | None]],
            conn.execute("SELECT * FROM message_image_jobs ORDER BY message_id").fetchall(),
        )
        assert after == [(*row[:5], (0 if row[2] == 1 else 3) if row[2] <= 4 else row[5], *row[6:]) for row in before]
        assert conn.execute("SELECT * FROM message_image_artifacts").fetchall() == [
            (42, 10, 1, 55, 7, "saved", "hash", 10, 2, 3)
        ]
        assert conn.execute("SELECT max(version) FROM schema_version").fetchone() == (100,)


def test_image_job_revision_is_unchanged_without_bound_account(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "unbound-image.db"
    with monkeypatch.context() as old:
        old.setattr(sync_db, "_CURRENT_SCHEMA_VERSION", 98)
        sync_db.ensure_sync_schema(path)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "INSERT INTO messages(dialog_id,message_id,sent_at,media_kind,media_payload) VALUES(10,1,1,'photo','{}')"
        )
        conn.execute("INSERT INTO message_observations(dialog_id,message_id,observation_order) VALUES(10,1,7)")
        conn.execute("INSERT INTO message_image_jobs VALUES(42,10,1,'queued',9,7,NULL,3,2,17,1,4,NULL)")
    sync_db.ensure_sync_schema(path)
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT source_generation FROM message_image_jobs").fetchone() == (7,)
