"""Focused tests for the finite, two-dialog topic projection repair."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mcp_telegram.message_contracts import ExtractedMessage, StoredMessage
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema
from mcp_telegram.topic_attribution_campaign import (
    TopicAttributionCampaignError,
    enroll_campaign,
    next_checkpoint,
    record_page,
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
def conn(tmp_path: Path) -> sqlite3.Connection:
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
    assert next_checkpoint(conn, now=101) == (101, 0)

    manifest = record_page(
        conn,
        101,
        0,
        [_extracted(101, 11, 7), _extracted(101, 12, 8), _extracted(101, 13, 9), _extracted(101, 14, None)],
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
    counts = manifest["dialogs"]["101"]["counts"]
    assert counts == {"attributed": 1, "no_topic": 0, "no_longer_needed": 2, "unresolved": 1}
    assert conn.execute(
        "SELECT topic_attribution_state,topic_attribution_completed_at FROM synced_dialogs WHERE dialog_id=101"
    ).fetchone() == ("partial", None)
    with pytest.raises(TopicAttributionCampaignError, match="checkpoint changed"):
        record_page(conn, 101, 0, ())
