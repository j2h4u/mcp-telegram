from __future__ import annotations

import shutil
import sqlite3
import time
from pathlib import Path
from typing import cast

import pytest

from mcp_telegram import event_recovery
from mcp_telegram.sync_db import ensure_sync_schema


def _source_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """CREATE TABLE schema_version(version INTEGER);
        INSERT INTO schema_version VALUES (50);
        CREATE TABLE telemetry_events(
          id INTEGER PRIMARY KEY, tool_name TEXT, timestamp REAL, duration_ms REAL,
          result_count INTEGER, has_cursor INTEGER, page_depth INTEGER, has_filter INTEGER,
          error_type TEXT, outcome TEXT, error_code TEXT);
        CREATE TABLE daemon_events(
          id INTEGER PRIMARY KEY, kind TEXT, dialog_id INTEGER, occurred_at INTEGER, payload_json TEXT);
        CREATE TABLE sync_alert_events(
          kind TEXT, dialog_id INTEGER, message_id INTEGER, version INTEGER, occurred_at INTEGER);
        CREATE TABLE messages(
          dialog_id INTEGER, message_id INTEGER, sender_id INTEGER, out INTEGER, is_service INTEGER);
        CREATE TABLE dialogs(dialog_id INTEGER, type TEXT);
        CREATE TABLE entities(id INTEGER, type TEXT);
        CREATE TABLE message_versions(
          dialog_id INTEGER, message_id INTEGER, version INTEGER, old_text TEXT);
        CREATE TABLE message_transcriptions(
          dialog_id INTEGER, message_id INTEGER, received_at INTEGER);
        INSERT INTO dialogs VALUES (1,'user');
        INSERT INTO entities VALUES (1,'user');
        INSERT INTO messages VALUES (1,10,1,0,0),(1,11,1,0,0);
        INSERT INTO message_versions VALUES (1,10,1,'before');
        INSERT INTO sync_alert_events VALUES ('edit',1,10,1,100);
        INSERT INTO sync_alert_events VALUES ('deleted_message',1,11,NULL,101);
        INSERT INTO daemon_events VALUES (
          7,'access_lost',1,102,
          '{"reason":"ChannelPrivateError","previous_status":"full"}');
        """
    )
    now = time.time()
    conn.executemany(
        "INSERT INTO telemetry_events VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            (1, "old", now - 10_000, 1.0, 0, 0, 0, 0, None, "success", None),
            (2, "recent", now - 10, 2.0, 1, 0, 0, 0, None, "success", None),
        ],
    )
    conn.commit()
    return conn


def _target_db(path: Path) -> None:
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute("INSERT INTO dialogs(dialog_id,type) VALUES (1,'user')")
        conn.execute("INSERT INTO entities(id,type,updated_at) VALUES (1,'user',1)")
        conn.execute(
            "INSERT INTO messages(dialog_id,message_id,sent_at,sender_id,out,is_service) "
            "VALUES (1,10,1,1,0,0),(1,11,1,1,0,0)"
        )
        conn.execute(
            "INSERT INTO message_versions(dialog_id,message_id,version,old_text,edit_date,origin) "
            "VALUES (1,10,1,'before',100,'telegram_edit')"
        )
        conn.execute(
            "INSERT INTO conversation_history_events(kind,occurred_at,time_basis,dialog_id,message_id,version) "
            "VALUES ('edit',100,'telegram',1,10,1)"
        )
        conn.execute(
            "INSERT INTO conversation_history_events(kind,occurred_at,time_basis,dialog_id,message_id) "
            "VALUES ('deleted_message',101,'observed',1,11)"
        )
        conn.execute(
            "INSERT INTO conversation_history_events(kind,occurred_at,time_basis,dialog_id) "
            "VALUES ('access_lost',102,'observed',1)"
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def recovery_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, sqlite3.Connection, str]:
    monkeypatch.setattr(event_recovery, "EXPECTED_TELEMETRY", 2)
    monkeypatch.setattr(event_recovery, "EXPECTED_EDITS", 1)
    monkeypatch.setattr(event_recovery, "EXPECTED_DELETES", 1)
    monkeypatch.setattr(event_recovery, "EXPECTED_LOSSES", 1)
    source = tmp_path / "sync.db"
    source_conn = _source_db(source)
    target = tmp_path / "target.db"
    _target_db(target)
    return source, target, source_conn, event_recovery.source_fingerprint(source)


def test_recovery_imports_verifies_and_repeats_as_no_op(
    recovery_case: tuple[Path, Path, sqlite3.Connection, str],
) -> None:
    source, target, source_conn, fingerprint = recovery_case
    try:
        first = event_recovery.recover_events(source, target, expected_fingerprint=fingerprint, retention_seconds=3600)
        second = event_recovery.recover_events(source, target, expected_fingerprint=fingerprint, retention_seconds=3600)
    finally:
        source_conn.close()
    assert first == {"status": "imported", "telemetry": 2, "lifecycle": 1}
    assert second == {"status": "no_op", "telemetry": 2, "lifecycle": 1}
    conn = sqlite3.connect(target)
    try:
        assert conn.execute(
            "SELECT tool_name,source_event_id FROM runtime_observations WHERE source_namespace='backup-2026-09-06'"
        ).fetchall() == [("recent", 2)]
        assert conn.execute(
            "SELECT reason_code,previous_status,source_event_id FROM conversation_history_events "
            "WHERE kind='access_lost'"
        ).fetchone() == ("ChannelPrivateError", "full", 7)
        bounds = cast(
            list[tuple[str, str]],
            conn.execute(
                "SELECT key,value FROM daemon_state WHERE key LIKE 'runtime_observations_legacy_%' ORDER BY key"
            ).fetchall(),
        )
        observed_at_ms = cast(
            tuple[int],
            conn.execute(
                "SELECT observed_at_ms FROM runtime_observations WHERE source_namespace='backup-2026-09-06'"
            ).fetchone(),
        )[0]
        assert len(bounds) == 2
        assert {int(value) for _, value in bounds} == {observed_at_ms}
    finally:
        conn.close()


def test_recovery_conflict_rolls_back_without_ledger(
    recovery_case: tuple[Path, Path, sqlite3.Connection, str],
) -> None:
    source, target, source_conn, fingerprint = recovery_case
    conn = sqlite3.connect(target)
    conn.execute("DROP TRIGGER conversation_history_no_update")
    conn.execute("UPDATE conversation_history_events SET reason_code='different' WHERE kind='access_lost'")
    conn.execute(event_recovery._CONVERSATION_HISTORY_TRIGGERS_V54[-2])
    conn.commit()
    conn.close()
    try:
        with pytest.raises(RuntimeError, match="enrichment conflict"):
            event_recovery.recover_events(source, target, expected_fingerprint=fingerprint, retention_seconds=3600)
    finally:
        source_conn.close()
    conn = sqlite3.connect(target)
    try:
        assert conn.execute("SELECT COUNT(*) FROM event_recovery_ledger").fetchone()[0] == 0
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM runtime_observations WHERE source_namespace='backup-2026-09-06'"
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def test_recovery_rejects_copy_changed_during_snapshot(
    recovery_case: tuple[Path, Path, sqlite3.Connection, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, source_conn, fingerprint = recovery_case
    original = shutil.copy2

    def corrupt_copy(src: Path, dst: Path) -> Path:
        result = original(src, dst)
        if str(dst).endswith("-shm"):
            with Path(dst).open("ab") as handle:
                handle.write(b"changed")
        return Path(result)

    monkeypatch.setattr(event_recovery.shutil, "copy2", corrupt_copy)
    try:
        with pytest.raises(RuntimeError, match="changed while creating"):
            event_recovery.recover_events(source, target, expected_fingerprint=fingerprint, retention_seconds=3600)
    finally:
        source_conn.close()


def test_recovery_no_op_revalidates_enriched_lifecycle(
    recovery_case: tuple[Path, Path, sqlite3.Connection, str],
) -> None:
    source, target, source_conn, fingerprint = recovery_case
    event_recovery.recover_events(source, target, expected_fingerprint=fingerprint, retention_seconds=3600)
    conn = sqlite3.connect(target)
    conn.execute("DROP TRIGGER conversation_history_no_update")
    conn.execute("UPDATE conversation_history_events SET reason_code='tampered' WHERE kind='access_lost'")
    conn.execute(event_recovery._CONVERSATION_HISTORY_TRIGGERS_V54[-2])
    conn.commit()
    conn.close()
    try:
        with pytest.raises(RuntimeError, match="metadata differs"):
            event_recovery.recover_events(source, target, expected_fingerprint=fingerprint, retention_seconds=3600)
    finally:
        source_conn.close()
