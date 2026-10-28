from __future__ import annotations

import json
import sqlite3
from typing import cast

import pytest

from mcp_telegram.sync_db import (
    load_account_cooldown_until_utc,
    load_rpc_safety_owners,
    load_rpc_safety_record,
    save_rpc_safety_record,
)


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("CREATE TABLE daemon_state (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("CREATE TABLE dialog_directory_state (singleton INTEGER PRIMARY KEY, account_id INTEGER)")
    conn.execute("CREATE TABLE update_delivery (singleton INTEGER PRIMARY KEY, account_id INTEGER)")
    conn.execute("CREATE TABLE durable_fact (id INTEGER PRIMARY KEY, value TEXT, observed_at INTEGER)")
    conn.execute("INSERT INTO durable_fact VALUES (1, 'kept', 123)")
    conn.execute("INSERT INTO daemon_state VALUES ('unrelated', 'untouched')")
    return conn


@pytest.fixture
def conn():
    connection = _db()
    yield connection
    connection.close()


def _record(account_id: int | None = None, *, stage: str = "verified", until_utc: float = 100.0) -> str:
    return json.dumps(
        {
            "version": 1,
            "generation": "opaque-generation",
            "scope": {"stage": stage, "dc_id": 2, "key_id": 9, "account_id": account_id},
            "floor": {
                "boot_id": "boot-x",
                "until_monotonic": 50.0,
                "until_utc": until_utc,
                "remaining_seconds": 20.0,
            },
            "intents": [],
            "protection": None,
            "recovery": None,
            "transition": None,
        },
        separators=(",", ":"),
    )


def test_absent_roundtrip_and_canonical_owners(conn: sqlite3.Connection) -> None:
    assert load_rpc_safety_record(conn) is None
    assert load_rpc_safety_owners(conn) == (None, None)
    encoded = _record(11)
    save_rpc_safety_record(conn, None, encoded)
    assert load_rpc_safety_record(conn) == encoded
    assert load_account_cooldown_until_utc(conn) == 100.0
    conn.execute("INSERT INTO dialog_directory_state VALUES (1, 11)")
    conn.execute("INSERT INTO update_delivery VALUES (1, 11)")
    assert load_rpc_safety_owners(conn) == (11, 11)


def test_method_episode_floor_is_not_promoted_to_global_account_floor(conn: sqlite3.Connection) -> None:
    record = cast(dict[str, object], json.loads(_record(11)))
    record.update(
        version=2,
        methods={
            "telethon.tl.functions.users.GetFullUserRequest:b60f5918": {
                "generation": "method-generation",
                "consecutive_actual_floods": 1,
                "floor": {
                    "boot_id": "boot-x",
                    "until_monotonic": 50_000,
                    "until_utc": 50_000,
                    "remaining_seconds": 50_000,
                },
                "claim": {"attempt_id": "trial", "run": "run"},
            }
        },
    )
    encoded = json.dumps(record)
    save_rpc_safety_record(conn, None, encoded)
    assert load_account_cooldown_until_utc(conn) == 100
    assert load_rpc_safety_record(conn) == encoded
    assert conn.execute("SELECT * FROM durable_fact").fetchone() == (1, "kept", 123)


def test_stale_cas_rolls_back_all_changes(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO daemon_state VALUES ('telegram_rpc_safety', ?)", (_record(11),))
    before = conn.execute("SELECT key,value FROM daemon_state ORDER BY key").fetchall()
    with pytest.raises(ValueError, match="stale"):
        save_rpc_safety_record(conn, "wrong", _record(11, until_utc=200))
    assert conn.execute("SELECT key,value FROM daemon_state ORDER BY key").fetchall() == before


def test_foreign_owner_rejects_and_rolls_back_record_and_floor(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO dialog_directory_state VALUES (1, 22)")
    conn.execute("INSERT INTO daemon_state VALUES ('telegram_account_cooldown_until_utc', '300')")
    with pytest.raises(ValueError, match="conflicts"):
        save_rpc_safety_record(conn, None, _record(11, until_utc=200))
    assert load_rpc_safety_record(conn) is None
    assert load_account_cooldown_until_utc(conn) == 300.0


def test_auth_transition_rechecks_expected_account_before_unverified_binding_commit(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO dialog_directory_state VALUES (1, 22)")
    encoded = cast(dict[str, object], json.loads(_record(None, stage="transport_only")))
    encoded["transition"] = {"expected_account": 11}
    with pytest.raises(ValueError, match="conflicts"):
        save_rpc_safety_record(conn, None, json.dumps(encoded))
    assert load_rpc_safety_record(conn) is None
    assert load_account_cooldown_until_utc(conn) is None


def test_matching_and_absent_owners_allow_verified_or_unverified_scope(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO update_delivery VALUES (1, 11)")
    save_rpc_safety_record(conn, None, _record(11))
    next_record = _record(None, stage="transport_only", until_utc=90)
    save_rpc_safety_record(conn, _record(11), next_record)
    assert load_rpc_safety_record(conn) == next_record


def test_floor_is_maximum_and_other_durable_state_is_preserved(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO daemon_state VALUES ('telegram_account_cooldown_until_utc', '300')")
    save_rpc_safety_record(conn, None, _record(11, until_utc=200))
    assert load_account_cooldown_until_utc(conn) == 300.0
    assert conn.execute("SELECT key,value FROM daemon_state WHERE key='unrelated'").fetchone() == (
        "unrelated",
        "untouched",
    )
    assert conn.execute("SELECT value,observed_at FROM durable_fact WHERE id=1").fetchone() == ("kept", 123)


def test_corrupt_legacy_floor_fails_closed_and_rolls_back(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO daemon_state VALUES ('telegram_account_cooldown_until_utc', 'NaN')")
    with pytest.raises(ValueError, match="invalid persisted"):
        load_account_cooldown_until_utc(conn)
    with pytest.raises(ValueError, match="invalid persisted"):
        save_rpc_safety_record(conn, None, _record(11))
    assert load_rpc_safety_record(conn) is None
    assert conn.execute(
        "SELECT value FROM daemon_state WHERE key='telegram_account_cooldown_until_utc'"
    ).fetchone() == ("NaN",)
