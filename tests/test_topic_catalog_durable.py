"""Durable topic enumeration receipts, membership and realtime ordering."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import cast

import pytest

from mcp_telegram.observation_order import allocate_observation_order
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.topics.catalog import TopicCatalogRepository, TopicScan
from mcp_telegram.topics.contracts import TopicCursor, TopicFact, TopicPage, TopicSourceUnavailableError
from mcp_telegram.topics.sqlite_repository import SQLiteTopicMetadataRepository


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    path = tmp_path / "topics.sqlite"
    ensure_sync_schema(path)
    with closing(sqlite3.connect(path)) as conn:
        yield conn


def _enroll(conn: sqlite3.Connection, dialog_id: int = 42) -> TopicCatalogRepository:
    conn.execute(
        "INSERT INTO full_history_enrollment(dialog_id,enabled,source,updated_at) VALUES (?,1,'explicit',100)",
        (dialog_id,),
    )
    conn.commit()
    return TopicCatalogRepository(conn)


def _begin(repository: TopicCatalogRepository, dialog_id: int = 42) -> TopicScan:
    scan = repository.begin_page(dialog_id)
    assert scan is not None
    return scan


def _row(conn: sqlite3.Connection, sql: str, params: tuple[object, ...] = ()) -> tuple[object, ...]:
    row = cast(tuple[object, ...] | None, conn.execute(sql, params).fetchone())
    assert isinstance(row, tuple)
    return row


def _seed(conn: sqlite3.Connection, topic_id: int, title: str = "Retained") -> None:
    SQLiteTopicMetadataRepository(conn).upsert_topics(42, (TopicFact(topic_id, title),))


def test_legacy_rows_do_not_fabricate_complete_catalog(db: sqlite3.Connection) -> None:
    _seed(db, 7)
    db.execute("UPDATE topic_metadata SET snapshot_at=9999999999")
    db.commit()
    repository = TopicCatalogRepository(db)
    changes = db.total_changes

    coverage = repository.coverage(42)

    assert coverage["coverage"] == "partial"
    assert coverage["completed_at"] is None
    assert repository.coverage(99)["coverage"] == "not_observed"
    assert db.total_changes == changes


def test_authoritative_empty_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "empty.sqlite"
    ensure_sync_schema(path)
    with closing(sqlite3.connect(path)) as conn, conn:
        repository = _enroll(conn)
        repository.request(42, "explicit_enable")
        assert repository.apply_page(_begin(repository), TopicPage((), 0, False, None))
        receipt = repository.coverage(42)
        assert receipt["coverage"] == "complete"
        assert isinstance(receipt["completed_at"], int)
        assert receipt["pending"] is False
    with closing(sqlite3.connect(path)) as reopened:
        assert TopicCatalogRepository(reopened).coverage(42) == receipt
        assert _row(reopened, "SELECT COUNT(*) FROM topic_metadata") == (0,)


def test_new_request_preserves_prior_completion_but_reports_partial(db: sqlite3.Connection) -> None:
    repository = _enroll(db)
    repository.request(42, "explicit_enable")
    assert repository.apply_page(_begin(repository), TopicPage((TopicFact(1, "General"),), 1, False, None))
    completed_at = repository.coverage(42)["completed_at"]

    repository.request(42, "topic_changed")

    coverage = repository.coverage(42)
    assert coverage["coverage"] == "partial"
    assert coverage["completed_at"] == completed_at
    assert coverage["pending"] is True
    assert _row(db, "SELECT title,catalog_present FROM topic_metadata WHERE topic_id=1") == ("General", 1)


def test_partial_page_and_failure_preserve_omitted_topics(db: sqlite3.Connection) -> None:
    repository = _enroll(db)
    _seed(db, 99)
    repository.request(42, "explicit_enable")
    scan = _begin(repository)
    cursor = TopicCursor(100, 10, 1)

    assert not repository.apply_page(scan, TopicPage((TopicFact(1, "First"),), 2, False, cursor))
    repository.fail(_begin(repository), "flood_wait", 500)

    assert repository.coverage(42)["coverage"] == "partial"
    assert repository.coverage(42)["retry_at"] == 500
    assert _row(db, "SELECT title,catalog_present,is_deleted FROM topic_metadata WHERE topic_id=99") == (
        "Retained",
        1,
        0,
    )
    assert _row(db, "SELECT title FROM topic_metadata WHERE topic_id=1") == ("First",)
    assert _begin(repository).cursor == cursor


def test_complete_omission_changes_membership_not_deletion_or_history_title(db: sqlite3.Connection) -> None:
    repository = _enroll(db)
    _seed(db, 99, "Historical topic")
    db.execute("UPDATE topic_metadata SET pinned=1 WHERE topic_id=99")
    db.execute(
        "INSERT INTO messages(dialog_id,message_id,sent_at,text,forum_topic_id) VALUES (42,100,100,'retained',99)"
    )
    db.commit()
    repository.request(42, "explicit_enable")

    assert repository.apply_page(_begin(repository), TopicPage((TopicFact(1, "General"),), 1, False, None))

    assert _row(db, "SELECT title,catalog_present,is_deleted,pinned FROM topic_metadata WHERE topic_id=99") == (
        "Historical topic",
        0,
        0,
        1,
    )
    assert _row(
        db,
        "SELECT m.text,t.title FROM messages m LEFT JOIN topic_metadata t "
        "ON t.dialog_id=m.dialog_id AND t.topic_id=m.forum_topic_id WHERE m.message_id=100",
    ) == ("retained", "Historical topic")


@pytest.mark.parametrize("mutation", ["create", "edit", "hide", "pin", "pins"])
def test_post_start_event_survives_snapshot_and_keeps_invalidation_pending(
    db: sqlite3.Connection,
    mutation: str,
) -> None:
    repository = _enroll(db)
    _seed(db, 7, "Before event")
    repository.request(42, "explicit_enable")
    scan = _begin(repository)
    metadata = SQLiteTopicMetadataRepository(db)
    with write_transaction(db):
        if mutation == "create":
            metadata.apply_topic_create(42, 7, title="After event", icon_emoji_id=8, date=100, observed_at=100)
        elif mutation in {"edit", "hide"}:
            metadata.apply_topic_edit(
                42,
                7,
                title="After event",
                icon_emoji_id=8,
                hidden=mutation == "hide",
                observed_at=100,
            )
        elif mutation == "pin":
            metadata.apply_topic_pin(42, 7, pinned=True, observed_at=100)
        else:
            metadata.apply_topic_pins(42, (7,), observed_at=100)
        repository.request(42, "topic_changed")

    assert not repository.apply_page(
        scan, TopicPage((TopicFact(7, "Stale snapshot", icon_emoji_id=5),), 1, False, None)
    )

    expected_title = "After event" if mutation in {"create", "edit", "hide"} else "Before event"
    assert _row(db, "SELECT title FROM topic_metadata WHERE topic_id=7") == (expected_title,)
    if mutation in {"pin", "pins"}:
        assert _row(db, "SELECT pinned FROM topic_metadata WHERE topic_id=7") == (1,)
    if mutation == "hide":
        assert _row(db, "SELECT hidden FROM topic_metadata WHERE topic_id=7") == (1,)
    assert repository.coverage(42)["pending"] is True
    assert repository.coverage(42)["coverage"] == "partial"


def test_new_topic_after_scan_start_is_not_removed_by_omission(db: sqlite3.Connection) -> None:
    repository = _enroll(db)
    repository.request(42, "explicit_enable")
    scan = _begin(repository)
    with write_transaction(db):
        SQLiteTopicMetadataRepository(db).apply_topic_create(
            42,
            99,
            title="Created during fetch",
            icon_emoji_id=None,
            date=100,
            observed_at=100,
        )

    repository.apply_page(scan, TopicPage((TopicFact(1, "General"),), 1, False, None))

    assert _row(db, "SELECT catalog_present,is_deleted FROM topic_metadata WHERE topic_id=99") == (1, 0)


def test_per_page_observation_order_allows_newer_page_facts(db: sqlite3.Connection) -> None:
    repository = _enroll(db)
    _seed(db, 7, "Original")
    repository.request(42, "explicit_enable")
    first = _begin(repository)
    assert not repository.apply_page(first, TopicPage((TopicFact(1, "General"),), 2, False, TopicCursor(100, 10, 1)))
    with write_transaction(db):
        SQLiteTopicMetadataRepository(db).apply_topic_edit(
            42,
            7,
            title="Between pages",
            icon_emoji_id=None,
            hidden=None,
            observed_at=100,
        )
    second = _begin(repository)
    assert second.generation == first.generation
    assert second.observation_order > first.observation_order

    assert repository.apply_page(second, TopicPage((TopicFact(7, "Later fetched title"),), 2, False, None))

    assert _row(db, "SELECT title FROM topic_metadata WHERE topic_id=7") == ("Later fetched title",)


@pytest.mark.parametrize("count", [1, 3])
def test_count_drift_cannot_confirm_complete_or_remove_omitted_rows(db: sqlite3.Connection, count: int) -> None:
    repository = _enroll(db)
    _seed(db, 99)
    repository.request(42, "explicit_enable")
    assert not repository.apply_page(
        _begin(repository),
        TopicPage((TopicFact(1, "First"),), 2, False, TopicCursor(100, 10, 1)),
    )
    before = _row(db, "SELECT offset_date,offset_id,offset_topic FROM topic_catalog_state WHERE dialog_id=42")

    with pytest.raises(TopicSourceUnavailableError):
        repository.apply_page(_begin(repository), TopicPage((TopicFact(2, "Second"),), count, False, None))

    assert repository.coverage(42)["completed_at"] is None
    assert _row(db, "SELECT catalog_present FROM topic_metadata WHERE topic_id=99") == (1,)
    assert _row(db, "SELECT offset_date,offset_id,offset_topic FROM topic_catalog_state WHERE dialog_id=42") == before
    assert _row(db, "SELECT COUNT(*) FROM topic_metadata WHERE topic_id=2") == (0,)


@pytest.mark.parametrize("failure", ["repeated_topic", "repeated_cursor", "changed_order", "premature_empty"])
def test_invalid_page_does_not_advance_or_publish(db: sqlite3.Connection, failure: str) -> None:
    repository = _enroll(db)
    repository.request(42, "explicit_enable")
    cursor = TopicCursor(100, 10, 1)
    assert not repository.apply_page(_begin(repository), TopicPage((TopicFact(1, "First"),), 3, False, cursor))
    page = TopicPage(
        () if failure == "premature_empty" else (TopicFact(1 if failure == "repeated_topic" else 2, "Invalid"),),
        3,
        failure == "changed_order",
        cursor if failure == "repeated_cursor" else TopicCursor(90, 9, 2),
    )

    with pytest.raises(TopicSourceUnavailableError):
        repository.apply_page(_begin(repository), page)

    assert _begin(repository).cursor == cursor
    assert repository.coverage(42)["completed_at"] is None
    assert _row(db, "SELECT COUNT(*) FROM topic_metadata") == (1,)


def test_disabling_enrollment_during_fetch_rejects_page(db: sqlite3.Connection) -> None:
    repository = _enroll(db)
    repository.request(42, "explicit_enable")
    scan = _begin(repository)
    db.execute("UPDATE full_history_enrollment SET enabled=0 WHERE dialog_id=42")
    db.commit()

    assert not repository.apply_page(scan, TopicPage((TopicFact(1, "Not authorized"),), 1, False, None))

    assert _row(db, "SELECT COUNT(*) FROM topic_metadata") == (0,)
    assert repository.next_pending() is None
    assert repository.coverage(42)["pending"] is False


def test_old_generation_cannot_publish_after_replacement(db: sqlite3.Connection) -> None:
    repository = _enroll(db)
    repository.request(42, "explicit_enable")
    old = _begin(repository)
    repository.fail(old, "topic_count_changed", 500)
    current = _begin(repository)
    assert current.generation != old.generation

    assert not repository.apply_page(old, TopicPage((TopicFact(1, "Stale generation"),), 1, False, None))

    assert _row(db, "SELECT COUNT(*) FROM topic_metadata") == (0,)
    assert _begin(repository).generation == current.generation


def test_not_applicable_is_stable_across_bootstrap(db: sqlite3.Connection) -> None:
    repository = _enroll(db)
    repository.request(42, "explicit_enable")
    repository.mark_not_applicable(_begin(repository))
    receipt = repository.coverage(42)

    repository.bootstrap()

    assert receipt["coverage"] == "not_applicable"
    assert receipt["completed_at"] is None
    assert repository.coverage(42) == receipt
    assert repository.next_pending() is None


def test_restart_resumes_committed_page_not_first_page(tmp_path: Path) -> None:
    path = tmp_path / "resume.sqlite"
    ensure_sync_schema(path)
    cursor = TopicCursor(100, 10, 1)
    with closing(sqlite3.connect(path)) as conn, conn:
        repository = _enroll(conn)
        repository.request(42, "explicit_enable")
        first = _begin(repository)
        assert not repository.apply_page(first, TopicPage((TopicFact(1, "First"),), 2, False, cursor))
    with closing(sqlite3.connect(path)) as reopened:
        repository = TopicCatalogRepository(reopened)
        resumed = _begin(repository)
        assert resumed.generation == first.generation
        assert resumed.cursor == cursor
        assert repository.apply_page(resumed, TopicPage((TopicFact(2, "Second"),), 2, False, None))
        assert repository.coverage(42)["coverage"] == "complete"
        assert _row(reopened, "SELECT COUNT(*) FROM topic_metadata WHERE catalog_present=1") == (2,)


def test_explicit_deleted_constructor_retains_existing_title_and_marks_tombstone(db: sqlite3.Connection) -> None:
    repository = _enroll(db)
    _seed(db, 7, "Deleted topic name")
    repository.request(42, "explicit_enable")

    assert repository.apply_page(_begin(repository), TopicPage((), 2, False, None, deleted_ids=(7, 8)))

    assert _row(db, "SELECT title,is_deleted FROM topic_metadata WHERE topic_id=7") == ("Deleted topic name", 1)
    assert _row(db, "SELECT title,is_deleted FROM topic_metadata WHERE topic_id=8") == ("", 1)
    assert repository.coverage(42)["coverage"] == "complete"


def test_old_failure_cannot_delay_replacement_generation(db: sqlite3.Connection) -> None:
    repository = _enroll(db)
    repository.request(42, "explicit_enable")
    old = _begin(repository)
    repository.fail(old, "topic_count_changed", 500)
    repository.request(42, "retry_now")
    current = _begin(repository)

    repository.fail(old, "flood_wait", 999999)

    assert repository.coverage(42)["retry_at"] is None
    assert _begin(repository).generation == current.generation


def test_old_capability_result_cannot_finish_replacement_generation(db: sqlite3.Connection) -> None:
    repository = _enroll(db)
    repository.request(42, "explicit_enable")
    old = _begin(repository)
    repository.fail(old, "topic_count_changed", 500)
    current = _begin(repository)

    repository.mark_not_applicable(old)

    assert repository.coverage(42)["pending"] is True
    assert _begin(repository).generation == current.generation


def test_topic_catalog_migration_preserves_legacy_rows_without_receipts(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite"
    ensure_sync_schema(path)
    with closing(sqlite3.connect(path)) as conn, conn:
        _seed(conn, 7, "Legacy title")
        conn.execute("UPDATE topic_metadata SET pinned=1,hidden=1,icon_emoji_id=77,icon_emoji='🧭'")
        # Undo only the new schema on this test-owned database to reproduce v83.
        conn.execute("DROP TRIGGER topic_catalog_identity_changed")
        conn.execute("DROP TABLE topic_catalog_state")
        conn.execute("ALTER TABLE topic_metadata DROP COLUMN catalog_present")
        conn.execute("ALTER TABLE topic_metadata DROP COLUMN seen_generation")
        conn.execute("DELETE FROM schema_version WHERE version>=84")
    ensure_sync_schema(path)

    with closing(sqlite3.connect(path)) as migrated:
        assert _row(
            migrated,
            "SELECT title,pinned,hidden,icon_emoji_id,icon_emoji,catalog_present,seen_generation FROM topic_metadata",
        ) == ("Legacy title", 1, 1, 77, "🧭", 1, 0)
        assert _row(migrated, "SELECT COUNT(*) FROM topic_catalog_state") == (0,)
        assert TopicCatalogRepository(migrated).coverage(42)["coverage"] == "partial"


@pytest.mark.parametrize("source", ["explicit", "automatic"])
def test_bootstrap_reuses_enabled_enrollment_without_creating_new_scope(db: sqlite3.Connection, source: str) -> None:
    db.execute(
        "INSERT INTO full_history_enrollment(dialog_id,enabled,source,updated_at) VALUES (42,1,?,100)",
        (source,),
    )
    db.execute("INSERT INTO full_history_enrollment(dialog_id,enabled,source,updated_at) VALUES (43,0,'explicit',100)")
    db.commit()
    repository = TopicCatalogRepository(db)

    repository.bootstrap()

    assert repository.next_pending() == (42, 0)
    assert _row(db, "SELECT COUNT(*) FROM topic_catalog_state") == (1,)
    assert _row(db, "SELECT COUNT(*) FROM full_history_enrollment") == (2,)
    assert repository.coverage(43)["coverage"] == "not_observed"


@pytest.mark.parametrize("via_event", [False, True])
def test_changed_custom_icon_does_not_reuse_previous_unicode(db: sqlite3.Connection, via_event: bool) -> None:
    metadata = SQLiteTopicMetadataRepository(db)
    metadata.upsert_topics(42, (TopicFact(7, "Topic", icon_emoji_id=77, icon_emoji="🧭"),))

    if via_event:
        with write_transaction(db):
            metadata.apply_topic_edit(42, 7, title=None, icon_emoji_id=88, hidden=None, observed_at=100)
    else:
        metadata.upsert_topics(42, (TopicFact(7, "Topic", icon_emoji_id=88),))

    assert _row(db, "SELECT icon_emoji_id,icon_emoji FROM topic_metadata WHERE topic_id=7") == (88, None)


def test_overlapping_pages_complete_using_distinct_topic_count(db: sqlite3.Connection) -> None:
    repository = _enroll(db)
    repository.request(42, "explicit_enable")
    assert not repository.apply_page(
        _begin(repository),
        TopicPage((TopicFact(1, "One"), TopicFact(2, "Two")), 3, False, TopicCursor(100, 20, 2)),
    )

    assert repository.apply_page(
        _begin(repository),
        TopicPage((TopicFact(2, "Two updated"), TopicFact(3, "Three")), 3, False, None),
    )

    assert repository.coverage(42)["coverage"] == "complete"
    assert _row(db, "SELECT COUNT(*) FROM topic_metadata WHERE catalog_present=1") == (3,)


def test_transient_source_failure_retains_committed_cursor_and_generation(db: sqlite3.Connection) -> None:
    repository = _enroll(db)
    repository.request(42, "explicit_enable")
    first = _begin(repository)
    cursor = TopicCursor(100, 10, 1)
    assert not repository.apply_page(first, TopicPage((TopicFact(1, "First"),), 2, False, cursor))
    failed = _begin(repository)

    repository.fail(failed, "topic_source_unavailable", 500)

    resumed = _begin(repository)
    assert resumed.generation == first.generation
    assert resumed.cursor == cursor
    assert repository.coverage(42)["retry_at"] == 500


def test_capability_result_cannot_clear_new_request_or_leave_uncapturable_old_revision(db: sqlite3.Connection) -> None:
    repository = _enroll(db)
    repository.request(42, "explicit_enable")
    old = _begin(repository)
    repository.request(42, "type_changed")

    repository.mark_not_applicable(old)

    assert repository.coverage(42)["pending"] is True
    newer = _begin(repository)
    assert newer.revision > old.revision
    assert newer.generation != old.generation


def test_final_deleted_page_completes_after_resumed_active_page(db: sqlite3.Connection) -> None:
    repository = _enroll(db)
    repository.request(42, "explicit_enable")
    assert not repository.apply_page(
        _begin(repository),
        TopicPage((TopicFact(1, "General"),), 2, False, TopicCursor(100, 10, 1)),
    )

    assert repository.apply_page(_begin(repository), TopicPage((), 2, False, None, deleted_ids=(7,)))

    assert repository.coverage(42)["coverage"] == "complete"
    assert _row(db, "SELECT is_deleted FROM topic_metadata WHERE topic_id=7") == (1,)


def test_nonterminal_page_without_cursor_rolls_back_facts_and_position(db: sqlite3.Connection) -> None:
    repository = _enroll(db)
    repository.request(42, "explicit_enable")
    scan = _begin(repository)

    with pytest.raises(TopicSourceUnavailableError):
        repository.apply_page(scan, TopicPage((), 2, False, None, deleted_ids=(7,)))

    assert _begin(repository).cursor == TopicCursor()
    assert repository.coverage(42)["completed_at"] is None
    assert _row(db, "SELECT COUNT(*) FROM topic_metadata") == (0,)


def test_new_capability_observation_reopens_not_applicable_without_ttl(db: sqlite3.Connection) -> None:
    repository = _enroll(db)
    assert repository.observe_capability(42, False, 100)
    assert repository.coverage(42)["coverage"] == "not_applicable"

    assert repository.observe_capability(42, True, 101)

    assert repository.coverage(42)["pending"] is True
    assert repository.coverage(42)["coverage"] != "not_applicable"
    assert repository.next_pending() == (42, 0)


def test_repeated_and_older_capability_observations_do_not_requeue(db: sqlite3.Connection) -> None:
    repository = _enroll(db)
    assert repository.observe_capability(42, True, 100)
    assert repository.apply_page(_begin(repository), TopicPage((), 0, False, None))
    receipt = repository.coverage(42)

    assert not repository.observe_capability(42, True, 101)
    assert not repository.observe_capability(42, False, 99)

    assert repository.coverage(42) == receipt
    assert repository.next_pending() is None


def test_capability_change_fences_already_inflight_page(db: sqlite3.Connection) -> None:
    repository = _enroll(db)
    repository.request(42, "explicit_enable")
    old = _begin(repository)
    assert repository.observe_capability(42, False, old.observation_order + 1)

    assert not repository.apply_page(old, TopicPage((TopicFact(1, "Stale result"),), 1, False, None))

    assert repository.coverage(42)["coverage"] == "not_applicable"
    assert _row(db, "SELECT COUNT(*) FROM topic_metadata") == (0,)


@pytest.mark.parametrize("recovery", ["explicit_enable", "recovery_gap", "same_capability"])
def test_blocked_catalog_preserves_receipt_and_cursor_until_authorized_recovery(
    db: sqlite3.Connection,
    recovery: str,
) -> None:
    repository = _enroll(db)
    assert repository.observe_capability(42, True, allocate_observation_order(db))
    assert repository.apply_page(_begin(repository), TopicPage((TopicFact(1, "First"),), 1, False, None))
    receipt = repository.coverage(42)["completed_at"]
    repository.request(42, "refresh")
    cursor = TopicCursor(100, 10, 1)
    assert not repository.apply_page(_begin(repository), TopicPage((TopicFact(1, "First"),), 2, False, cursor))
    scan = _begin(repository)

    repository.fail(scan, "source_access_unavailable", None)
    repository.bootstrap()

    coverage = repository.coverage(42)
    assert coverage["coverage"] == "partial"
    assert coverage["completed_at"] == receipt
    assert coverage["pending"] is False
    assert coverage["reason"] == "source_access_unavailable"
    assert coverage["retry_at"] is None
    assert repository.next_pending() is None
    assert _row(db, "SELECT generation,offset_date,offset_id,offset_topic FROM topic_catalog_state") == (
        scan.generation,
        100,
        10,
        1,
    )
    assert _row(db, "SELECT title,catalog_present,is_deleted FROM topic_metadata") == ("First", 1, 0)

    if recovery == "same_capability":
        assert repository.observe_capability(42, True, allocate_observation_order(db))
    elif recovery == "recovery_gap":
        repository.request_recovery()
    else:
        repository.request(42, "explicit_enable")

    assert repository.next_pending() == (42, 0)
    assert repository.coverage(42)["pending"] is True
    assert repository.coverage(42)["completed_at"] == receipt
    assert repository.coverage(42)["reason"] != "source_access_unavailable"


@pytest.mark.parametrize("newer_evidence", ["same_capability", "request", "generation"])
def test_inflight_forbidden_cannot_override_newer_evidence(
    db: sqlite3.Connection,
    newer_evidence: str,
) -> None:
    repository = _enroll(db)
    assert repository.observe_capability(42, True, allocate_observation_order(db))
    old = _begin(repository)
    if newer_evidence == "same_capability":
        assert not repository.observe_capability(42, True, allocate_observation_order(db))
    elif newer_evidence == "request":
        repository.request(42, "explicit_enable")
    else:
        repository.fail(old, "topic_count_changed", 500)
        repository.request(42, "explicit_enable")
        assert _begin(repository).generation != old.generation
    before = repository.coverage(42)

    repository.fail(old, "source_access_unavailable", None)

    assert repository.coverage(42) == before
    assert repository.next_pending() == (42, 0)


def test_delayed_same_capability_cannot_reopen_newer_forbidden_observation(db: sqlite3.Connection) -> None:
    repository = _enroll(db)
    assert repository.observe_capability(42, True, allocate_observation_order(db))
    delayed_order = allocate_observation_order(db)
    blocked_scan = _begin(repository)
    assert delayed_order < blocked_scan.observation_order
    repository.fail(blocked_scan, "source_access_unavailable", None)
    blocked = repository.coverage(42)

    assert not repository.observe_capability(42, True, delayed_order)

    assert repository.coverage(42) == blocked
    assert repository.next_pending() is None
    assert repository.observe_capability(42, True, allocate_observation_order(db))
    assert repository.next_pending() == (42, 0)
