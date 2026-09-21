"""Focused tests for the finite, two-dialog topic projection repair."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import cast

import pytest

from mcp_telegram.message_history.contracts import (
    MESSAGE_HISTORY_PAGE_LIMIT,
    TopicAttributionMessage,
    TopicAttributionPage,
)
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema
from mcp_telegram.topic_attribution_campaign import (
    CAMPAIGN_STATE_KEY,
    TopicAttributionCampaignError,
    abort_campaign,
    advance_campaign,
    campaign_release_at,
    campaign_status,
    enroll_campaign,
    record_access_lost,
    record_failed_attempt,
    record_page,
    reset_campaign,
    resume_campaign,
)
from tests.history_enrollment_helpers import seed_full_history_enrollment


def _message(_dialog_id: int, message_id: int, topic_id: int | None) -> TopicAttributionMessage:
    return TopicAttributionMessage(message_id=message_id, forum_topic_id=topic_id)


def _page(messages: Sequence[TopicAttributionMessage] = (), *, complete: bool | None = None) -> TopicAttributionPage:
    raw_messages = tuple(messages)
    return TopicAttributionPage(
        messages=raw_messages,
        next_cursor=min((message.message_id for message in raw_messages), default=None),
        complete=len(raw_messages) < MESSAGE_HISTORY_PAGE_LIMIT if complete is None else complete,
    )


@pytest.fixture()
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    db = _open_sync_db(path)
    db.executemany("INSERT INTO dialogs(dialog_id, type) VALUES (?, 'bot')", [(101,), (102,)])
    db.executemany("INSERT INTO synced_dialogs(dialog_id, status) VALUES (?, 'synced')", [(101,), (102,)])
    seed_full_history_enrollment(db, 101, enabled=True)
    seed_full_history_enrollment(db, 102, enabled=True)
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
        _page(
            [
                _message(101, 11, 7),
                _message(101, 12, 8),
                _message(101, 13, 9),
                _message(101, 14, None),
                _message(101, 15, 10),
            ]
        ),
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
    assert counts == {"attributed": 1, "no_topic": 1, "no_longer_needed": 2, "unresolved": 1}
    assert "success_pages" not in item
    assert conn.execute(
        "SELECT topic_attribution_state,topic_attribution_completed_at,topic_attribution_no_topic_count "
        "FROM synced_dialogs WHERE dialog_id=101"
    ).fetchone() == ("partial", None, 1)
    with pytest.raises(TopicAttributionCampaignError, match="checkpoint_changed"):
        record_page(conn, 101, 0, _page(), observed_at=103)


def test_campaign_expiry_is_pure_until_advance_then_restart_safe(conn: sqlite3.Connection) -> None:
    enroll_campaign(conn, [101, 102], now=100)
    assert campaign_release_at(conn, now=100 + 7 * 24 * 60 * 60) == 0.0
    assert campaign_status(conn)["state"] == "active"
    assert advance_campaign(conn, now=100 + 7 * 24 * 60 * 60) is None
    assert campaign_status(conn)["terminal_reason"] == "expiry"


def test_failure_keeps_checkpoint_and_delays_retry(conn: sqlite3.Connection) -> None:
    enroll_campaign(conn, [101, 102], now=100)
    record_failed_attempt(conn, 101, 0, reason="MessageHistoryUnavailableError", observed_at=110)
    assert advance_campaign(conn, now=111) == (102, 0)
    record_page(conn, 102, 0, _page(), observed_at=111)
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
    page = _page(
        [_message(101, message_id, None) for message_id in range(1, MESSAGE_HISTORY_PAGE_LIMIT + 1)],
        complete=False,
    )
    record_page(conn, 101, 0, page, observed_at=101)
    assert advance_campaign(conn, now=102) == (101, 1)


def test_stalled_local_projection_still_advances_from_raw_page_cursor(conn: sqlite3.Connection) -> None:
    enroll_campaign(conn, [101, 102], now=100)
    record_page(conn, 101, 0, _page([_message(101, 67, 9)], complete=False), observed_at=101)

    assert advance_campaign(conn, now=102) == (101, 67)
    assert campaign_status(conn)["counts"] == {
        "attributed": 0,
        "no_topic": 0,
        "no_longer_needed": 0,
        "unresolved": 1,
    }


def test_non_advancing_raw_cursor_terminalizes_without_reconciling_again(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO messages(dialog_id,message_id,sent_at,text,forum_topic_id,is_deleted) VALUES (101,7,1,'keep',NULL,0)"
    )
    conn.commit()
    enroll_campaign(conn, [101, 102], now=100)
    record_page(conn, 101, 0, _page([_message(101, 7, 9)], complete=False), observed_at=101)
    record_page(conn, 101, 7, _page([_message(101, 7, 10)], complete=False), observed_at=102)

    assert conn.execute("SELECT forum_topic_id FROM messages WHERE dialog_id=101 AND message_id=7").fetchone() == (9,)
    assert campaign_status(conn)["terminal_reason"] == "non_advancing_cursor"
    assert campaign_status(conn)["terminal_severity"] == "failed"


def test_resume_operator_abort_preserves_committed_progress_and_reactivates_only_aborted_items(
    conn: sqlite3.Connection,
) -> None:
    conn.execute(
        "INSERT INTO messages(dialog_id,message_id,sent_at,text,forum_topic_id,is_deleted) VALUES (101,11,1,'keep',NULL,0)"
    )
    conn.commit()
    enroll_campaign(conn, [101, 102], now=100)
    record_page(conn, 101, 0, _page([_message(101, 11, 4)]), observed_at=101)
    assert abort_campaign(conn) == {"terminal_reason": "operator_abort"}

    assert resume_campaign(conn, now=102) == {"resumed_dialogs": 1}
    manifest_row = cast(
        tuple[str] | None,
        conn.execute("SELECT value FROM daemon_state WHERE key=?", (CAMPAIGN_STATE_KEY,)).fetchone(),
    )
    assert manifest_row is not None
    manifest = cast(dict[str, object], json.loads(cast(str, manifest_row[0])))
    dialogs = cast(dict[str, dict[str, object]], manifest["dialogs"])
    assert dialogs["101"]["state"] == "done"
    assert dialogs["101"]["cursor"] == 11
    assert dialogs["101"]["counts"] == {"attributed": 1, "no_topic": 0, "no_longer_needed": 0, "unresolved": 0}
    assert dialogs["102"]["state"] == "pending"
    assert dialogs["102"]["cursor"] == 0


def test_committed_page_is_restart_safe_and_cannot_be_applied_twice(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO messages(dialog_id,message_id,sent_at,text,forum_topic_id,is_deleted) VALUES (101,11,1,'keep',NULL,0)"
    )
    conn.commit()
    enroll_campaign(conn, [101, 102], now=100)
    page = _page([_message(101, 11, None)], complete=False)
    record_page(conn, 101, 0, page, observed_at=101)
    conn.commit()
    db_path = Path(cast(str, conn.execute("PRAGMA database_list").fetchone()[2]))
    reopened = _open_sync_db(db_path)
    try:
        manifest = cast(
            dict[str, object],
            json.loads(
                cast(
                    str,
                    reopened.execute("SELECT value FROM daemon_state WHERE key=?", (CAMPAIGN_STATE_KEY,)).fetchone()[0],
                )
            ),
        )
        item = cast(dict[str, object], cast(dict[str, object], manifest["dialogs"])["101"])
        assert item["cursor"] == 11
        assert cast(dict[str, int], item["counts"])["no_topic"] == 1
    finally:
        reopened.close()
    with pytest.raises(TopicAttributionCampaignError, match="checkpoint_changed"):
        record_page(conn, 101, 0, page, observed_at=102)
    assert cast(dict[str, int], campaign_status(conn)["counts"])["no_topic"] == 1


def test_empty_incomplete_page_terminalizes_at_record_boundary(conn: sqlite3.Connection) -> None:
    enroll_campaign(conn, [101, 102], now=100)
    record_page(conn, 101, 0, _page((), complete=False), observed_at=101)

    assert campaign_status(conn)["terminal_reason"] == "empty_incomplete_page"
    assert campaign_status(conn)["terminal_severity"] == "failed"


def test_legacy_operator_abort_resumes_only_the_uncommitted_item(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO messages(dialog_id,message_id,sent_at,text,forum_topic_id,is_deleted) VALUES (101,11,1,'keep',NULL,0)"
    )
    conn.commit()
    enroll_campaign(conn, [101, 102], now=100)
    record_page(conn, 101, 0, _page([_message(101, 11, 4)]), observed_at=101)
    assert abort_campaign(conn) == {"terminal_reason": "operator_abort"}
    manifest = cast(
        dict[str, object],
        json.loads(
            cast(str, conn.execute("SELECT value FROM daemon_state WHERE key=?", (CAMPAIGN_STATE_KEY,)).fetchone()[0])
        ),
    )
    dialogs = cast(dict[str, dict[str, object]], manifest["dialogs"])
    dialogs["102"].pop("terminal_reason")
    conn.execute("UPDATE daemon_state SET value=? WHERE key=?", (json.dumps(manifest), CAMPAIGN_STATE_KEY))
    conn.commit()

    assert resume_campaign(conn, now=102) == {"resumed_dialogs": 1}
    resumed = cast(
        dict[str, object],
        json.loads(
            cast(str, conn.execute("SELECT value FROM daemon_state WHERE key=?", (CAMPAIGN_STATE_KEY,)).fetchone()[0])
        ),
    )
    resumed_dialogs = cast(dict[str, dict[str, object]], resumed["dialogs"])
    assert resumed_dialogs["101"]["state"] == "done"
    assert resumed_dialogs["101"]["cursor"] == 11
    assert resumed_dialogs["102"]["state"] == "pending"


def test_legacy_operator_abort_rejects_ambiguous_partial_item(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "INSERT INTO messages(dialog_id,message_id,sent_at,text,forum_topic_id,is_deleted) VALUES (?,?,?,?,?,?)",
        [(101, 11, 1, "first", None, 0), (102, 12, 1, "second", None, 0)],
    )
    conn.commit()
    enroll_campaign(conn, [101, 102], now=100)
    record_page(conn, 101, 0, _page([_message(101, 11, 4)], complete=False), observed_at=101)
    record_page(conn, 102, 0, _page([_message(102, 12, 5)], complete=False), observed_at=102)
    assert abort_campaign(conn) == {"terminal_reason": "operator_abort"}
    manifest = cast(
        dict[str, object],
        json.loads(
            cast(str, conn.execute("SELECT value FROM daemon_state WHERE key=?", (CAMPAIGN_STATE_KEY,)).fetchone()[0])
        ),
    )
    dialogs = cast(dict[str, dict[str, object]], manifest["dialogs"])
    dialogs["101"].pop("terminal_reason", None)
    dialogs["102"].pop("terminal_reason", None)
    conn.execute("UPDATE daemon_state SET value=? WHERE key=?", (json.dumps(manifest), CAMPAIGN_STATE_KEY))
    conn.commit()

    with pytest.raises(TopicAttributionCampaignError, match="legacy_resume_ambiguous"):
        resume_campaign(conn, now=102)


def test_legacy_operator_abort_resumes_unique_partial_receipt_with_preserved_progress(
    conn: sqlite3.Connection,
) -> None:
    conn.executemany(
        "INSERT INTO messages(dialog_id,message_id,sent_at,text,forum_topic_id,is_deleted) VALUES (?,?,?,?,?,?)",
        [(101, 500, 1, "complete", None, 0), (102, 304584, 1, "interrupted", None, 0)],
    )
    conn.commit()
    enroll_campaign(conn, [101, 102], now=100)
    record_page(conn, 101, 0, _page([_message(101, 500, 4)]), observed_at=101)
    record_page(conn, 102, 0, _page([_message(102, 304584, 5)], complete=False), observed_at=102)
    assert abort_campaign(conn) == {"terminal_reason": "operator_abort"}
    manifest = cast(
        dict[str, object],
        json.loads(
            cast(str, conn.execute("SELECT value FROM daemon_state WHERE key=?", (CAMPAIGN_STATE_KEY,)).fetchone()[0])
        ),
    )
    dialogs = cast(dict[str, dict[str, object]], manifest["dialogs"])
    dialogs["102"].pop("terminal_reason")
    conn.execute("UPDATE daemon_state SET value=? WHERE key=?", (json.dumps(manifest), CAMPAIGN_STATE_KEY))
    conn.commit()

    assert resume_campaign(conn, now=103) == {"resumed_dialogs": 1}

    resumed = cast(
        dict[str, object],
        json.loads(
            cast(str, conn.execute("SELECT value FROM daemon_state WHERE key=?", (CAMPAIGN_STATE_KEY,)).fetchone()[0])
        ),
    )
    resumed_dialogs = cast(dict[str, dict[str, object]], resumed["dialogs"])
    assert resumed_dialogs["101"] == dialogs["101"]
    assert resumed_dialogs["102"]["state"] == "pending"
    assert resumed_dialogs["102"]["cursor"] == 304584
    assert resumed_dialogs["102"]["counts"] == {
        "attributed": 1,
        "no_topic": 0,
        "no_longer_needed": 0,
        "unresolved": 0,
    }
    assert conn.execute("SELECT topic_attribution_state FROM synced_dialogs WHERE dialog_id=101").fetchone() == (
        "complete",
    )
    assert conn.execute("SELECT topic_attribution_state FROM synced_dialogs WHERE dialog_id=102").fetchone() == (
        "partial",
    )


@pytest.mark.parametrize("corruption", ["partial_without_progress", "unknown_committed", "other_terminal"])
def test_legacy_operator_abort_rejects_nonunique_or_untrusted_resume_state(
    conn: sqlite3.Connection, corruption: str
) -> None:
    conn.execute(
        "INSERT INTO messages(dialog_id,message_id,sent_at,text,forum_topic_id,is_deleted) VALUES (101,11,1,'keep',NULL,0)"
    )
    conn.commit()
    enroll_campaign(conn, [101, 102], now=100)
    record_page(conn, 101, 0, _page([_message(101, 11, 4)], complete=False), observed_at=101)
    assert abort_campaign(conn) == {"terminal_reason": "operator_abort"}
    manifest = cast(
        dict[str, object],
        json.loads(
            cast(str, conn.execute("SELECT value FROM daemon_state WHERE key=?", (CAMPAIGN_STATE_KEY,)).fetchone()[0])
        ),
    )
    dialogs = cast(dict[str, dict[str, object]], manifest["dialogs"])
    dialogs["101"].pop("terminal_reason")
    dialogs["102"].pop("terminal_reason")
    if corruption == "partial_without_progress":
        dialogs["101"]["cursor"] = 0
        dialogs["101"]["counts"] = {"attributed": 0, "no_topic": 0, "no_longer_needed": 0, "unresolved": 0}
    elif corruption == "unknown_committed":
        conn.execute("UPDATE synced_dialogs SET topic_attribution_state='unknown' WHERE dialog_id=101")
    else:
        dialogs["101"]["terminal_reason"] = "access_lost"
    conn.execute("UPDATE daemon_state SET value=? WHERE key=?", (json.dumps(manifest), CAMPAIGN_STATE_KEY))
    conn.commit()

    with pytest.raises(TopicAttributionCampaignError, match="legacy_resume_ambiguous"):
        resume_campaign(conn, now=102)


def test_operator_abort_cannot_resume_after_expiry(conn: sqlite3.Connection) -> None:
    enroll_campaign(conn, [101, 102], now=100)
    assert abort_campaign(conn) == {"terminal_reason": "operator_abort"}

    with pytest.raises(TopicAttributionCampaignError, match="campaign_expired"):
        resume_campaign(conn, now=100 + 7 * 24 * 60 * 60)


def test_access_lost_status_is_skipped_without_history_acquisition(conn: sqlite3.Connection) -> None:
    enroll_campaign(conn, [101, 102], now=100)
    conn.execute("UPDATE synced_dialogs SET status='access_lost' WHERE dialog_id=101")
    conn.commit()
    assert advance_campaign(conn, now=101) == (102, 0)
    assert campaign_status(conn)["abandoned_dialogs"] == 1


def test_terminal_status_preserves_access_loss_over_exhausted(conn: sqlite3.Connection) -> None:
    enroll_campaign(conn, [101, 102], now=100)
    record_access_lost(conn, 101, 0, observed_at=101)
    record_page(conn, 102, 0, _page(), observed_at=102)

    assert campaign_status(conn) == {
        "state": "complete",
        "terminal_reason": "access_lost",
        "terminal_severity": "degraded",
        "dialog_count": 2,
        "pending_dialogs": 0,
        "failed_dialogs": 0,
        "abandoned_dialogs": 1,
        "counts": {"attributed": 0, "no_topic": 0, "no_longer_needed": 0, "unresolved": 0},
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
    record_page(conn, 102, 0, _page(), observed_at=103)
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
    assert reset_campaign(conn) == {"previous_terminal_reason": "expiry"}
    reset_record = next(
        record for record in caplog.records if record.message.startswith("topic_attribution_campaign_reset")
    )
    assert reset_record.getMessage() == "topic_attribution_campaign_reset prior_terminal_reason=expiry"
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


def test_campaign_rejects_disabled_enrollment_and_abandons_midflight(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE full_history_enrollment SET enabled=0 WHERE dialog_id=101")
    conn.commit()
    with pytest.raises(TopicAttributionCampaignError, match="enabled full-history"):
        enroll_campaign(conn, [101, 102], now=100)
    conn.execute("UPDATE full_history_enrollment SET enabled=1 WHERE dialog_id=101")
    conn.commit()
    enroll_campaign(conn, [101, 102], now=100)
    conn.execute("UPDATE full_history_enrollment SET enabled=0 WHERE dialog_id=101")
    conn.commit()
    assert advance_campaign(conn, now=101) == (102, 0)
    assert campaign_status(conn)["abandoned_dialogs"] == 1


def test_valid_json_with_incomplete_manifest_is_quarantined_on_mutation(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO daemon_state(key,value) VALUES (?,?)",
        (CAMPAIGN_STATE_KEY, json.dumps({"version": 1, "state": "active", "dialog_ids": [101, 102], "dialogs": {}})),
    )
    conn.commit()
    assert campaign_status(conn)["state"] == "invalid"
    assert advance_campaign(conn, now=100) is None
    assert campaign_status(conn)["terminal_reason"] == "invalid_manifest"


def test_active_campaign_can_be_aborted_then_reset_for_reenrollment(conn: sqlite3.Connection) -> None:
    enroll_campaign(conn, [101, 102], now=100)
    assert abort_campaign(conn) == {"terminal_reason": "operator_abort"}
    with pytest.raises(TopicAttributionCampaignError, match="not_active"):
        abort_campaign(conn)
    assert reset_campaign(conn) == {"previous_terminal_reason": "operator_abort"}
    assert enroll_campaign(conn, [101, 102], now=200)["state"] == "active"


def test_campaign_rejects_hidden_dialog_and_persists_midflight_abandonment(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE dialogs SET hidden=1 WHERE dialog_id=101")
    conn.commit()
    with pytest.raises(TopicAttributionCampaignError, match="synced bot"):
        enroll_campaign(conn, [101, 102], now=100)
    conn.execute("UPDATE dialogs SET hidden=0 WHERE dialog_id=101")
    conn.commit()
    enroll_campaign(conn, [101, 102], now=100)
    conn.execute("UPDATE dialogs SET hidden=1 WHERE dialog_id=101")
    conn.commit()
    assert advance_campaign(conn, now=101) == (102, 0)
    assert campaign_status(conn)["abandoned_dialogs"] == 1


@pytest.mark.parametrize(
    "manifest_patch",
    [
        {"expires_at": "later"},
        {"dialogs": {"101": {"cursor": "bad"}}},
        {"dialogs": {"101": {"next_retry_at": "bad"}}},
        {"dialogs": {"101": {"counts": {"unresolved": True}}}},
    ],
)
def test_invalid_numeric_manifest_is_quarantined_on_mutation(
    conn: sqlite3.Connection, manifest_patch: dict[str, object]
) -> None:
    manifest = enroll_campaign(conn, [101, 102], now=100)
    dialogs = manifest["dialogs"]
    assert isinstance(dialogs, dict)
    for key, value in manifest_patch.items():
        if key == "dialogs":
            dialog_changes = cast(dict[str, dict[str, object]], value)
            for dialog_id, changes in dialog_changes.items():
                assert isinstance(changes, dict)
                item = dialogs[dialog_id]
                assert isinstance(item, dict)
                for field, replacement in changes.items():
                    if field == "counts":
                        counts = item["counts"]
                        assert isinstance(counts, dict)
                        counts.update(cast(dict[str, object], replacement))
                    else:
                        item[field] = replacement
        else:
            manifest[key] = value
    conn.execute("UPDATE daemon_state SET value=? WHERE key=?", (json.dumps(manifest), CAMPAIGN_STATE_KEY))
    conn.commit()
    assert campaign_status(conn)["state"] == "invalid"
    assert advance_campaign(conn, now=101) is None
    assert campaign_status(conn)["terminal_reason"] == "invalid_manifest"


def test_clean_campaign_dialog_completes_with_legal_no_topic_while_sibling_is_abandoned(
    conn: sqlite3.Connection,
) -> None:
    conn.execute(
        "INSERT INTO messages(dialog_id,message_id,sent_at,text,forum_topic_id,is_deleted) VALUES (101,1,1,'root',NULL,0)"
    )
    conn.commit()
    enroll_campaign(conn, [101, 102], now=100)
    record_page(conn, 101, 0, _page([_message(101, 1, None)]), observed_at=101)
    assert conn.execute(
        "SELECT topic_attribution_state,topic_attribution_no_topic_count FROM synced_dialogs WHERE dialog_id=101"
    ).fetchone() == ("complete", 1)
    record_access_lost(conn, 102, 0, observed_at=102)
    assert campaign_status(conn)["terminal_severity"] == "degraded"


def test_exhausted_campaign_with_missing_local_row_is_degraded(conn: sqlite3.Connection) -> None:
    enroll_campaign(conn, [101, 102], now=100)
    record_page(conn, 101, 0, _page([_message(101, 99, 7)]), observed_at=101)
    record_page(conn, 102, 0, _page(), observed_at=102)
    status = campaign_status(conn)
    assert status["terminal_reason"] == "exhausted"
    assert status["terminal_severity"] == "degraded"
    counts = cast(dict[str, int], status["counts"])
    assert counts["unresolved"] == 1
