"""V1 profile knowledge survives the account-scoped v2 coverage cutover."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import cast

import pytest

import mcp_telegram.sync_db as sync_db


def test_v87_preserves_v1_facts_and_marks_new_coverage_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "sync.db"
    with monkeypatch.context() as old:
        old.setattr(sync_db, "_CURRENT_SCHEMA_VERSION", 86)
        sync_db.ensure_sync_schema(path)
    facts = json.dumps(
        {"schema": 1, "id": 42, "type": "user", "note": " \n💬 keep \n ", "business_intro": {"title": "retained"}},
        ensure_ascii=False,
    )
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            "INSERT INTO entities(id,type,name,updated_at) VALUES (42,'user','Retained',10),(43,'user','Unknown owner',10)"
        )
        conn.execute(
            "INSERT INTO entity_details(entity_id,detail_json,fetched_at,profile_owner_account_id) VALUES (42,?,100,77),(43,?,90,NULL)",
            (facts, facts),
        )
        conn.execute(
            "INSERT INTO entity_detail_sections(entity_id,section,status,observed_at,normalization_version) VALUES (42,'full_profile','fresh',100,'entity-profile-full-user-v1')"
        )
        conn.commit()
    sync_db.ensure_sync_schema(path)
    sync_db.ensure_sync_schema(path)
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute(
            "SELECT detail_json,fetched_at,profile_owner_account_id FROM entity_details WHERE entity_id=42"
        ).fetchone() == (facts, 100, 77)
        assert conn.execute(
            "SELECT status,observed_at,normalization_version FROM entity_detail_sections WHERE entity_id=42"
        ).fetchone() == ("fresh", 100, "entity-profile-full-user-v1")
        row = cast(
            tuple[object, ...],
            conn.execute(
                "SELECT owner_account_id,entity_id,normalization_version,fields_json,coverage_json,complete,observation_order,observation_started_at,observation_completed_at,source FROM entity_profile_semantic_snapshots"
            ).fetchone(),
        )
        assert row[:4] == (77, 42, "entity-profile-full-user-v2", "{}")
        assert json.loads(cast(str, row[4])) == {"reason": "legacy_normalization_incomplete"}
        assert row[5:] == (0, 0, None, None, "legacy")
        assert conn.execute("SELECT COUNT(*) FROM schema_version WHERE version=87").fetchone() == (1,)
        assert conn.execute("SELECT COUNT(*) FROM entity_profile_semantic_snapshots WHERE entity_id=43").fetchone() == (
            0,
        )
        assert conn.execute("SELECT detail_json FROM entity_details WHERE entity_id=43").fetchone() == (facts,)


def test_v87_fresh_schema_account_namespace_and_owner_constraints(tmp_path: Path) -> None:
    path = tmp_path / "fresh.db"
    sync_db.ensure_sync_schema(path)
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT MAX(version),COUNT(DISTINCT version) FROM schema_version").fetchone() == (88, 88)
        assert conn.execute("SELECT COUNT(*) FROM entity_profile_semantic_snapshots").fetchone() == (0,)
        conn.execute("INSERT INTO entities(id,type,updated_at) VALUES (42,'user',1)")
        for owner in (77, 88):
            conn.execute(
                "INSERT INTO entity_profile_semantic_snapshots(owner_account_id,entity_id,normalization_version,source) VALUES (?,42,'entity-profile-full-user-v2','rpc')",
                (owner,),
            )
        assert conn.execute(
            "SELECT owner_account_id,complete FROM entity_profile_semantic_snapshots ORDER BY owner_account_id"
        ).fetchall() == [(77, 0), (88, 0)]
        for invalid in (0, -1, "invalid"):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO entity_profile_semantic_snapshots(owner_account_id,entity_id,normalization_version,source) VALUES (?,42,'entity-profile-full-user-v2','rpc')",
                    (invalid,),
                )
