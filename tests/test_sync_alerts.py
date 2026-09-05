from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3

import pytest

import mcp_telegram.sync_alerts as sync_alerts_module
from mcp_telegram.sync_alerts import (
    SyncAlertCursor,
    SyncAlertTokenCodec,
    parse_request,
    query_alerts,
)


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """CREATE TABLE messages (
            dialog_id INTEGER, message_id INTEGER, is_deleted INTEGER,
            deleted_at INTEGER, text TEXT
        );
        CREATE TABLE message_versions (
            dialog_id INTEGER, message_id INTEGER, version INTEGER,
            edit_date INTEGER, old_text TEXT
        );
        CREATE TABLE synced_dialogs (
            dialog_id INTEGER, status TEXT, access_lost_at INTEGER
        );
        CREATE TABLE daemon_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, dialog_id INTEGER,
            occurred_at INTEGER, payload_json TEXT
        );
        CREATE TABLE sync_alert_events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            occurred_at INTEGER NOT NULL,
            dialog_id INTEGER NOT NULL,
            message_id INTEGER,
            version INTEGER,
            daemon_event_id INTEGER
        );
        CREATE UNIQUE INDEX sync_alert_deleted ON sync_alert_events(dialog_id, message_id) WHERE kind = 'deleted_message';
        CREATE UNIQUE INDEX sync_alert_edit ON sync_alert_events(dialog_id, message_id, version) WHERE kind = 'edit';
        CREATE UNIQUE INDEX sync_alert_access ON sync_alert_events(daemon_event_id) WHERE kind = 'access_lost';
        CREATE TRIGGER sync_alert_deleted_insert AFTER INSERT ON messages
        WHEN NEW.is_deleted = 1 AND NEW.deleted_at IS NOT NULL BEGIN
            INSERT OR IGNORE INTO sync_alert_events(kind, occurred_at, dialog_id, message_id)
            VALUES ('deleted_message', NEW.deleted_at, NEW.dialog_id, NEW.message_id);
        END;
        CREATE TRIGGER sync_alert_deleted_update AFTER UPDATE OF is_deleted, deleted_at ON messages
        WHEN OLD.is_deleted = 0 AND NEW.is_deleted = 1 AND NEW.deleted_at IS NOT NULL BEGIN
            INSERT OR IGNORE INTO sync_alert_events(kind, occurred_at, dialog_id, message_id)
            VALUES ('deleted_message', NEW.deleted_at, NEW.dialog_id, NEW.message_id);
        END;
        CREATE TRIGGER sync_alert_edit AFTER INSERT ON message_versions
        WHEN NEW.edit_date IS NOT NULL BEGIN
            INSERT INTO sync_alert_events(kind, occurred_at, dialog_id, message_id, version)
            VALUES ('edit', NEW.edit_date, NEW.dialog_id, NEW.message_id, NEW.version);
        END;
        CREATE TRIGGER sync_alert_access AFTER INSERT ON daemon_events
        WHEN NEW.kind = 'access_lost' BEGIN
            INSERT INTO sync_alert_events(kind, occurred_at, dialog_id, daemon_event_id)
            VALUES ('access_lost', NEW.occurred_at, NEW.dialog_id, NEW.id);
        END;"""
    )
    return conn


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = _db()
    yield conn
    conn.close()


def test_parse_request_is_strict_and_page_limit_is_effective() -> None:
    assert parse_request({"page_limit": 4}).page_limit == 4
    with pytest.raises(ValueError, match="integer"):
        parse_request({"since": "0"})
    with pytest.raises(ValueError, match="greater than or equal"):
        parse_request({"since": -1})
    with pytest.raises(ValueError, match="integer"):
        parse_request({"limit": True})
    with pytest.raises(ValueError, match="between"):
        parse_request({"limit": 501})
    with pytest.raises(ValueError, match="between"):
        parse_request({"page_limit": 0})
    with pytest.raises(ValueError, match="match"):
        parse_request({"limit": 2, "page_limit": 3})


def test_global_keyset_page_and_snapshot_exclude_newer_events(db: sqlite3.Connection) -> None:
    conn = db
    conn.executemany("INSERT INTO messages VALUES (?, ?, 1, ?, ?)", [(1, 1, 10, "one"), (1, 2, 9, "two")])
    conn.execute("INSERT INTO message_versions VALUES (2, 4, 1, 10, 'old')")
    codec = SyncAlertTokenCodec()
    first = query_alerts(conn, {"limit": 2}, codec)
    data = first["data"]
    assert isinstance(data, dict)
    assert [item["kind"] for item in data["alerts"]] == ["edit", "deleted_message"]
    assert data["has_more"] is True
    token = data["next_navigation"]
    conn.execute("INSERT INTO messages VALUES (1, 99, 1, 11, 'new')")
    # A late row at the same timestamp but below the full snapshot tuple is
    # also outside the frozen feed.
    conn.execute("INSERT INTO messages VALUES (1, 999, 1, 10, 'late')")
    second = query_alerts(conn, {"limit": 2, "navigation": token}, codec)
    second_data = second["data"]
    assert isinstance(second_data, dict)
    assert [item["message_id"] for item in second_data["alerts"]] == [1]
    fresh = query_alerts(conn, {"limit": 10}, codec)
    fresh_data = fresh["data"]
    assert isinstance(fresh_data, dict)
    assert 99 in [item["message_id"] for item in fresh_data["alerts"]]


def test_navigation_can_change_size_only_with_explicit_page_limit(db: sqlite3.Connection) -> None:
    db.executemany("INSERT INTO messages VALUES (1, ?, 1, ?, ?)", [(1, 3, "a"), (2, 2, "b"), (3, 1, "c")])
    codec = SyncAlertTokenCodec()
    first = query_alerts(db, {"limit": 1}, codec)
    token = first["data"]["next_navigation"]
    changed = query_alerts(db, {"page_limit": 2, "navigation": token}, codec)
    assert changed["ok"] is True
    assert changed["data"]["page_limit"] == 2


def test_navigation_only_preserves_cursor_context_and_page_depth(db: sqlite3.Connection) -> None:
    db.executemany("INSERT INTO messages VALUES (1, ?, 1, ?, ?)", [(1, 3, "a"), (2, 2, "b"), (3, 1, "c")])
    codec = SyncAlertTokenCodec()
    first = query_alerts(db, {"limit": 1}, codec)
    second = query_alerts(db, {"navigation": first["data"]["next_navigation"]}, codec)
    third = query_alerts(db, {"navigation": second["data"]["next_navigation"]}, codec)
    assert second["data"]["page_depth"] == 2
    assert third["data"]["page_depth"] == 3


def test_snapshot_watermark_is_max_fact_time_within_snapshot(db: sqlite3.Connection) -> None:
    db.execute("INSERT INTO messages VALUES (1, 1, 1, 11, 'newer')")
    db.execute("INSERT INTO messages VALUES (1, 2, 1, 10, 'older')")
    codec = SyncAlertTokenCodec()
    first = query_alerts(db, {"limit": 1}, codec)
    db.execute("INSERT INTO messages VALUES (1, 3, 1, 5, 'late')")
    second = query_alerts(db, {"navigation": first["data"]["next_navigation"]}, codec)
    assert second["data"]["snapshot_upper_event_at"] == 11


def test_snapshot_watermark_is_computed_once_and_cursor_bound(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    db.execute("INSERT INTO messages VALUES (1, 1, 1, 11, 'newer')")
    db.execute("INSERT INTO messages VALUES (1, 2, 1, 10, 'older')")
    codec = SyncAlertTokenCodec()
    calls = 0
    original = sync_alerts_module._snapshot_event_at

    def counted(conn: sqlite3.Connection, snapshot_seq: int, since: int) -> int:
        nonlocal calls
        calls += 1
        return original(conn, snapshot_seq, since)

    monkeypatch.setattr(sync_alerts_module, "_snapshot_event_at", counted)
    first = query_alerts(db, {"limit": 1}, codec)
    db.execute("INSERT INTO messages VALUES (1, 3, 1, 5, 'late')")
    second = query_alerts(db, {"navigation": first["data"]["next_navigation"]}, codec)
    assert calls == 1
    assert second["data"]["snapshot_upper_event_at"] == 11


def test_token_requires_snapshot_watermark_field() -> None:
    codec = SyncAlertTokenCodec()
    body = {
        "kind": "sync_alerts",
        "version": 1,
        "since": 0,
        "snapshot_seq": 2,
        "after_seq": 1,
        "page_depth": 2,
        "page_limit": 1,
    }
    encoded = sync_alerts_module._b64encode(json.dumps(body, separators=(",", ":"), sort_keys=True).encode())
    signature = hmac.new(codec._secret, encoded.encode(), hashlib.sha256).digest()
    token = f"{encoded}.{sync_alerts_module._b64encode(signature)}"
    with pytest.raises(ValueError, match="invalid_navigation"):
        codec.decode(token)


def test_token_decode_requires_canonical_bounded_base64_and_daemon_secret(db: sqlite3.Connection) -> None:
    codec = SyncAlertTokenCodec()
    cursor = SyncAlertCursor(0, 1, 3, 3, 2, 2)
    token = codec.encode(cursor)
    encoded, signature = token.split(".")
    for malformed in (f"{encoded}=.{signature}", f"{encoded}.{signature}=", f"{encoded}junk.{signature}"):
        with pytest.raises(ValueError, match="invalid_navigation"):
            codec.decode(malformed)
    with pytest.raises(ValueError, match="invalid_navigation"):
        SyncAlertTokenCodec().decode(token)


def test_tampered_and_context_mismatched_tokens_are_rejected(db: sqlite3.Connection) -> None:
    conn = db
    conn.execute("INSERT INTO messages VALUES (1, 1, 1, 10, 'one')")
    conn.execute("INSERT INTO messages VALUES (1, 2, 1, 9, 'two')")
    codec = SyncAlertTokenCodec()
    result = query_alerts(conn, {"limit": 1}, codec)
    data = result["data"]
    assert isinstance(data, dict)
    token = data["next_navigation"]
    assert isinstance(token, str)
    assert query_alerts(conn, {"limit": 2, "navigation": token}, codec)["error"] == "invalid_navigation"
    assert query_alerts(conn, {"limit": 1, "navigation": token[:-1] + "x"}, codec)["error"] == "invalid_navigation"
