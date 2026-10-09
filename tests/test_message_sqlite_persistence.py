"""Focused tests for transaction-neutral event message persistence."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from mcp_telegram.fts import stem_text
from mcp_telegram.hydration_queue import HydrationPriority
from mcp_telegram.message_contracts import ExtractedMessage, StoredMessage
from mcp_telegram.messages.sqlite_bundle import (
    find_unique_incoming_human_dm_dialogs,
    insert_messages_with_fts,
    list_undeleted_message_ids,
    mark_message_deleted,
    persist_edited_message,
    persist_transcribed_text,
    read_message_text,
)
from mcp_telegram.messages.sqlite_hydration import (
    apply_message_transcription,
    stage_message_transcription,
    upsert_message_transcription,
)
from mcp_telegram.messages.sqlite_hydration_jobs import (
    _REPAIR_MEDIA_METADATA_CONTACT_OTHER_SQL,
    _REPAIR_MEDIA_METADATA_VIDEO_SQL,
    _REPAIR_TRANSCRIPTION_CANDIDATES_FROM_SQL,
    _TRANSCRIBABLE_MEDIA_SQL,
    HydrationRepairCursor,
    TranscriptionHydrationRepair,
    _is_transcribable_media_pair,
    _repair_raw_page,
    reconcile_fact_hydration_jobs_for_dialog,
    repair_media_metadata_hydration_jobs,
    repair_transcription_hydration_jobs,
    transcription_hydration_eligible,
)
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema
from mcp_telegram.sync_transactions import enable_runtime_writes, write_transaction


def _message(  # noqa: PLR0913
    message_id: int,
    *,
    text: str | None,
    sent_at: int = 100,
    media_kind: str | None = None,
    media_payload: str | None = None,
    out: int = 0,
) -> ExtractedMessage:
    return ExtractedMessage(
        message=StoredMessage(
            dialog_id=42,
            message_id=message_id,
            sent_at=sent_at,
            text=text,
            sender_id=42,
            sender_first_name="Test",
            reply_to_msg_id=None,
            forum_topic_id=None,
            edit_date=None,
            grouped_id=None,
            reply_to_peer_id=None,
            out=out,
            is_service=0,
            post_author=None,
            media_kind=media_kind,
            media_payload=media_payload,
        ),
        reply_count=0,
    )


@pytest.fixture()
def conn(tmp_path: Path):
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    connection = _open_sync_db(path)
    enable_runtime_writes(connection)
    try:
        yield connection
    finally:
        connection.close()


def _seed_human_dm(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT OR IGNORE INTO dialogs(dialog_id, type) VALUES (42, 'user')")
    conn.execute("INSERT OR IGNORE INTO entities(id, type, updated_at) VALUES (42, 'user', 1)")


def test_read_message_text_distinguishes_missing_from_null(conn: sqlite3.Connection) -> None:
    missing = read_message_text(conn, 42, 1)
    assert missing.found is False
    assert missing.text is None

    with write_transaction(conn):
        insert_messages_with_fts(conn, [_message(1, text=None)])
    null_text = read_message_text(conn, 42, 1)
    assert null_text.found is True
    assert null_text.text is None


def test_persist_edited_message_versions_sequentially_and_refreshes_fts(conn: sqlite3.Connection) -> None:
    with write_transaction(conn):
        _seed_human_dm(conn)
        insert_messages_with_fts(conn, [_message(10, text="first")])
    with write_transaction(conn):
        assert persist_edited_message(conn, _message(10, text="second"), old_text="first", edit_date=200) == 1
    with write_transaction(conn):
        assert persist_edited_message(conn, _message(10, text="third"), old_text="second", edit_date=300) == 2

    assert conn.execute(
        "SELECT version, old_text FROM message_versions WHERE dialog_id=42 AND message_id=10 ORDER BY version"
    ).fetchall() == [(1, "first"), (2, "second")]
    assert conn.execute("SELECT text FROM messages WHERE dialog_id=42 AND message_id=10").fetchone() == ("third",)
    assert conn.execute("SELECT stemmed_text FROM messages_fts WHERE dialog_id=42 AND message_id=10").fetchone() == (
        stem_text("third"),
    )


def test_persist_edited_message_unchanged_is_noop(conn: sqlite3.Connection) -> None:
    with write_transaction(conn):
        insert_messages_with_fts(conn, [_message(11, text="same")])
    with write_transaction(conn):
        assert persist_edited_message(conn, _message(11, text="same"), old_text="same", edit_date=200) is None
    assert conn.execute("SELECT COUNT(*) FROM message_versions").fetchone() == (0,)


def test_edit_alert_policy_accepts_only_incoming_confirmed_human_dm(conn: sqlite3.Connection) -> None:
    with write_transaction(conn):
        conn.execute("INSERT INTO dialogs(dialog_id, type) VALUES (42, 'user')")
        conn.execute("INSERT INTO entities(id, type, updated_at) VALUES (42, 'user', 1)")
        insert_messages_with_fts(conn, [_message(20, text="first"), _message(21, text="own", out=1)])
    with write_transaction(conn):
        assert persist_edited_message(conn, _message(20, text="second"), old_text="first", edit_date=200) == 1
        assert persist_edited_message(conn, _message(21, text="changed", out=1), old_text="own", edit_date=201) is None
    assert conn.execute("SELECT message_id FROM message_versions ORDER BY message_id").fetchall() == [(20,)]
    assert conn.execute(
        "SELECT kind,dialog_id,message_id FROM conversation_history_events ORDER BY seq"
    ).fetchall() == [("edit", 42, 20)]


@pytest.mark.parametrize(
    ("dialog_type", "entity_type"),
    [
        ("bot", "bot"),
        ("group", None),
        ("supergroup", None),
        ("forum", None),
        ("channel", None),
        ("service", "service"),
        (None, None),
        ("user", "bot"),
    ],
)
def test_irrelevant_edit_updates_message_without_storing_history(
    conn: sqlite3.Connection, dialog_type: str | None, entity_type: str | None
) -> None:
    with write_transaction(conn):
        conn.execute("INSERT INTO dialogs(dialog_id, type) VALUES (42, ?)", (dialog_type,))
        if entity_type is not None:
            conn.execute("INSERT INTO entities(id, type, updated_at) VALUES (42, ?, 1)", (entity_type,))
        insert_messages_with_fts(conn, [_message(23, text="before")])
        assert persist_edited_message(conn, _message(23, text="after"), old_text="before", edit_date=301) is None
    assert conn.execute("SELECT text FROM messages WHERE message_id=23").fetchone() == ("after",)
    assert conn.execute("SELECT COUNT(*) FROM message_versions WHERE message_id=23").fetchone() == (0,)
    assert conn.execute("SELECT COUNT(*) FROM conversation_history_events WHERE message_id=23").fetchone() == (0,)


def test_transcription_creates_neither_version_nor_change_alert(conn: sqlite3.Connection) -> None:
    with write_transaction(conn):
        conn.execute("INSERT INTO dialogs(dialog_id, type) VALUES (42, 'user')")
        insert_messages_with_fts(conn, [_message(22, text=None)])
        assert persist_transcribed_text(conn, 42, 22, old_text=None, transcribed_text="local transcript")
    assert conn.execute("SELECT COUNT(*) FROM message_versions WHERE message_id=22").fetchone() == (0,)
    assert conn.execute("SELECT COUNT(*) FROM conversation_history_events").fetchone() == (0,)


def _make_hydration_eligible(conn: sqlite3.Connection, status: str = "synced") -> None:
    with write_transaction(conn):
        conn.execute("INSERT INTO synced_dialogs(dialog_id, status) VALUES (42, ?)", (status,))
        conn.execute(
            "INSERT INTO full_history_enrollment(dialog_id, enabled, source, updated_at) VALUES (42, 1, 'explicit', 1)"
        )


@pytest.mark.parametrize("media_kind", ["contact", "other"])
def test_message_persistence_enqueues_one_unresolved_job_and_preserves_attempts(
    conn: sqlite3.Connection, media_kind: str
) -> None:
    _make_hydration_eligible(conn)
    with write_transaction(conn):
        conn.execute(
            "INSERT INTO hydration_jobs(kind, dialog_id, message_id, due_at, attempts) "
            "VALUES ('media_metadata', 42, 90, 100, 2)"
        )
    with write_transaction(conn):
        insert_messages_with_fts(
            conn,
            [_message(90, text=None, media_kind=media_kind, media_payload="{}")],
            priority=HydrationPriority.FOREGROUND,
        )
        insert_messages_with_fts(
            conn,
            [_message(90, text=None, media_kind=media_kind, media_payload="{}")],
            priority=HydrationPriority.FOREGROUND,
        )
    assert conn.execute("SELECT COUNT(*) FROM messages WHERE dialog_id=42 AND message_id=90").fetchone() == (1,)
    assert conn.execute("SELECT kind, dialog_id, message_id, attempts, priority FROM hydration_jobs").fetchall() == [
        ("media_metadata", 42, 90, 2, 0)
    ]


def test_foreground_voice_persistence_keeps_transcription_foreground(conn: sqlite3.Connection) -> None:
    _make_hydration_eligible(conn)
    with write_transaction(conn):
        insert_messages_with_fts(
            conn,
            [_message(93, text=None, media_kind="voice", media_payload="{}")],
            priority=HydrationPriority.FOREGROUND,
        )

    assert conn.execute("SELECT kind, priority FROM hydration_jobs").fetchall() == [("transcription", 1)]


def test_foreground_round_video_persistence_enqueues_transcription(conn: sqlite3.Connection) -> None:
    _make_hydration_eligible(conn)
    with write_transaction(conn):
        insert_messages_with_fts(
            conn,
            [_message(94, text=None, media_kind="video", media_payload='{"round_message":true}')],
            priority=HydrationPriority.FOREGROUND,
        )

    assert conn.execute("SELECT kind, priority FROM hydration_jobs").fetchall() == [("transcription", 1)]
    assert transcription_hydration_eligible(conn, 42, 94)


def test_plain_video_is_not_admitted_to_transcription(conn: sqlite3.Connection) -> None:
    _make_hydration_eligible(conn)
    with write_transaction(conn):
        insert_messages_with_fts(
            conn,
            [_message(95, text=None, media_kind="video", media_payload='{"duration":12}')],
            priority=HydrationPriority.FOREGROUND,
        )

    assert conn.execute("SELECT COUNT(*) FROM hydration_jobs").fetchone() == (0,)
    assert not transcription_hydration_eligible(conn, 42, 95)


def test_media_metadata_repair_is_bounded_newest_first_and_terminal_safe(conn: sqlite3.Connection) -> None:
    _make_hydration_eligible(conn)
    with write_transaction(conn):
        conn.executemany(
            "INSERT INTO messages(dialog_id, message_id, sent_at, text, media_kind, media_payload, is_deleted) "
            "VALUES (42, ?, ?, NULL, ?, ?, ?)",
            [
                (101, 100, "other", "{}", 0),
                (102, 200, "video", '{"duration": 2}', 0),
                (103, 300, "other", "{}", 1),
                (104, 400, "video", '{"round_message":false}', 0),
                (105, 500, "video", '{"duration": 2}', 0),
                (106, 150, "other", "{}", 0),
                (107, 550, "other", "{}", 0),
            ],
        )
        conn.execute(
            "INSERT INTO hydration_jobs(kind, dialog_id, message_id, due_at, attempts, terminal) "
            "VALUES ('media_metadata', 42, 107, 1, 3, 1)"
        )

    with write_transaction(conn):
        first = repair_media_metadata_hydration_jobs(conn, due_at=900, max_jobs=2)
    assert first.has_more is True
    assert first.next_contact_other_cursor == HydrationRepairCursor(550, 42, 107)
    assert first.next_video_cursor == HydrationRepairCursor(500, 42, 105)
    assert conn.execute(
        "SELECT message_id FROM hydration_jobs WHERE kind = 'media_metadata' AND terminal = 0 "
        "ORDER BY message_sent_at DESC"
    ).fetchall() == [(105,)]

    with write_transaction(conn):
        second = repair_media_metadata_hydration_jobs(
            conn,
            due_at=901,
            max_jobs=2,
            contact_other_cursor=first.next_contact_other_cursor,
            video_cursor=first.next_video_cursor,
        )
    assert second.has_more is True
    with write_transaction(conn):
        third = repair_media_metadata_hydration_jobs(
            conn,
            due_at=902,
            max_jobs=2,
            contact_other_cursor=second.next_contact_other_cursor,
            video_cursor=second.next_video_cursor,
        )
    assert third.has_more is False
    assert conn.execute(
        "SELECT message_id FROM hydration_jobs WHERE kind = 'media_metadata' AND terminal = 0 "
        "ORDER BY message_sent_at DESC"
    ).fetchall() == [(105,), (102,), (106,), (101,)]
    assert conn.execute("SELECT due_at, attempts, terminal FROM hydration_jobs WHERE message_id = 107").fetchone() == (
        1,
        3,
        1,
    )
    with write_transaction(conn):
        fourth = repair_media_metadata_hydration_jobs(
            conn,
            due_at=903,
            max_jobs=2,
            contact_other_cursor=third.next_contact_other_cursor,
            video_cursor=third.next_video_cursor,
        )
    assert fourth.has_more is False

    for index_name in (
        "idx_messages_media_unresolved_contact_other",
        "idx_messages_media_unresolved_video",
    ):
        row = cast(tuple[str], conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (index_name,)).fetchone())
        sql = row[0]
        assert "WHERE" in sql


def test_media_metadata_repair_plan_uses_both_partial_indexes_without_sort(conn: sqlite3.Connection) -> None:
    selection = (
        "SELECT 'media_metadata', m.dialog_id, m.message_id, 900, 0, 0, m.sent_at, 0 "
        f"{_REPAIR_MEDIA_METADATA_CONTACT_OTHER_SQL} "
        "UNION ALL "
        "SELECT 'media_metadata', m.dialog_id, m.message_id, 900, 0, 0, m.sent_at, 0 "
        f"{_REPAIR_MEDIA_METADATA_VIDEO_SQL} "
        "ORDER BY 7 DESC, 2, 3 LIMIT 2"
    )
    plan = cast(list[tuple[object, ...]], conn.execute("EXPLAIN QUERY PLAN " + selection).fetchall())
    details = " ".join(str(row[3]) for row in plan)
    assert "idx_messages_media_unresolved_contact_other" in details
    assert "idx_messages_media_unresolved_video" in details
    assert "SCAN messages" not in details
    assert "USE TEMP B-TREE" not in details


def test_historical_transcription_repair_and_dialog_reconciliation_admit_voice_and_round_video(
    conn: sqlite3.Connection,
) -> None:
    _make_hydration_eligible(conn)
    with write_transaction(conn):
        conn.executemany(
            "INSERT INTO messages(dialog_id, message_id, sent_at, text, media_kind, media_payload) "
            "VALUES (42, ?, ?, NULL, ?, ?)",
            [
                (96, 96, "video", '{"round_message":true}'),
                (97, 97, "voice", "{}"),
                (98, 98, "video", '{"round_message":false}'),
            ],
        )

    with write_transaction(conn):
        repair = repair_transcription_hydration_jobs(conn, due_at=900, max_jobs=300)
    assert conn.execute("SELECT message_id FROM hydration_jobs ORDER BY message_id").fetchall() == [(96,), (97,)]
    assert conn.execute("SELECT DISTINCT priority FROM hydration_jobs").fetchall() == [
        (int(HydrationPriority.BACKFILL),)
    ]
    with write_transaction(conn):
        conn.execute("DELETE FROM hydration_jobs")

    with write_transaction(conn):
        reconcile_fact_hydration_jobs_for_dialog(conn, 42, due_at=901)
    assert conn.execute("SELECT message_id FROM hydration_jobs ORDER BY message_id").fetchall() == [(96,), (97,)]


@pytest.mark.parametrize(
    ("media_kind", "media_payload"),
    [
        ("voice", "{}"),
        ("voice", "{ }"),
        ("video", '{"round_message":true}'),
        ("video", '{"round_message": true}'),
        ("video", '{"duration":12, "round_message": true}'),
        ("video", '{"round_message": true, "duration":12}'),
        ("video", '{"round_message":false,"round_message":true}'),
        ("video", '{"round_message":true,"round_message":false}'),
        ("video", '{"round_message":true,"round_message":NaN}'),
        ("video", '{"meta":{"ok":true,"ok":NaN},"round_message":true}'),
        ("video", '{"round_message":false}'),
        ("video", "{}"),
        ("video", '{"round_message":"true"}'),
        ("video", '{"round_message":1}'),
        ("audio", "{}"),
        ("other", "{}"),
        ("video", "not-json"),
        ("voice", '{"duration":NaN}'),
        ("voice", '{"voice":true,"voice":NaN}'),
        ("video", "[]"),
        ("video", "true"),
        ("voice", None),
        (None, None),
    ],
)
def test_sql_transcribable_media_predicate_matches_pair_adapter(
    conn: sqlite3.Connection, media_kind: str | None, media_payload: str | None
) -> None:
    with write_transaction(conn):
        conn.execute("CREATE TEMP TABLE media_candidates(media_kind TEXT, media_payload TEXT)")
        conn.execute("INSERT INTO media_candidates VALUES (?, ?)", (media_kind, media_payload))
    sql_row = cast(
        tuple[object] | None,
        conn.execute(f"SELECT {_TRANSCRIBABLE_MEDIA_SQL} FROM media_candidates m").fetchone(),
    )
    assert sql_row is not None
    sql_result = sql_row[0]

    assert bool(sql_result) is _is_transcribable_media_pair(media_kind, media_payload)


def test_transcription_repair_accepts_noncanonical_json_and_preflight_stays_eligible(
    conn: sqlite3.Connection,
) -> None:
    _make_hydration_eligible(conn)
    with write_transaction(conn):
        conn.execute(
            "INSERT INTO messages(dialog_id, message_id, sent_at, text, media_kind, media_payload) "
            "VALUES (42, 101, 101, NULL, 'video', '{\"duration\":12, \"round_message\": true}')"
        )

    with write_transaction(conn):
        first = repair_transcription_hydration_jobs(conn, due_at=900, max_jobs=1)
    assert first.has_more is False
    assert transcription_hydration_eligible(conn, 42, 101)
    with write_transaction(conn):
        second = repair_transcription_hydration_jobs(conn, due_at=901, max_jobs=1)
    assert second.has_more is False
    assert conn.execute("SELECT COUNT(*) FROM hydration_jobs").fetchone() == (1,)


@pytest.mark.parametrize(
    "media_kind, media_payload, expected",
    [("voice", "{}", True), ("video", '{"round_message":true}', True), ("video", "{}", False)],
)
def test_authoritative_transcription_applies_only_to_transcribable_media(
    conn: sqlite3.Connection, media_kind: str, media_payload: str, expected: bool
) -> None:
    with write_transaction(conn):
        insert_messages_with_fts(
            conn,
            [_message(98, text=None, media_kind=media_kind, media_payload=media_payload)],
        )

    with write_transaction(conn):
        applied = apply_message_transcription(
            conn, 42, 98, transcribed_text="round words", transcription_id=7, received_at=100
        )
    assert applied is expected
    assert conn.execute("SELECT COUNT(*) FROM message_transcriptions").fetchone() == ((1,) if expected else (0,))


def test_staged_transcription_is_removed_when_plain_video_materializes(conn: sqlite3.Connection) -> None:
    _make_hydration_eligible(conn)
    with write_transaction(conn):
        assert stage_message_transcription(
            conn, 42, 99, transcribed_text="staged speech", transcription_id=8, received_at=100
        )
    with write_transaction(conn):
        insert_messages_with_fts(
            conn,
            [_message(99, text="caption", media_kind="video", media_payload='{"duration":12}')],
        )

    assert conn.execute("SELECT text FROM messages WHERE message_id=99").fetchone() == ("caption",)
    assert conn.execute("SELECT stemmed_text FROM messages_fts WHERE message_id=99").fetchone() == (
        stem_text("caption"),
    )
    assert conn.execute("SELECT COUNT(*) FROM message_transcriptions").fetchone() == (0,)
    assert conn.execute("SELECT COUNT(*) FROM hydration_jobs").fetchone() == (0,)


def test_staged_transcription_overlays_round_video_materialization(conn: sqlite3.Connection) -> None:
    _make_hydration_eligible(conn)
    with write_transaction(conn):
        assert stage_message_transcription(
            conn, 42, 100, transcribed_text="round speech", transcription_id=9, received_at=100
        )
    with write_transaction(conn):
        insert_messages_with_fts(
            conn,
            [_message(100, text="caption", media_kind="video", media_payload='{"round_message":true}')],
        )

    assert conn.execute("SELECT text FROM messages WHERE message_id=100").fetchone() == ("round speech",)
    assert conn.execute("SELECT stemmed_text FROM messages_fts WHERE message_id=100").fetchone() == (
        stem_text("round speech"),
    )
    assert conn.execute("SELECT text, transcription_id FROM message_transcriptions").fetchone() == (
        "round speech",
        9,
    )
    assert conn.execute("SELECT COUNT(*) FROM hydration_jobs").fetchone() == (0,)


def test_transcription_repair_is_bounded_idempotent_and_newest_first(conn: sqlite3.Connection) -> None:
    _make_hydration_eligible(conn)
    with write_transaction(conn):
        conn.executemany(
            "INSERT INTO messages(dialog_id, message_id, sent_at, text, media_kind, media_payload) "
            "VALUES (42, ?, ?, NULL, 'voice', '{}')",
            ((message_id, message_id) for message_id in range(1, 506)),
        )

    with write_transaction(conn):
        first = repair_transcription_hydration_jobs(conn, due_at=900, max_jobs=300)
    assert first.has_more is True
    assert conn.execute("SELECT COUNT(*) FROM hydration_jobs WHERE kind = 'transcription'").fetchone() == (300,)
    assert conn.execute("SELECT MIN(message_id), MAX(message_id) FROM hydration_jobs").fetchone() == (206, 505)
    assert conn.execute(
        "SELECT due_at, attempts, priority, message_sent_at, terminal FROM hydration_jobs WHERE message_id = 505"
    ).fetchone() == (900, 0, 0, 505, 0)

    with write_transaction(conn):
        second = repair_transcription_hydration_jobs(conn, due_at=901, max_jobs=300, cursor=first.next_cursor)
    assert second.has_more is False
    with write_transaction(conn):
        third = repair_transcription_hydration_jobs(conn, due_at=902, max_jobs=300, cursor=second.next_cursor)
    assert third.has_more is False
    assert conn.execute("SELECT COUNT(*) FROM hydration_jobs WHERE kind = 'transcription'").fetchone() == (505,)


def test_transcription_repair_has_more_uses_raw_page_lookahead(conn: sqlite3.Connection) -> None:
    _make_hydration_eligible(conn)
    with write_transaction(conn):
        conn.executemany(
            "INSERT INTO messages(dialog_id, message_id, sent_at, text, media_kind, media_payload) "
            "VALUES (42, ?, ?, NULL, 'voice', '{}')",
            ((message_id, message_id) for message_id in range(1, 3)),
        )

    def traced_repair(
        max_jobs: int, cursor: HydrationRepairCursor | None = None
    ) -> tuple[TranscriptionHydrationRepair, list[str]]:
        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        try:
            with write_transaction(conn):
                result = repair_transcription_hydration_jobs(conn, due_at=900, max_jobs=max_jobs, cursor=cursor)
        finally:
            conn.set_trace_callback(None)
        executable = [
            statement
            for statement in statements
            if statement.lstrip().split(maxsplit=1)[0].upper() not in {"BEGIN", "COMMIT", "ROLLBACK", "PRAGMA"}
        ]
        return result, executable

    first, first_statements = traced_repair(max_jobs=1)
    assert first.has_more is True
    assert len(first_statements) == 2

    second, second_statements = traced_repair(max_jobs=1, cursor=first.next_cursor)
    assert second.has_more is False
    assert len(second_statements) == 4

    third, third_statements = traced_repair(max_jobs=10, cursor=second.next_cursor)
    assert third.has_more is False
    assert len(third_statements) == 3
    assert all("LIMIT" in sql for sql in third_statements)
    assert all("SELECT 1" not in sql for sql in third_statements)


def test_transcription_repair_excludes_ineligible_and_queued_messages(conn: sqlite3.Connection) -> None:
    _make_hydration_eligible(conn)
    with write_transaction(conn):
        conn.execute("INSERT INTO synced_dialogs(dialog_id, status) VALUES (43, 'access_lost')")
        conn.execute(
            "INSERT INTO full_history_enrollment(dialog_id, enabled, source, updated_at) VALUES (43, 1, 'explicit', 1)"
        )
        conn.executemany(
            "INSERT INTO messages(dialog_id, message_id, sent_at, text, media_kind, media_payload, is_deleted, out) "
            "VALUES (?, ?, ?, NULL, ?, ?, ?, ?)",
            [
                (42, 1, 10, "voice", "{}", 0, 0),  # eligible inbound, transcript below
                (42, 2, 20, "voice", "{}", 0, 1),  # eligible outbound, terminal job below
                (42, 3, 30, "voice", "{}", 1, 0),  # deleted
                (42, 4, 40, "other", "{}", 0, 0),  # not transcribable
                (43, 5, 50, "voice", "{}", 0, 0),  # inactive dialog
                (42, 6, 60, "voice", "{}", 0, 0),  # eligible inbound
                (42, 7, 70, "voice", "{}", 0, 1),  # eligible outbound
                (42, 8, 80, "video", '{"round_message":true}', 0, 0),  # round, transcript below
                (42, 9, 90, "video", '{"round_message":true}', 0, 0),  # round, terminal job below
                (42, 10, 100, "video", '{"round_message":true}', 1, 0),  # deleted round
                (43, 11, 110, "video", '{"round_message":true}', 0, 0),  # inactive round
                (42, 12, 120, "video", '{"round_message":true}', 0, 0),  # eligible round
            ],
        )
        conn.execute(
            "INSERT INTO message_transcriptions(dialog_id, message_id, text, transcription_id, received_at) "
            "VALUES (42, 1, 'already', 1, 1)"
        )
        conn.execute(
            "INSERT INTO message_transcriptions(dialog_id, message_id, text, transcription_id, received_at) "
            "VALUES (42, 8, 'already round', 8, 1)"
        )
        conn.execute(
            "INSERT INTO hydration_jobs(kind, dialog_id, message_id, due_at, attempts, terminal) "
            "VALUES ('transcription', 42, 2, 1, 4, 1)"
        )
        conn.execute(
            "INSERT INTO hydration_jobs(kind, dialog_id, message_id, due_at, attempts, terminal) "
            "VALUES ('transcription', 42, 9, 1, 4, 1)"
        )

    with write_transaction(conn):
        repair = repair_transcription_hydration_jobs(conn, due_at=100, max_jobs=300)

    assert repair.has_more is False
    assert conn.execute(
        "SELECT dialog_id, message_id, terminal FROM hydration_jobs ORDER BY message_id"
    ).fetchall() == [
        (42, 2, 1),
        (42, 6, 0),
        (42, 7, 0),
        (42, 9, 1),
        (42, 12, 0),
    ]


def test_transcription_repair_plan_uses_partial_voice_index(conn: sqlite3.Connection) -> None:
    plan = cast(
        list[tuple[object, ...]],
        conn.execute(
            "EXPLAIN QUERY PLAN SELECT m.message_id "
            f"{_REPAIR_TRANSCRIPTION_CANDIDATES_FROM_SQL} "
            "ORDER BY m.sent_at DESC, m.dialog_id, m.message_id LIMIT 300"
        ).fetchall(),
    )
    details = " ".join(str(row[3]) for row in plan)
    assert "USING INDEX idx_messages_transcribable_undeleted_sent" in details
    assert "USE TEMP B-TREE" not in details
    assert "SCAN messages" not in details


def test_transcription_repair_rolls_back_without_leaving_queue_rows(conn: sqlite3.Connection) -> None:
    _make_hydration_eligible(conn)
    with write_transaction(conn):
        conn.execute(
            "INSERT INTO messages(dialog_id, message_id, sent_at, text, media_kind, media_payload) "
            "VALUES (42, 8, 8, NULL, 'voice', '{}')"
        )

    with pytest.raises(RuntimeError, match="abort"):
        with write_transaction(conn):
            repair_transcription_hydration_jobs(conn, due_at=900, max_jobs=1)
            raise RuntimeError("abort")
    assert conn.execute("SELECT COUNT(*) FROM hydration_jobs").fetchone() == (0,)

    with write_transaction(conn):
        repair = repair_transcription_hydration_jobs(conn, due_at=901, max_jobs=1)
    assert repair.has_more is False


@pytest.mark.parametrize(
    ("media_kind", "media_payload"),
    [(None, None), ("photo", "{}"), ("contact", '{"phone_number":"1"}')],
)
def test_message_persistence_removes_job_for_resolved_or_missing_media(
    conn: sqlite3.Connection, media_kind: str | None, media_payload: str | None
) -> None:
    _make_hydration_eligible(conn)
    with write_transaction(conn):
        conn.execute(
            "INSERT INTO hydration_jobs(kind, dialog_id, message_id, due_at, attempts) "
            "VALUES ('media_metadata', 42, 91, 100, 2)"
        )
    with write_transaction(conn):
        insert_messages_with_fts(conn, [_message(91, text=None, media_kind=media_kind, media_payload=media_payload)])
    assert conn.execute("SELECT COUNT(*) FROM hydration_jobs").fetchone() == (0,)


@pytest.mark.parametrize("status", ["not_synced", "own_only", "fragment", "access_lost"])
def test_message_persistence_does_not_enqueue_inactive_dialogs(conn: sqlite3.Connection, status: str) -> None:
    _make_hydration_eligible(conn, status=status)
    with write_transaction(conn):
        insert_messages_with_fts(conn, [_message(92, text=None, media_kind="other", media_payload="{}")])
    assert conn.execute("SELECT COUNT(*) FROM hydration_jobs").fetchone() == (0,)


def test_persist_transcribed_text_refreshes_fts_without_version_history(conn: sqlite3.Connection) -> None:
    with write_transaction(conn):
        insert_messages_with_fts(conn, [_message(12, text=None)])
    with write_transaction(conn):
        assert (
            persist_transcribed_text(
                conn,
                42,
                12,
                old_text=None,
                transcribed_text="voice words",
            )
            is True
        )
    assert conn.execute("SELECT text FROM messages WHERE dialog_id=42 AND message_id=12").fetchone() == ("voice words",)
    assert conn.execute("SELECT COUNT(*) FROM message_versions WHERE dialog_id=42 AND message_id=12").fetchone() == (0,)
    assert conn.execute("SELECT stemmed_text FROM messages_fts WHERE dialog_id=42 AND message_id=12").fetchone() == (
        stem_text("voice words"),
    )
    with write_transaction(conn):
        assert (
            persist_transcribed_text(
                conn,
                42,
                12,
                old_text="voice words",
                transcribed_text="voice words",
            )
            is False
        )
    assert conn.execute("SELECT COUNT(*) FROM message_versions WHERE dialog_id=42 AND message_id=12").fetchone() == (0,)


def test_message_transcription_is_applied_by_canonical_bundle_writer(conn: sqlite3.Connection) -> None:
    with write_transaction(conn):
        upsert_message_transcription(conn, 42, 14, transcribed_text="voice words", transcription_id=14, received_at=400)
        insert_messages_with_fts(conn, [_message(14, text="caption", media_kind="voice", media_payload="{}")])
        insert_messages_with_fts(conn, [_message(14, text=None, media_kind="voice", media_payload="{}")])

    assert conn.execute("SELECT text FROM messages WHERE dialog_id=42 AND message_id=14").fetchone() == ("voice words",)
    assert conn.execute("SELECT stemmed_text FROM messages_fts WHERE dialog_id=42 AND message_id=14").fetchone() == (
        stem_text("voice words"),
    )
    assert conn.execute("SELECT COUNT(*) FROM message_transcriptions").fetchone() == (1,)


def test_existing_transcription_survives_voice_reimport(conn: sqlite3.Connection) -> None:
    with write_transaction(conn):
        upsert_message_transcription(conn, 42, 15, transcribed_text="voice words", transcription_id=15, received_at=400)
        insert_messages_with_fts(conn, [_message(15, text="caption", media_kind="voice", media_payload="{}")])
        insert_messages_with_fts(conn, [_message(15, text=None, media_kind="voice", media_payload="{}")])
    assert conn.execute("SELECT text FROM messages WHERE dialog_id=42 AND message_id=15").fetchone() == ("voice words",)


def test_unrelated_media_caption_can_be_removed_on_reimport(conn: sqlite3.Connection) -> None:
    with write_transaction(conn):
        insert_messages_with_fts(conn, [_message(16, text="caption", media_kind="photo", media_payload="{}")])
        insert_messages_with_fts(conn, [_message(16, text=None, media_kind="photo", media_payload="{}")])
    assert conn.execute("SELECT text FROM messages WHERE dialog_id=42 AND message_id=16").fetchone() == (None,)


def test_mark_message_deleted_is_idempotent_and_retains_text(conn: sqlite3.Connection) -> None:
    with write_transaction(conn):
        insert_messages_with_fts(conn, [_message(13, text="retain me")])
    with write_transaction(conn):
        assert mark_message_deleted(conn, 42, 13, 500) is True
    with write_transaction(conn):
        assert mark_message_deleted(conn, 42, 13, 600) is False
    assert conn.execute(
        "SELECT text, is_deleted, deleted_at FROM messages WHERE dialog_id=42 AND message_id=13"
    ).fetchone() == (
        "retain me",
        1,
        500,
    )
    assert conn.execute("SELECT COUNT(*) FROM messages_fts WHERE dialog_id=42 AND message_id=13").fetchone() == (0,)


def test_find_unique_incoming_human_dm_dialogs_requires_one_policy_match(conn: sqlite3.Connection) -> None:
    with write_transaction(conn):
        conn.execute("INSERT INTO dialogs(dialog_id, type) VALUES (42, 'user'), (43, 'user')")
        conn.execute("INSERT INTO entities(id, type, updated_at) VALUES (42, 'user', 1), (43, 'user', 1)")
        insert_messages_with_fts(conn, [_message(13, text="one")])
    assert find_unique_incoming_human_dm_dialogs(conn, [13]) == {13: 42}
    with write_transaction(conn):
        duplicate = replace(
            _message(13, text="two"),
            message=replace(_message(13, text="two").message, dialog_id=43, sender_id=43),
        )
        insert_messages_with_fts(conn, [duplicate])
    assert find_unique_incoming_human_dm_dialogs(conn, [13]) == {}


def test_list_undeleted_message_ids_uses_strict_cutoff(conn: sqlite3.Connection) -> None:
    with write_transaction(conn):
        insert_messages_with_fts(
            conn,
            [
                _message(20, text="before", sent_at=99),
                _message(21, text="at cutoff", sent_at=100),
                _message(22, text="after", sent_at=101),
                _message(23, text="deleted", sent_at=98),
            ],
        )
        assert mark_message_deleted(conn, 42, 23, 600) is True
    assert list_undeleted_message_ids(conn, 42, 100) == (20,)


def test_repository_writes_rollback_with_caller_transaction(conn: sqlite3.Connection) -> None:
    with write_transaction(conn):
        _seed_human_dm(conn)
        insert_messages_with_fts(conn, [_message(30, text="before")])
    with pytest.raises(RuntimeError, match="abort"):
        with write_transaction(conn):
            assert persist_edited_message(conn, _message(30, text="after"), old_text="before", edit_date=700) == 1
            raise RuntimeError("abort")
    assert conn.execute("SELECT text FROM messages WHERE dialog_id=42 AND message_id=30").fetchone() == ("before",)
    assert conn.execute("SELECT COUNT(*) FROM message_versions WHERE dialog_id=42 AND message_id=30").fetchone() == (0,)


@pytest.mark.parametrize(
    "media_kind,candidates_sql,index_name",
    [
        ("voice", _REPAIR_TRANSCRIPTION_CANDIDATES_FROM_SQL, "idx_messages_transcribable_undeleted_sent"),
        ("contact", _REPAIR_MEDIA_METADATA_CONTACT_OTHER_SQL, "idx_messages_media_unresolved_contact_other"),
        ("video", _REPAIR_MEDIA_METADATA_VIDEO_SQL, "idx_messages_media_unresolved_video"),
    ],
)
def test_hydration_repair_bounds_terminal_prefix_and_later_equal_time_seek(
    conn: sqlite3.Connection, media_kind: str, candidates_sql: str, index_name: str
) -> None:
    _make_hydration_eligible(conn)
    total = 6_000
    payload = '{"duration":1}' if media_kind == "video" else "{}"
    kind = "transcription" if media_kind == "voice" else "media_metadata"
    with write_transaction(conn):
        conn.executemany(
            "INSERT INTO messages(dialog_id,message_id,sent_at,media_kind,media_payload) VALUES (42,?,10,?,?)",
            ((identifier, media_kind, payload) for identifier in range(1, total + 1)),
        )
        conn.executemany(
            "INSERT INTO hydration_jobs(kind,dialog_id,message_id,due_at,attempts,priority,terminal) "
            "VALUES (?,42,?,1,4,1,1)",
            ((kind, identifier) for identifier in range(1, total - 9)),
        )

    def repair(cursor: HydrationRepairCursor | None, budget: int) -> tuple[bool, HydrationRepairCursor | None]:
        if media_kind == "voice":
            transcription = repair_transcription_hydration_jobs(conn, due_at=900, max_jobs=budget, cursor=cursor)
            return transcription.has_more, transcription.next_cursor
        media = repair_media_metadata_hydration_jobs(
            conn,
            due_at=900,
            max_jobs=budget,
            contact_other_cursor=cursor if media_kind == "contact" else None,
            video_cursor=cursor if media_kind == "video" else None,
        )
        return media.has_more, media.next_contact_other_cursor if media_kind == "contact" else media.next_video_cursor

    for incoming in (None, HydrationRepairCursor(10, 42, total - 20)):
        steps = 0
        statements: list[str] = []

        def progress() -> int:
            nonlocal steps
            steps += 1
            return int(steps > 15_000)

        conn.set_progress_handler(progress, 1)
        conn.set_trace_callback(statements.append)
        try:
            with write_transaction(conn):
                has_more, next_cursor = repair(incoming, 64)
        finally:
            conn.set_progress_handler(None, 0)
            conn.set_trace_callback(None)
        assert steps < 15_000
        assert next_cursor == HydrationRepairCursor(10, 42, 64 if incoming is None else total)
        assert has_more is (incoming is None)
        if incoming is not None:
            selections = [statement for statement in statements if statement.startswith("SELECT m.sent_at")]
            for selection in selections:
                if index_name not in selection:
                    continue
                plan = cast(list[tuple[object, ...]], conn.execute("EXPLAIN QUERY PLAN " + selection).fetchall())
                details = " ".join(str(row[3]) for row in plan)
                assert "SEARCH m USING INDEX " + index_name in details
                assert "USE TEMP B-TREE" not in details

    # Even a page containing only terminal jobs must advance until older missing work is reached.
    cursor = None
    for _ in range(total // 256 + 2):
        with write_transaction(conn):
            has_more, cursor = repair(cursor, 256)
        if not has_more:
            break
    assert has_more is False
    assert cursor == HydrationRepairCursor(10, 42, total)
    assert conn.execute("SELECT COUNT(*) FROM hydration_jobs WHERE kind=? AND terminal=0", (kind,)).fetchone() == (10,)
    assert conn.execute(
        "SELECT due_at,attempts,priority,terminal FROM hydration_jobs WHERE kind=? AND message_id=1", (kind,)
    ).fetchone() == (1, 4, 1, 1)


def test_hydration_raw_seek_crosses_equal_peer_and_older_time_without_skips(conn: sqlite3.Connection) -> None:
    with write_transaction(conn):
        conn.executemany(
            "INSERT INTO messages(dialog_id,message_id,sent_at,media_kind,media_payload) VALUES (?,?,?,'voice','{}')",
            [(42, 1, 10), (42, 2, 10), (43, 1, 10), (43, 2, 10), (42, 3, 9), (42, 4, 9)],
        )
    page = _repair_raw_page(conn, _REPAIR_TRANSCRIPTION_CANDIDATES_FROM_SQL, HydrationRepairCursor(10, 42, 1), 4)
    assert page == [(10, 42, 2), (10, 43, 1), (10, 43, 2), (9, 42, 3)]
    assert _repair_raw_page(conn, _REPAIR_TRANSCRIPTION_CANDIDATES_FROM_SQL, HydrationRepairCursor(*page[-1]), 4) == [
        (9, 42, 4)
    ]


def test_delayed_bundles_cannot_overwrite_edits_or_resurrect_deletions(conn: sqlite3.Connection) -> None:
    original = replace(_message(90, text="original"), observation_order=1)
    edited = replace(_message(90, text="edited"), observation_order=3)
    edited = replace(edited, message=replace(edited.message, edit_date=200))
    with write_transaction(conn):
        insert_messages_with_fts(conn, [original])
        insert_messages_with_fts(conn, [edited])
        insert_messages_with_fts(conn, [replace(original, observation_order=4)])
    assert read_message_text(conn, 42, 90).text == "edited"
    with write_transaction(conn):
        mark_message_deleted(conn, 42, 90, 300)
        mark_message_deleted(conn, 42, 91, 300)
        insert_messages_with_fts(conn, [replace(edited, observation_order=100), _message(91, text="late")])
    assert conn.execute("SELECT is_deleted,text FROM messages WHERE dialog_id=42 AND message_id=90").fetchone() == (
        1,
        "edited",
    )
    assert not read_message_text(conn, 42, 91).found
    assert conn.execute(
        "SELECT COUNT(*) FROM messages_fts WHERE dialog_id=42 AND message_id IN (90,91)"
    ).fetchone() == (0,)


def test_same_caption_edit_persists_full_bundle_without_version(conn: sqlite3.Connection) -> None:
    with write_transaction(conn):
        original = _message(92, text="same", media_kind="video", media_payload='{"duration":12}')
        insert_messages_with_fts(conn, [original])
        edited = replace(
            original,
            message=replace(original.message, edit_date=200, reply_to_msg_id=10, media_payload='{"duration":24}'),
        )
        assert persist_edited_message(conn, edited, old_text="same", edit_date=200) is None
    assert conn.execute(
        "SELECT edit_date,reply_to_msg_id,media_payload FROM messages WHERE message_id=92"
    ).fetchone() == (200, 10, '{"duration":24}')
    assert conn.execute("SELECT COUNT(*) FROM message_versions WHERE message_id=92").fetchone() == (0,)


def test_fts_repair_handles_missing_keys_masked_by_duplicates_and_deleted(conn: sqlite3.Connection) -> None:
    from mcp_telegram.fts import DELETE_FTS_SQL, backfill_fts_index

    with write_transaction(conn):
        insert_messages_with_fts(
            conn, [_message(93, text="live"), _message(94, text="missing"), _message(95, text="deleted")]
        )
        conn.execute(DELETE_FTS_SQL, (42, 94))
        mark_message_deleted(conn, 42, 95, 300)
        conn.execute("INSERT INTO messages_fts(dialog_id,message_id,stemmed_text) VALUES(42,93,'duplicate')")
        conn.execute("INSERT INTO messages_fts(dialog_id,message_id,stemmed_text) VALUES(42,95,'deleted')")
    assert backfill_fts_index(conn) == 1
    assert conn.execute("SELECT dialog_id,message_id FROM messages_fts ORDER BY message_id").fetchall() == [
        (42, 93),
        (42, 94),
    ]
    assert backfill_fts_index(conn) == 0
    plan = cast(
        list[tuple[int, int, int, str]], conn.execute("EXPLAIN QUERY PLAN " + DELETE_FTS_SQL, (42, 93)).fetchall()
    )
    assert any("VIRTUAL TABLE INDEX" in row[3] and "=" in row[3] for row in plan)


def test_hydration_rejects_observation_started_before_newer_message(conn: sqlite3.Connection) -> None:
    from mcp_telegram.messages.sqlite_hydration import apply_hydrated_media_fact, apply_message_transcription_if_absent

    with write_transaction(conn):
        insert_messages_with_fts(conn, [replace(_message(96, text="newer"), observation_order=20)])
        assert not apply_hydrated_media_fact(conn, 42, 96, None, None, observation_order=10)
        assert (
            apply_message_transcription_if_absent(
                conn, 42, 96, transcribed_text="oldvoice", transcription_id=1, received_at=1, observation_order=10
            )
            == "not_applied"
        )
    assert read_message_text(conn, 42, 96).text == "newer"
    assert conn.execute("SELECT COUNT(*) FROM message_transcriptions WHERE message_id=96").fetchone() == (0,)


def test_migration_helper_rolls_back_ddl_on_failure(tmp_path: Path) -> None:
    from mcp_telegram.sync_db import _apply_migration

    path = tmp_path / "interrupted.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE schema_version(version INTEGER PRIMARY KEY,applied_at INTEGER)")
    db.execute("CREATE TABLE retained(value TEXT)")
    db.execute("INSERT INTO retained VALUES ('keep')")
    db.commit()
    with pytest.raises(sqlite3.OperationalError):
        _apply_migration(
            db, 79, 80, ["ALTER TABLE retained ADD COLUMN observation_order INTEGER", "INSERT INTO absent VALUES (1)"]
        )
    db.close()
    db = sqlite3.connect(path)
    rows = cast(list[tuple[int, str, str, int, str | None, int]], db.execute("PRAGMA table_info(retained)").fetchall())
    assert [r[1] for r in rows] == ["value"]
    _apply_migration(db, 79, 80, ["ALTER TABLE retained ADD COLUMN observation_order INTEGER"])
    assert db.execute("SELECT value FROM retained").fetchone() == ("keep",)
    db.close()


def test_same_second_delayed_observation_cannot_replace_newer_bundle(conn: sqlite3.Connection) -> None:
    from mcp_telegram.observation_order import allocate_observation_order

    older = allocate_observation_order(conn)
    newer = allocate_observation_order(conn)
    base = _message(97, text="newer")
    base = replace(base, message=replace(base.message, edit_date=500))
    with write_transaction(conn):
        insert_messages_with_fts(conn, [replace(base, observation_order=newer)])
        insert_messages_with_fts(
            conn, [replace(base, observation_order=older, message=replace(base.message, text="delayed"))]
        )
    assert read_message_text(conn, 42, 97).text == "newer"


def test_newer_telegram_version_resets_observation_order(conn: sqlite3.Connection) -> None:
    old_version = replace(_message(98, text="old"), observation_order=30)
    new_version = replace(
        old_version, observation_order=10, message=replace(old_version.message, text="new", edit_date=200)
    )
    same_new_version = replace(new_version, observation_order=20, message=replace(new_version.message, text="latest"))
    with write_transaction(conn):
        insert_messages_with_fts(conn, [old_version])
        insert_messages_with_fts(conn, [new_version])
        insert_messages_with_fts(conn, [same_new_version])
    assert read_message_text(conn, 42, 98).text == "latest"
