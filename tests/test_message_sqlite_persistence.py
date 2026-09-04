"""Focused tests for transaction-neutral event message persistence."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import cast

import pytest

from mcp_telegram.fts import stem_text
from mcp_telegram.hydration_queue import HydrationPriority
from mcp_telegram.message_contracts import ExtractedMessage, StoredMessage
from mcp_telegram.messages.sqlite_bundle import (
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
    TranscriptionHydrationRepair,
    _is_transcribable_media_pair,
    reconcile_fact_hydration_jobs_for_dialog,
    repair_media_metadata_hydration_jobs,
    repair_transcription_hydration_jobs,
    transcription_hydration_eligible,
)
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema


def _message(
    message_id: int,
    *,
    text: str | None,
    sent_at: int = 100,
    media_kind: str | None = None,
    media_payload: str | None = None,
) -> ExtractedMessage:
    return ExtractedMessage(
        message=StoredMessage(
            dialog_id=42,
            message_id=message_id,
            sent_at=sent_at,
            text=text,
            sender_id=7,
            sender_first_name="Test",
            reply_to_msg_id=None,
            forum_topic_id=None,
            edit_date=None,
            grouped_id=None,
            reply_to_peer_id=None,
            out=0,
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
    try:
        yield connection
    finally:
        connection.close()


def test_read_message_text_distinguishes_missing_from_null(conn: sqlite3.Connection) -> None:
    missing = read_message_text(conn, 42, 1)
    assert missing.found is False
    assert missing.text is None

    with conn:
        insert_messages_with_fts(conn, [_message(1, text=None)])
    null_text = read_message_text(conn, 42, 1)
    assert null_text.found is True
    assert null_text.text is None


def test_persist_edited_message_versions_sequentially_and_refreshes_fts(conn: sqlite3.Connection) -> None:
    with conn:
        insert_messages_with_fts(conn, [_message(10, text="first")])
    with conn:
        assert persist_edited_message(conn, _message(10, text="second"), old_text="first", edit_date=200) == 1
    with conn:
        assert persist_edited_message(conn, _message(10, text="third"), old_text="second", edit_date=300) == 2

    assert conn.execute(
        "SELECT version, old_text FROM message_versions WHERE dialog_id=42 AND message_id=10 ORDER BY version"
    ).fetchall() == [(1, "first"), (2, "second")]
    assert conn.execute("SELECT text FROM messages WHERE dialog_id=42 AND message_id=10").fetchone() == ("third",)
    assert conn.execute("SELECT stemmed_text FROM messages_fts WHERE dialog_id=42 AND message_id=10").fetchone() == (
        stem_text("third"),
    )


def test_persist_edited_message_unchanged_is_noop(conn: sqlite3.Connection) -> None:
    with conn:
        insert_messages_with_fts(conn, [_message(11, text="same")])
    with conn:
        assert persist_edited_message(conn, _message(11, text="same"), old_text="same", edit_date=200) is None
    assert conn.execute("SELECT COUNT(*) FROM message_versions").fetchone() == (0,)


def _make_hydration_eligible(conn: sqlite3.Connection, status: str = "synced") -> None:
    conn.execute("INSERT INTO synced_dialogs(dialog_id, status) VALUES (42, ?)", (status,))
    conn.execute(
        "INSERT INTO full_history_enrollment(dialog_id, enabled, source, updated_at) VALUES (42, 1, 'explicit', 1)"
    )


@pytest.mark.parametrize("media_kind", ["contact", "other"])
def test_message_persistence_enqueues_one_unresolved_job_and_preserves_attempts(
    conn: sqlite3.Connection, media_kind: str
) -> None:
    _make_hydration_eligible(conn)
    conn.execute(
        "INSERT INTO hydration_jobs(kind, dialog_id, message_id, due_at, attempts) "
        "VALUES ('media_metadata', 42, 90, 100, 2)"
    )
    with conn:
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
    with conn:
        insert_messages_with_fts(
            conn,
            [_message(93, text=None, media_kind="voice", media_payload="{}")],
            priority=HydrationPriority.FOREGROUND,
        )

    assert conn.execute("SELECT kind, priority FROM hydration_jobs").fetchall() == [("transcription", 1)]


def test_foreground_round_video_persistence_enqueues_transcription(conn: sqlite3.Connection) -> None:
    _make_hydration_eligible(conn)
    with conn:
        insert_messages_with_fts(
            conn,
            [_message(94, text=None, media_kind="video", media_payload='{"round_message":true}')],
            priority=HydrationPriority.FOREGROUND,
        )

    assert conn.execute("SELECT kind, priority FROM hydration_jobs").fetchall() == [("transcription", 1)]
    assert transcription_hydration_eligible(conn, 42, 94)


def test_plain_video_is_not_admitted_to_transcription(conn: sqlite3.Connection) -> None:
    _make_hydration_eligible(conn)
    with conn:
        insert_messages_with_fts(
            conn,
            [_message(95, text=None, media_kind="video", media_payload='{"duration":12}')],
            priority=HydrationPriority.FOREGROUND,
        )

    assert conn.execute("SELECT COUNT(*) FROM hydration_jobs").fetchone() == (0,)
    assert not transcription_hydration_eligible(conn, 42, 95)


def test_media_metadata_repair_is_bounded_newest_first_and_terminal_safe(conn: sqlite3.Connection) -> None:
    _make_hydration_eligible(conn)
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
    conn.commit()

    first = repair_media_metadata_hydration_jobs(conn, due_at=900, max_jobs=2)
    assert first == type(first)(enqueued=2, has_more=True)
    assert conn.execute(
        "SELECT message_id, priority, message_sent_at, terminal FROM hydration_jobs "
        "WHERE kind = 'media_metadata' ORDER BY message_id"
    ).fetchall() == [(102, 0, 200, 0), (105, 0, 500, 0), (107, 0, 0, 1)]
    assert conn.execute(
        "SELECT message_id FROM hydration_jobs WHERE kind = 'media_metadata' AND terminal = 0 "
        "ORDER BY message_sent_at DESC"
    ).fetchall() == [(105,), (102,)]

    second = repair_media_metadata_hydration_jobs(conn, due_at=901, max_jobs=2)
    assert second == type(second)(enqueued=2, has_more=False)
    assert conn.execute(
        "SELECT message_id FROM hydration_jobs WHERE kind = 'media_metadata' AND terminal = 0 "
        "ORDER BY message_sent_at DESC"
    ).fetchall() == [(105,), (102,), (106,), (101,)]

    third = repair_media_metadata_hydration_jobs(conn, due_at=902, max_jobs=2)
    assert third == type(third)(enqueued=0, has_more=False)

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
    conn.executemany(
        "INSERT INTO messages(dialog_id, message_id, sent_at, text, media_kind, media_payload) "
        "VALUES (42, ?, ?, NULL, ?, ?)",
        [
            (96, 96, "video", '{"round_message":true}'),
            (97, 97, "voice", "{}"),
            (98, 98, "video", '{"round_message":false}'),
        ],
    )
    conn.commit()

    repair = repair_transcription_hydration_jobs(conn, due_at=900, max_jobs=300)
    assert repair.enqueued == 2
    assert conn.execute("SELECT message_id FROM hydration_jobs ORDER BY message_id").fetchall() == [(96,), (97,)]
    assert conn.execute("SELECT DISTINCT priority FROM hydration_jobs").fetchall() == [
        (int(HydrationPriority.BACKFILL),)
    ]
    conn.execute("DELETE FROM hydration_jobs")
    conn.commit()

    with conn:
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
    conn.execute(
        "INSERT INTO messages(dialog_id, message_id, sent_at, text, media_kind, media_payload) "
        "VALUES (42, 101, 101, NULL, 'video', '{\"duration\":12, \"round_message\": true}')"
    )
    conn.commit()

    first = repair_transcription_hydration_jobs(conn, due_at=900, max_jobs=1)
    assert (first.enqueued, first.has_more) == (1, False)
    assert transcription_hydration_eligible(conn, 42, 101)
    second = repair_transcription_hydration_jobs(conn, due_at=901, max_jobs=1)
    assert (second.enqueued, second.has_more) == (0, False)


@pytest.mark.parametrize(
    "media_kind, media_payload, expected",
    [("voice", "{}", True), ("video", '{"round_message":true}', True), ("video", "{}", False)],
)
def test_authoritative_transcription_applies_only_to_transcribable_media(
    conn: sqlite3.Connection, media_kind: str, media_payload: str, expected: bool
) -> None:
    with conn:
        insert_messages_with_fts(
            conn,
            [_message(98, text=None, media_kind=media_kind, media_payload=media_payload)],
        )

    with conn:
        applied = apply_message_transcription(
            conn, 42, 98, transcribed_text="round words", transcription_id=7, received_at=100
        )
    assert applied is expected
    assert conn.execute("SELECT COUNT(*) FROM message_transcriptions").fetchone() == ((1,) if expected else (0,))


def test_staged_transcription_is_removed_when_plain_video_materializes(conn: sqlite3.Connection) -> None:
    _make_hydration_eligible(conn)
    with conn:
        assert stage_message_transcription(
            conn, 42, 99, transcribed_text="staged speech", transcription_id=8, received_at=100
        )
    with conn:
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
    with conn:
        assert stage_message_transcription(
            conn, 42, 100, transcribed_text="round speech", transcription_id=9, received_at=100
        )
    with conn:
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
    conn.executemany(
        "INSERT INTO messages(dialog_id, message_id, sent_at, text, media_kind, media_payload) "
        "VALUES (42, ?, ?, NULL, 'voice', '{}')",
        ((message_id, message_id) for message_id in range(1, 506)),
    )
    conn.commit()

    first = repair_transcription_hydration_jobs(conn, due_at=900, max_jobs=300)
    assert (first.enqueued, first.has_more) == (300, True)
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM hydration_jobs WHERE kind = 'transcription'").fetchone() == (300,)
    assert conn.execute("SELECT MIN(message_id), MAX(message_id) FROM hydration_jobs").fetchone() == (206, 505)
    assert conn.execute(
        "SELECT due_at, attempts, priority, message_sent_at, terminal FROM hydration_jobs WHERE message_id = 505"
    ).fetchone() == (900, 0, 0, 505, 0)

    second = repair_transcription_hydration_jobs(conn, due_at=901, max_jobs=300)
    assert (second.enqueued, second.has_more) == (205, False)
    conn.commit()
    third = repair_transcription_hydration_jobs(conn, due_at=902, max_jobs=300)
    assert (third.enqueued, third.has_more) == (0, False)
    assert conn.execute("SELECT COUNT(*) FROM hydration_jobs WHERE kind = 'transcription'").fetchone() == (505,)


def test_transcription_repair_only_probes_has_more_at_batch_boundary(conn: sqlite3.Connection) -> None:
    _make_hydration_eligible(conn)
    conn.executemany(
        "INSERT INTO messages(dialog_id, message_id, sent_at, text, media_kind, media_payload) "
        "VALUES (42, ?, ?, NULL, 'voice', '{}')",
        ((message_id, message_id) for message_id in range(1, 3)),
    )
    conn.commit()

    def traced_repair(max_jobs: int) -> tuple[TranscriptionHydrationRepair, list[str]]:
        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        try:
            result = repair_transcription_hydration_jobs(conn, due_at=900, max_jobs=max_jobs)
        finally:
            conn.set_trace_callback(None)
        executable = [
            statement
            for statement in statements
            if statement.lstrip().split(maxsplit=1)[0].upper() not in {"BEGIN", "COMMIT", "ROLLBACK"}
        ]
        return result, executable

    first, first_statements = traced_repair(max_jobs=1)
    assert (first.enqueued, first.has_more) == (1, True)
    assert len(first_statements) == 2
    conn.commit()

    second, second_statements = traced_repair(max_jobs=1)
    assert (second.enqueued, second.has_more) == (1, False)
    assert len(second_statements) == 2
    conn.commit()

    third, third_statements = traced_repair(max_jobs=10)
    assert (third.enqueued, third.has_more) == (0, False)
    assert len(third_statements) == 1


def test_transcription_repair_excludes_ineligible_and_queued_messages(conn: sqlite3.Connection) -> None:
    _make_hydration_eligible(conn)
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
    conn.commit()

    repair = repair_transcription_hydration_jobs(conn, due_at=100, max_jobs=300)

    assert (repair.enqueued, repair.has_more) == (3, False)
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
    conn.execute(
        "INSERT INTO messages(dialog_id, message_id, sent_at, text, media_kind, media_payload) "
        "VALUES (42, 8, 8, NULL, 'voice', '{}')"
    )
    conn.commit()

    repair = repair_transcription_hydration_jobs(conn, due_at=900, max_jobs=1)
    assert repair.enqueued == 1
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM hydration_jobs").fetchone() == (0,)

    repair = repair_transcription_hydration_jobs(conn, due_at=901, max_jobs=1)
    assert (repair.enqueued, repair.has_more) == (1, False)


@pytest.mark.parametrize(
    ("media_kind", "media_payload"),
    [(None, None), ("photo", "{}"), ("contact", '{"phone_number":"1"}')],
)
def test_message_persistence_removes_job_for_resolved_or_missing_media(
    conn: sqlite3.Connection, media_kind: str | None, media_payload: str | None
) -> None:
    _make_hydration_eligible(conn)
    conn.execute(
        "INSERT INTO hydration_jobs(kind, dialog_id, message_id, due_at, attempts) "
        "VALUES ('media_metadata', 42, 91, 100, 2)"
    )
    with conn:
        insert_messages_with_fts(conn, [_message(91, text=None, media_kind=media_kind, media_payload=media_payload)])
    assert conn.execute("SELECT COUNT(*) FROM hydration_jobs").fetchone() == (0,)


@pytest.mark.parametrize("status", ["not_synced", "own_only", "fragment", "access_lost"])
def test_message_persistence_does_not_enqueue_inactive_dialogs(conn: sqlite3.Connection, status: str) -> None:
    _make_hydration_eligible(conn, status=status)
    with conn:
        insert_messages_with_fts(conn, [_message(92, text=None, media_kind="other", media_payload="{}")])
    assert conn.execute("SELECT COUNT(*) FROM hydration_jobs").fetchone() == (0,)


def test_persist_transcribed_text_versions_and_refreshes_fts(conn: sqlite3.Connection) -> None:
    with conn:
        insert_messages_with_fts(conn, [_message(12, text=None)])
    with conn:
        assert (
            persist_transcribed_text(
                conn,
                42,
                12,
                old_text=None,
                transcribed_text="voice words",
                transcribed_at=400,
            )
            == 1
        )
    assert conn.execute("SELECT text FROM messages WHERE dialog_id=42 AND message_id=12").fetchone() == ("voice words",)
    assert conn.execute(
        "SELECT old_text, version FROM message_versions WHERE dialog_id=42 AND message_id=12"
    ).fetchone() == (
        None,
        1,
    )
    assert conn.execute("SELECT stemmed_text FROM messages_fts WHERE dialog_id=42 AND message_id=12").fetchone() == (
        stem_text("voice words"),
    )
    with conn:
        assert (
            persist_transcribed_text(
                conn,
                42,
                12,
                old_text="voice words",
                transcribed_text="voice words",
                transcribed_at=401,
            )
            is None
        )
    assert conn.execute("SELECT COUNT(*) FROM message_versions WHERE dialog_id=42 AND message_id=12").fetchone() == (1,)


def test_message_transcription_is_applied_by_canonical_bundle_writer(conn: sqlite3.Connection) -> None:
    with conn:
        upsert_message_transcription(conn, 42, 14, transcribed_text="voice words", transcription_id=14, received_at=400)
        insert_messages_with_fts(conn, [_message(14, text="caption", media_kind="voice", media_payload="{}")])
        insert_messages_with_fts(conn, [_message(14, text=None, media_kind="voice", media_payload="{}")])

    assert conn.execute("SELECT text FROM messages WHERE dialog_id=42 AND message_id=14").fetchone() == ("voice words",)
    assert conn.execute("SELECT stemmed_text FROM messages_fts WHERE dialog_id=42 AND message_id=14").fetchone() == (
        stem_text("voice words"),
    )
    assert conn.execute("SELECT COUNT(*) FROM message_transcriptions").fetchone() == (1,)


def test_existing_transcription_survives_voice_reimport(conn: sqlite3.Connection) -> None:
    with conn:
        upsert_message_transcription(conn, 42, 15, transcribed_text="voice words", transcription_id=15, received_at=400)
        insert_messages_with_fts(conn, [_message(15, text="caption", media_kind="voice", media_payload="{}")])
        insert_messages_with_fts(conn, [_message(15, text=None, media_kind="voice", media_payload="{}")])
    assert conn.execute("SELECT text FROM messages WHERE dialog_id=42 AND message_id=15").fetchone() == ("voice words",)


def test_unrelated_media_caption_can_be_removed_on_reimport(conn: sqlite3.Connection) -> None:
    with conn:
        insert_messages_with_fts(conn, [_message(16, text="caption", media_kind="photo", media_payload="{}")])
        insert_messages_with_fts(conn, [_message(16, text=None, media_kind="photo", media_payload="{}")])
    assert conn.execute("SELECT text FROM messages WHERE dialog_id=42 AND message_id=16").fetchone() == (None,)


def test_mark_message_deleted_is_idempotent_and_retains_text_and_fts(conn: sqlite3.Connection) -> None:
    with conn:
        insert_messages_with_fts(conn, [_message(13, text="retain me")])
    with conn:
        assert mark_message_deleted(conn, 42, 13, 500) is True
    with conn:
        assert mark_message_deleted(conn, 42, 13, 600) is False
    assert conn.execute(
        "SELECT text, is_deleted, deleted_at FROM messages WHERE dialog_id=42 AND message_id=13"
    ).fetchone() == (
        "retain me",
        1,
        500,
    )
    assert conn.execute("SELECT COUNT(*) FROM messages_fts WHERE dialog_id=42 AND message_id=13").fetchone() == (1,)


def test_list_undeleted_message_ids_uses_strict_cutoff(conn: sqlite3.Connection) -> None:
    with conn:
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
    with conn:
        insert_messages_with_fts(conn, [_message(30, text="before")])
    with pytest.raises(RuntimeError, match="abort"):
        with conn:
            assert persist_edited_message(conn, _message(30, text="after"), old_text="before", edit_date=700) == 1
            raise RuntimeError("abort")
    assert conn.execute("SELECT text FROM messages WHERE dialog_id=42 AND message_id=30").fetchone() == ("before",)
    assert conn.execute("SELECT COUNT(*) FROM message_versions WHERE dialog_id=42 AND message_id=30").fetchone() == (0,)
