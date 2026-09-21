"""Focused tests for the finite, two-dialog topic projection repair."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from mcp_telegram.message_contracts import ExtractedMessage, StoredMessage
from mcp_telegram.message_history.contracts import HISTORY_PAGE_SIZE
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema
from mcp_telegram.topic_attribution_campaign import (
    CAMPAIGN_STATE_KEY,
    TopicAttributionCampaignError,
    advance_campaign,
    campaign_release_at,
    campaign_status,
    enroll_campaign,
    record_access_lost,
    record_failed_attempt,
    record_page,
    reset_campaign,
)


def _extracted(dialog_id: int, message_id: int, topic_id: int | None) -> ExtractedMessage:
    return ExtractedMessage(
        message=StoredMessage(
            dialog_id=dialog_id,
            message_id=message_id,
            sent_at=1,
            text="remote text is never written by this campaign",
            sender_id=None,
            sender_first_name=None,
            reply_to_msg_id=None,
            forum_topic_id=topic_id,
            edit_date=None,
            grouped_id=None,
            reply_to_peer_id=None,
            out=0,
            is_service=0,
            post_author=None,
        ),
        reply_count=0,
    )


@pytest.fixture()
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    db = _open_sync_db(path)
    db.executemany("INSERT INTO dialogs(dialog_id, type) VALUES (?, 'bot')", [(101,), (102,)])
    db.executemany("INSERT INTO synced_dialogs(dialog_id, status) VALUES (?, 'synced')", [(101,), (102,)])
    db.commit()
    yield db
    db.close()


def test_campaign_requires_exactly_two_currently_synced_bot_dialogs(conn: sqlite3.Connection) -> None:
    with pytest.raises(TopicAttributionCampaignError, match="exactly two"):
        enroll_campaign(conn, [101])
    conn.execute("UPDATE dialogs SET type='user' WHERE dialog_id=102")
    conn.commit()
    with pytest.raises(TopicAttributionCampaignError, match="synced bot"):
        enroll_campaign(conn, [101, 102])


def test_campaign_updates_only_live_null_topic_and_keeps_remote_omission_unresolved(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "INSERT INTO messages(dialog_id,message_id,sent_at,text,forum_topic_id,is_deleted) VALUES (?,?,?,?,?,?)",
        [
            (101, 11, 1, "keep this text", None, 0),
            (101, 12, 1, "tombstone", None, 1),
            (101, 13, 1, "existing topic", 3, 0),
            (101, 14, 1, "unknown omission", None, 0),
        ],
    )
    conn.commit()
    enroll_campaign(conn, [101, 102], now=100)
    assert campaign_release_at(conn, now=101) == 0.0
    assert advance_campaign(conn, now=101) == (101, 0)

    manifest = record_page(
        conn,
        101,
        0,
        [
            _extracted(101, 11, 7),
            _extracted(101, 12, 8),
            _extracted(101, 13, 9),
            _extracted(101, 14, None),
            _extracted(101, 15, 10),
        ],
        observed_at=102,
    )

    assert conn.execute(
        "SELECT message_id,text,forum_topic_id,is_deleted FROM messages WHERE dialog_id=101 ORDER BY message_id"
    ).fetchall() == [
        (11, "keep this text", 7, 0),
        (12, "tombstone", None, 1),
        (13, "existing topic", 3, 0),
        (14, "unknown omission", None, 0),
    ]
    dialogs = manifest["dialogs"]
    assert isinstance(dialogs, dict)
    item = dialogs["101"]
    assert isinstance(item, dict)
    counts = item["counts"]
    assert counts == {"attributed": 1, "no_longer_needed": 2, "unresolved": 2}
    assert "success_pages" not in item
    assert conn.execute(
        "SELECT topic_attribution_state,topic_attribution_completed_at FROM synced_dialogs WHERE dialog_id=101"
    ).fetchone() == ("partial", None)
    with pytest.raises(TopicAttributionCampaignError, match="checkpoint_changed"):
        record_page(conn, 101, 0, (), observed_at=103)


def test_campaign_expiry_is_pure_until_advance_then_restart_safe(conn: sqlite3.Connection) -> None:
    enroll_campaign(conn, [101, 102], now=100)
    assert campaign_release_at(conn, now=100 + 7 * 24 * 60 * 60) == 0.0
    assert campaign_status(conn)["state"] == "active"
    assert advance_campaign(conn, now=100 + 7 * 24 * 60 * 60) is None
    assert campaign_status(conn)["terminal_reason"] == "deadline"


def test_failure_keeps_checkpoint_and_delays_retry(conn: sqlite3.Connection) -> None:
    enroll_campaign(conn, [101, 102], now=100)
    record_failed_attempt(conn, 101, 0, reason="MessageHistoryUnavailableError", observed_at=110)
    assert advance_campaign(conn, now=111) == (102, 0)
    record_page(conn, 102, 0, (), observed_at=111)
    assert campaign_release_at(conn, now=111) == 170.0
    assert advance_campaign(conn, now=111) is None
    assert advance_campaign(conn, now=170) == (101, 0)


def test_ready_second_dialog_is_not_blocked_by_delayed_first_dialog(conn: sqlite3.Connection) -> None:
    enroll_campaign(conn, [101, 102], now=100)
    record_failed_attempt(conn, 101, 0, reason="MessageHistoryUnavailableError", observed_at=100)
    assert campaign_release_at(conn, now=101) == 0.0
    assert advance_campaign(conn, now=101) == (102, 0)


def test_successful_full_page_keeps_resumable_cursor_without_page_cap(conn: sqlite3.Connection) -> None:
    enroll_campaign(conn, [101, 102], now=100)
    page = [_extracted(101, message_id, None) for message_id in range(1, HISTORY_PAGE_SIZE + 1)]
    record_page(conn, 101, 0, page, observed_at=101)
    assert advance_campaign(conn, now=102) == (101, 1)


def test_access_lost_status_is_skipped_without_history_acquisition(conn: sqlite3.Connection) -> None:
    enroll_campaign(conn, [101, 102], now=100)
    conn.execute("UPDATE synced_dialogs SET status='access_lost' WHERE dialog_id=101")
    conn.commit()
    assert advance_campaign(conn, now=101) == (102, 0)


def test_terminal_status_preserves_access_loss_over_exhausted(conn: sqlite3.Connection) -> None:
    enroll_campaign(conn, [101, 102], now=100)
    record_access_lost(conn, 101, 0, observed_at=101)
    record_page(conn, 102, 0, (), observed_at=102)

    assert campaign_status(conn) == {
        "state": "complete",
        "terminal_reason": "access_lost",
        "terminal_severity": "degraded",
        "dialog_count": 2,
        "pending_dialogs": 0,
        "failed_dialogs": 0,
        "abandoned_dialogs": 1,
        "counts": {"attributed": 0, "no_longer_needed": 0, "unresolved": 0},
    }


def test_failure_limit_is_visible_in_terminal_status(conn: sqlite3.Connection) -> None:
    manifest = enroll_campaign(conn, [101, 102], now=100)
    manifest["max_failures_per_dialog"] = 1
    conn.execute(
        "UPDATE daemon_state SET value=? WHERE key=?",
        (json.dumps(manifest), CAMPAIGN_STATE_KEY),
    )
    conn.commit()
    record_failed_attempt(conn, 101, 0, reason="MessageHistoryUnavailableError", observed_at=101)

    status = campaign_status(conn)
    assert status["state"] == "active"
    assert status["failed_dialogs"] == 1
    assert advance_campaign(conn, now=102) == (102, 0)
    record_page(conn, 102, 0, (), observed_at=103)
    status = campaign_status(conn)
    assert status["terminal_reason"] == "failure_limit"
    assert status["terminal_severity"] == "failed"
    assert status["failed_dialogs"] == 1


def test_terminal_campaign_can_be_reset_then_explicitly_reenrolled(
    conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="mcp_telegram.topic_attribution_campaign")
    enroll_campaign(conn, [101, 102], now=100)
    with pytest.raises(TopicAttributionCampaignError, match="not_terminal"):
        reset_campaign(conn)
    assert advance_campaign(conn, now=100 + 7 * 24 * 60 * 60) is None
    assert reset_campaign(conn) == {"previous_terminal_reason": "deadline"}
    reset_record = next(record for record in caplog.records if record.message.startswith("topic_attribution_campaign_reset"))
    assert reset_record.getMessage() == "topic_attribution_campaign_reset prior_terminal_reason=deadline"
    assert campaign_status(conn)["state"] == "none"
    assert enroll_campaign(conn, [101, 102], now=200)["state"] == "active"


def test_campaign_eligibility_uses_canonical_bot_dialog_type(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE dialogs SET type='Bot' WHERE dialog_id=102")
    conn.commit()
    assert enroll_campaign(conn, [101, 102], now=100)["state"] == "active"


def test_invalid_manifest_is_purely_visible_then_quarantined_by_advance(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO daemon_state(key,value) VALUES (?,?)", (CAMPAIGN_STATE_KEY, "not-json"))
    conn.commit()
    assert campaign_release_at(conn, now=100) == 0.0
    assert campaign_status(conn)["state"] == "invalid"
    assert advance_campaign(conn, now=100) is None
    assert campaign_status(conn)["state"] == "complete"
    assert campaign_status(conn)["terminal_reason"] == "invalid_manifest"
