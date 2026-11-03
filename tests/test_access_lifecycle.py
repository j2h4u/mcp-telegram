from __future__ import annotations

import logging
import sqlite3

import pytest

from mcp_telegram.access_lifecycle import (
    AccessLossEvidence,
    complete_access_revalidation,
    record_access_recovery_observation,
    restore_access_after_revalidation,
    set_access_lost,
    stamp_access_revalidation,
)
from mcp_telegram.sync_db import _apply_migrations
from mcp_telegram.sync_transactions import write_transaction
from tests.history_enrollment_helpers import seed_full_history_enrollment


def test_membership_access_fence_rejects_replay_after_restoration() -> None:
    conn = _db()
    try:
        with write_transaction(conn):
            conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
            conn.execute("INSERT INTO entities(id,type,updated_at) VALUES (12,'user',1)")
            conn.execute("INSERT INTO dialogs(dialog_id,type) VALUES (12,'user'),(-5,'group')")
            conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES (-5,'synced')")
            conn.execute(
                "INSERT INTO entity_details(entity_id,detail_json,fetched_at,profile_owner_account_id) VALUES (12,'{}',1,42)"
            )
            conn.execute(
                "INSERT INTO entity_detail_sections(entity_id,section,status,payload_json) VALUES (12,'common_chats','fresh',?)",
                ('{"published":{"owner_account_id":42,"generation":1,"scan":0,"observation_started_at":10}}',),
            )
            conn.execute(
                "INSERT INTO entity_profile_collection_facts(owner_account_id,entity_id,section,item_key,fields_json,coverage_json,observation_order,observed_at) VALUES (42,12,'common_chats','chat:5','{}','{}',1,10)"
            )
            conn.execute(
                "INSERT INTO entity_profile_collection_membership(owner_account_id,entity_id,section,generation,scan,item_key,identity_kind,page_key,ordinal,detail_complete) VALUES (42,12,'common_chats',1,0,'chat:5','remote','first',0,1)"
            )
        with pytest.raises(RuntimeError, match="rollback"), write_transaction(conn):
            assert (
                set_access_lost(
                    conn, -5, 20, profile_owner_account_id=42, observation_order=10, membership_revision_at=20
                ).membership_counts.queued
                == 1
            )
            raise RuntimeError("rollback")
        assert conn.execute("SELECT status FROM synced_dialogs WHERE dialog_id=-5").fetchone() == ("synced",)
        assert conn.execute("SELECT COUNT(*) FROM entity_profile_refresh_state WHERE entity_id=12").fetchone() == (0,)
        with write_transaction(conn):
            assert not set_access_lost(
                conn, -5, 20, profile_owner_account_id=77, observation_order=9, membership_revision_at=20
            ).changed
            loss = set_access_lost(
                conn, -5, 20, profile_owner_account_id=42, observation_order=10, membership_revision_at=20
            )
            assert loss.changed and loss.membership_counts.queued == 1
            assert not set_access_lost(conn, -5, 21, profile_owner_account_id=42, observation_order=10).changed
            assert record_access_recovery_observation(conn, -5, 20)
            conn.execute("UPDATE entity_profile_refresh_state SET status='complete' WHERE entity_id=12")
            conn.execute(
                "UPDATE entity_detail_sections SET status='fresh',payload_json=? WHERE entity_id=12 AND section='common_chats'",
                ('{"published":{"owner_account_id":42,"generation":1,"scan":0,"observation_started_at":25}}',),
            )
            later = set_access_lost(
                conn, -5, 30, profile_owner_account_id=42, observation_order=30, membership_revision_at=30
            )
            assert later.changed and later.membership_counts.queued == 1
            assert not set_access_lost(conn, -5, 31, profile_owner_account_id=42, observation_order=30).changed
            assert record_access_recovery_observation(conn, -5, 40)
            assert restore_access_after_revalidation(conn, -5, 40, expected_observation_order=40)
            assert not set_access_lost(conn, -5, 41, profile_owner_account_id=42, observation_order=35).changed
        assert conn.execute("SELECT status FROM synced_dialogs WHERE dialog_id=-5").fetchone() != ("access_lost",)
        assert conn.execute("SELECT generation FROM entity_profile_refresh_state WHERE entity_id=12").fetchone() == (2,)
        assert conn.execute(
            "SELECT COUNT(*) FROM entity_profile_collection_membership WHERE entity_id=12"
        ).fetchone() == (1,)
    finally:
        conn.close()


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    _apply_migrations(conn)
    return conn


def test_nested_lifecycle_savepoint_preserves_outer_write() -> None:
    conn = _db()
    conn.execute("INSERT INTO synced_dialogs (dialog_id, status) VALUES (1, 'synced')")
    seed_full_history_enrollment(conn, 1, enabled=True)
    conn.execute(
        "INSERT INTO dialogs (dialog_id,hidden,needs_refresh,snapshot_at,archived,pinned,unread_mentions_count,unread_reactions_count,name) VALUES (1, 0, 0, 1, 0, 0, 0, 0, 'x')"
    )
    conn.execute("CREATE TABLE unrelated (value INTEGER)")
    conn.commit()
    try:
        with pytest.raises(RuntimeError, match="abort outer"):
            with write_transaction(conn):
                conn.execute("INSERT INTO unrelated VALUES (7)")
                set_access_lost(conn, 1, 10)
                stamp_access_revalidation(conn, 1, 11, 20)
                assert conn.in_transaction
                raise RuntimeError("abort outer")
        assert conn.execute("SELECT COUNT(*) FROM unrelated").fetchone() == (0,)
        assert conn.execute("SELECT status FROM synced_dialogs").fetchone() == ("synced",)
    finally:
        conn.close()


def test_access_restore_clears_read_position_retry() -> None:
    conn = _db()
    conn.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, read_position_next_attempt_at, read_position_attempt_count) "
        "VALUES (2, 'access_lost', 999, 3)"
    )
    seed_full_history_enrollment(conn, 2, enabled=True)
    conn.execute(
        "INSERT INTO dialogs (dialog_id,hidden,needs_refresh,snapshot_at,archived,pinned,unread_mentions_count,unread_reactions_count,name) VALUES (2, 1, 0, 1, 0, 0, 0, 0, 'x')"
    )
    conn.commit()
    try:
        restore_access_after_revalidation(conn, 2, 10)
        assert conn.execute(
            "SELECT read_position_next_attempt_at, read_position_attempt_count FROM synced_dialogs WHERE dialog_id=2"
        ).fetchone() == (None, 0)
    finally:
        conn.close()


def test_access_restore_rearms_terminal_reaction_details() -> None:
    conn = _db()
    conn.execute("INSERT INTO synced_dialogs (dialog_id, status) VALUES (9, 'access_lost')")
    seed_full_history_enrollment(conn, 9, enabled=True)
    conn.execute(
        "INSERT INTO dialogs (dialog_id,hidden,needs_refresh,snapshot_at,archived,pinned,unread_mentions_count,unread_reactions_count,name) VALUES (9, 1, 0, 1, 0, 0, 0, 0, 'x')"
    )
    conn.executemany(
        "INSERT INTO message_reaction_event_status "
        "(dialog_id, message_id, status, checked_at, next_offset, next_attempt_at, staged_count, failure_kind, display_generation) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (9, 1, "unavailable", 5, "old", None, 2, "access_lost", 4),
            (9, 2, "unavailable", 5, None, None, 0, "invalid_target", 6),
        ],
    )
    conn.execute(
        "INSERT INTO message_reaction_events "
        "(dialog_id, message_id, emoji, fetched_at, display_generation) VALUES (?,?,?,?,?)",
        (9, 2, "👍", 5, 0),
    )
    conn.commit()
    try:
        assert restore_access_after_revalidation(conn, 9, 12)
        assert conn.execute(
            "SELECT status, checked_at, next_offset, next_attempt_at, staged_count, failure_kind, display_generation "
            "FROM message_reaction_event_status WHERE dialog_id=9 AND message_id=1"
        ).fetchone() == ("stale", 12, None, 12, 0, None, 4)
        assert conn.execute(
            "SELECT status, next_attempt_at, failure_kind, display_generation "
            "FROM message_reaction_event_status WHERE dialog_id=9 AND message_id=2"
        ).fetchone() == ("stale", 12, None, 6)
        assert conn.execute(
            "SELECT COUNT(*) FROM message_reaction_events WHERE dialog_id=9 AND message_id=2 AND display_generation=0"
        ).fetchone() == (0,)
    finally:
        conn.close()


def test_complete_access_revalidation_keeps_access_lost_and_clears_retry() -> None:
    conn = _db()
    conn.execute(
        "INSERT INTO synced_dialogs "
        "(dialog_id, status, access_lost_at, access_next_revalidate_at) VALUES (4, 'access_lost', 10, 20)"
    )
    conn.commit()
    try:
        complete_access_revalidation(conn, 4, 30)
        assert conn.execute(
            "SELECT status, access_lost_at, access_last_revalidated_at, access_next_revalidate_at "
            "FROM synced_dialogs WHERE dialog_id=4"
        ).fetchone() == ("access_lost", 10, 30, None)
    finally:
        conn.close()


def test_access_loss_clears_read_position_retry() -> None:
    conn = _db()
    conn.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, read_position_next_attempt_at, read_position_attempt_count) "
        "VALUES (3, 'synced', 999, 3)"
    )
    conn.execute(
        "INSERT INTO dialogs (dialog_id,hidden,needs_refresh,snapshot_at,archived,pinned,unread_mentions_count,unread_reactions_count,name) VALUES (3, 0, 0, 1, 0, 0, 0, 0, 'x')"
    )
    conn.commit()
    try:
        set_access_lost(conn, 3, 10)
        assert conn.execute(
            "SELECT status, read_position_next_attempt_at, read_position_attempt_count "
            "FROM synced_dialogs WHERE dialog_id=3"
        ).fetchone() == ("access_lost", None, 0)
    finally:
        conn.close()


def test_durable_event_failure_rolls_back_lifecycle() -> None:
    conn = _db()
    conn.execute("INSERT INTO synced_dialogs (dialog_id, status) VALUES (1, 'synced')")
    seed_full_history_enrollment(conn, 1, enabled=True)
    conn.execute(
        "INSERT INTO dialogs (dialog_id,hidden,needs_refresh,snapshot_at,archived,pinned,unread_mentions_count,unread_reactions_count,name) VALUES (1, 0, 0, 1, 0, 0, 0, 0, 'x')"
    )
    conn.commit()
    conn.execute(
        "CREATE TRIGGER reject_history BEFORE INSERT ON conversation_history_events "
        "BEGIN SELECT RAISE(ABORT, 'injected'); END"
    )

    try:
        with pytest.raises(sqlite3.IntegrityError, match="injected"):
            set_access_lost(conn, 1, 10)
        assert conn.execute("SELECT status FROM synced_dialogs").fetchone() == ("synced",)
        assert conn.execute("SELECT hidden FROM dialogs").fetchone() == (0,)
    finally:
        conn.close()


def test_lifecycle_history_and_operational_logs_are_ordered_and_deduplicated(
    caplog: pytest.LogCaptureFixture,
) -> None:
    conn = _db()
    try:
        conn.execute("INSERT INTO synced_dialogs (dialog_id, status) VALUES (1, 'synced')")
        conn.execute(
            "INSERT INTO dialogs (dialog_id,hidden,needs_refresh,snapshot_at,archived,pinned,unread_mentions_count,unread_reactions_count,name) VALUES (1, 0, 0, 1, 0, 0, 0, 0, 'x')"
        )
        conn.commit()

        with caplog.at_level(logging.INFO, logger="mcp_telegram.access_lifecycle"):
            assert set_access_lost(
                conn,
                1,
                10,
                evidence=AccessLossEvidence("UpdateChannelParticipant", "removed_by_admin", 99),
            ).changed
            assert not set_access_lost(conn, 1, 11).changed
            assert restore_access_after_revalidation(conn, 1, 12)
            assert not restore_access_after_revalidation(conn, 1, 13)
            conn.execute("UPDATE synced_dialogs SET status = 'syncing' WHERE dialog_id = 1")
            conn.commit()
            assert set_access_lost(conn, 1, 14).changed
        assert conn.execute(
            "SELECT kind, occurred_at, reason_code, previous_status, access_change_cause, actor_id "
            "FROM conversation_history_events ORDER BY seq"
        ).fetchall() == [
            ("access_lost", 10, "UpdateChannelParticipant", "synced", "removed_by_admin", 99),
            ("access_restored", 12, None, "access_lost", None, None),
            ("access_lost", 14, None, "syncing", None, None),
        ]
        lifecycle_logs = [record.getMessage() for record in caplog.records]
        assert lifecycle_logs == [
            "access_lost dialog_id=1 reason_code=UpdateChannelParticipant",
            "access_restored dialog_id=1 total_messages=None",
            "access_lost dialog_id=1 reason_code=None",
        ]
    finally:
        conn.close()
