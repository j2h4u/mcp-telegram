"""Focused TG-3 source-order and recovery-fence checks."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from itertools import permutations
from pathlib import Path
from typing import cast

import pytest
from telethon.tl import types  # type: ignore[import-untyped]

from mcp_telegram.config import DraftRecoveryConfig
from mcp_telegram.drafts.contracts import (
    DraftComposition,
    DraftDisposition,
    DraftObservation,
    DraftObservationSource,
    DraftScope,
    SnapshotCoverage,
)
from mcp_telegram.drafts.sqlite_projection import SQLiteDraftProjection
from mcp_telegram.drafts.telethon_adapter import normalize_update_draft
from mcp_telegram.sync_db import ensure_sync_schema


@pytest.fixture()
def projection(tmp_path: Path) -> Iterator[tuple[sqlite3.Connection, SQLiteDraftProjection]]:
    database = tmp_path / "sync.db"
    ensure_sync_schema(database)
    conn = sqlite3.connect(database)
    repository = SQLiteDraftProjection(conn, DraftRecoveryConfig())
    repository.bind_account(100)
    try:
        yield conn, repository
    finally:
        conn.close()


def _at(second: int) -> datetime:
    return datetime(2026, 9, 22, tzinfo=UTC) + timedelta(seconds=second)


def _fetchone(
    conn: sqlite3.Connection,
    statement: str,
    parameters: tuple[object, ...] = (),
) -> tuple[object, ...] | None:
    return cast(tuple[object, ...] | None, conn.execute(statement, parameters).fetchone())


def _recovery_state(conn: sqlite3.Connection) -> tuple[int | None, int]:
    return cast(
        tuple[int | None, int],
        conn.execute(
            "SELECT recovery_due_at,recovery_failure_count FROM draft_projection_runtime WHERE singleton=1"
        ).fetchone(),
    )


def _present(
    scope: DraftScope,
    order: int | None,
    text: str,
    *,
    source: DraftObservationSource = DraftObservationSource.REALTIME,
) -> DraftObservation:
    source_order = None if order is None else _at(order)
    return DraftObservation(
        scope=scope,
        disposition=DraftDisposition.PRESENT,
        source=source,
        observed_at=_at(20),
        composition=DraftComposition(text=text, date=source_order),
        source_order_at=source_order,
    )


def _empty(
    scope: DraftScope,
    order: int | None,
    *,
    source: DraftObservationSource = DraftObservationSource.REALTIME,
) -> DraftObservation:
    return DraftObservation(
        scope=scope,
        disposition=DraftDisposition.TOMBSTONE,
        source=source,
        observed_at=_at(20),
        source_order_at=None if order is None else _at(order),
    )


def _claim(repository: SQLiteDraftProjection, second: int) -> int:
    repository.mark_recovery_needed(reason="test", observed_at=_at(second))
    claim = repository.claim_recovery(now=_at(second).timestamp())
    assert claim is not None
    return claim


def _coverage(update_count: int, *, second: int) -> SnapshotCoverage:
    return SnapshotCoverage(100, True, update_count, source_order_at=_at(second))


def test_source_order_wins_over_callback_arrival_and_clear_is_ordered(
    projection: tuple[sqlite3.Connection, SQLiteDraftProjection],
) -> None:
    conn, repository = projection
    scope = DraftScope(100, 200)
    assert repository.apply_realtime(_present(scope, 10, "new")).publication_changed
    older = repository.apply_realtime(_present(scope, 9, "old"))
    assert older.decision == "stale"
    assert conn.execute("SELECT text FROM draft_current").fetchone() == ("new",)
    cleared = repository.apply_realtime(_empty(scope, 11))
    assert cleared.publication_changed
    assert conn.execute("SELECT state,source_order_at FROM draft_current").fetchone() == ("empty", _at(11).timestamp())


def test_equal_conflict_and_missing_date_request_recovery_without_writes(
    projection: tuple[sqlite3.Connection, SQLiteDraftProjection],
) -> None:
    conn, repository = projection
    scope = DraftScope(100, 200)
    repository.apply_realtime(_present(scope, 10, "visible"))
    revision = _fetchone(conn, "SELECT projection_revision FROM draft_current")
    assert revision is not None
    conflict = repository.apply_realtime(_present(scope, 10, "other"))
    missing = repository.apply_realtime(_present(scope, None, "other"))
    assert conflict.decision == "equal_order_conflict"
    assert missing.decision == "missing_source_order"
    assert conn.execute("SELECT text,projection_revision FROM draft_current").fetchone() == ("visible", revision[0])
    assert conn.execute("SELECT status FROM draft_sync_state").fetchone() == ("recovery_needed",)


def test_explicit_ambiguous_dated_observation_never_publishes(
    projection: tuple[sqlite3.Connection, SQLiteDraftProjection],
) -> None:
    conn, repository = projection
    scope = DraftScope(100, 203)
    ambiguous = DraftObservation(
        scope=scope,
        disposition=DraftDisposition.PRESENT,
        source=DraftObservationSource.REALTIME,
        observed_at=_at(100),
        composition=DraftComposition(text="ambiguous", date=_at(10)),
        ambiguity=True,
        source_order_at=_at(10),
    )
    result = repository.apply_realtime(ambiguous)
    assert result.decision == "ambiguous_realtime"
    assert conn.execute("SELECT COUNT(*) FROM draft_current WHERE dialog_id=203").fetchone() == (0,)


def test_missing_date_identical_is_a_safe_noop_and_exact_duplicate_keeps_revision(
    projection: tuple[sqlite3.Connection, SQLiteDraftProjection],
) -> None:
    conn, repository = projection
    scope = DraftScope(100, 200)
    first = repository.apply_realtime(_present(scope, 10, "visible"))
    duplicate = repository.apply_realtime(_present(scope, 10, "visible"))
    missing = repository.apply_realtime(_present(scope, None, "visible"))
    assert duplicate.decision == "duplicate"
    assert missing.decision == "missing_source_order"
    assert duplicate.revision == first.revision
    assert missing.revision == first.revision
    assert conn.execute("SELECT text,projection_revision FROM draft_current").fetchone() == (
        "visible",
        first.revision,
    )


def test_snapshot_floor_rejects_old_unseen_scope_but_accepts_new_scope(
    projection: tuple[sqlite3.Connection, SQLiteDraftProjection],
) -> None:
    conn, repository = projection
    first_scope = DraftScope(100, 200)
    unseen_scope = DraftScope(100, 201)
    claim = _claim(repository, 10)
    snapshot = DraftObservation(
        scope=first_scope,
        disposition=DraftDisposition.PRESENT,
        source=DraftObservationSource.SNAPSHOT,
        observed_at=_at(20),
        composition=DraftComposition(text="snapshot", date=_at(20)),
        source_order_at=_at(20),
    )
    assert repository.apply_snapshot([snapshot], _coverage(1, second=20), claim_token=claim).accepted
    old = repository.apply_realtime(_present(unseen_scope, 19, "old"))
    new = repository.apply_realtime(_present(unseen_scope, 21, "new"))
    assert old.decision == "stale"
    assert new.publication_changed
    assert conn.execute("SELECT text FROM draft_current WHERE dialog_id=201").fetchone() == ("new",)


def test_snapshot_envelope_substitutes_missing_row_date_and_rejects_older_replay(
    projection: tuple[sqlite3.Connection, SQLiteDraftProjection],
) -> None:
    conn, repository = projection
    scope = DraftScope(100, 202)
    claim = _claim(repository, 10)
    missing_row_date = DraftObservation(
        scope=scope,
        disposition=DraftDisposition.PRESENT,
        source=DraftObservationSource.SNAPSHOT,
        observed_at=_at(20),
        composition=DraftComposition(text="snapshot", date=None),
    )
    assert repository.apply_snapshot([missing_row_date], _coverage(1, second=20), claim_token=claim).accepted
    assert conn.execute("SELECT source_order_at FROM draft_current WHERE dialog_id=202").fetchone() == (
        _at(20).timestamp(),
    )
    assert repository.apply_realtime(_present(scope, 19, "older")).decision == "stale"


def test_distinct_source_dates_converge_to_same_projection_in_any_callback_order(
    projection: tuple[sqlite3.Connection, SQLiteDraftProjection],
) -> None:
    _, repository = projection
    outcomes: list[str] = []
    for order in permutations((1, 2, 3)):
        scope = DraftScope(100, 300 + len(outcomes))
        for second in order:
            result = repository.apply_realtime(_present(scope, second, f"v{second}"))
            if result.publication_changed:
                outcomes.append(str(second))
        row = _fetchone(
            repository._conn,
            "SELECT text,source_order_at FROM draft_current WHERE dialog_id=?",
            (scope.dialog_id,),
        )
        assert row == ("v3", _at(3).timestamp())
    assert len(outcomes) >= 6


def test_topic_and_saved_peer_scopes_are_independent(
    projection: tuple[sqlite3.Connection, SQLiteDraftProjection],
) -> None:
    conn, repository = projection
    topic = DraftScope(100, 200, top_message_id=7)
    saved = DraftScope(100, 200, subdialog_peer_id=-300)
    assert repository.apply_realtime(_present(topic, 10, "topic")).publication_changed
    assert repository.apply_realtime(_present(saved, 10, "saved")).publication_changed
    assert conn.execute(
        "SELECT top_message_id,subdialog_peer_id,text FROM draft_current WHERE dialog_id=200 "
        "ORDER BY top_message_id,subdialog_peer_id"
    ).fetchall() == [(0, -300, "saved"), (7, 0, "topic")]


def test_ambiguity_coalesces_without_resetting_backoff(
    projection: tuple[sqlite3.Connection, SQLiteDraftProjection],
) -> None:
    conn, repository = projection
    repository.mark_recovery_needed(reason="first", observed_at=_at(1))
    token = repository.claim_recovery(now=_at(1).timestamp())
    assert token is not None
    assert repository.rearm_recovery(reason="failed", now=_at(1).timestamp(), claim_token=token)
    due_before = _recovery_state(conn)
    repository.apply_realtime(_present(DraftScope(100, 200), None, "ambiguous"))
    due_after = _recovery_state(conn)
    assert due_before == due_after


def test_repeated_claim_collisions_advance_backoff_and_converge(
    projection: tuple[sqlite3.Connection, SQLiteDraftProjection],
) -> None:
    conn, repository = projection
    scope = DraftScope(100, 205)
    repository.apply_realtime(_present(scope, 10, "one"))
    first_claim = _claim(repository, 11)
    assert repository.apply_realtime(_present(scope, 12, "two")).publication_changed
    first_due, first_failures = _recovery_state(conn)
    assert first_due is not None
    second_claim = repository.claim_recovery(now=first_due)
    assert second_claim is not None
    assert repository.apply_realtime(_present(scope, 13, "three")).publication_changed
    second_due, second_failures = _recovery_state(conn)
    assert second_due is not None
    assert second_failures > first_failures
    assert second_due > first_due
    assert first_claim != second_claim


def test_migrated_v75_null_order_rows_are_fenced_until_authoritative_bootstrap(tmp_path: Path) -> None:
    database = tmp_path / "legacy.db"
    ensure_sync_schema(database)
    conn = sqlite3.connect(database)
    conn.execute("ALTER TABLE draft_current DROP COLUMN source_order_at")
    conn.execute("ALTER TABLE draft_sync_state DROP COLUMN source_order_floor")
    conn.execute("DELETE FROM schema_version WHERE version=76")
    conn.commit()
    conn.close()
    ensure_sync_schema(database)
    conn = sqlite3.connect(database)
    repository = SQLiteDraftProjection(conn, DraftRecoveryConfig())
    repository.bind_account(100)
    conn.execute(
        "INSERT INTO draft_current(account_id,dialog_id,state,text,composition_complete,source_kind,"
        "source_observed_at,observation_started_at,observation_completed_at,projection_revision,normalization_version) "
        "VALUES (100,401,'present','legacy',1,'realtime_present',1,1,1,1,1)"
    )
    conn.execute(
        "INSERT INTO draft_current(account_id,dialog_id,state,composition_complete,source_kind,"
        "source_observed_at,observation_started_at,observation_completed_at,projection_revision,normalization_version) "
        "VALUES (100,402,'empty',1,'realtime_empty',1,1,1,2,1)"
    )
    conn.commit()
    repository.bind_account(100)
    blocked_present = repository.apply_realtime(_present(DraftScope(100, 401), 10, "replay"))
    blocked_clear = repository.apply_realtime(_present(DraftScope(100, 402), 10, "resurrect"))
    assert blocked_present.decision == "legacy_order_pending"
    assert blocked_clear.decision == "legacy_order_pending"
    repository.mark_recovery_needed(reason="test", observed_at=datetime.now(UTC))
    claim = repository.claim_recovery(now=datetime.now(UTC).timestamp())
    assert claim is not None
    snapshot = _present(DraftScope(100, 401), 20, "authoritative")
    snapshot = DraftObservation(
        snapshot.scope,
        snapshot.disposition,
        DraftObservationSource.SNAPSHOT,
        _at(20),
        snapshot.composition,
        source_order_at=_at(20),
    )
    assert repository.apply_snapshot([snapshot], _coverage(1, second=20), claim_token=claim).accepted
    assert repository.apply_realtime(_present(DraftScope(100, 401), 19, "older")).decision == "stale"
    assert repository.apply_realtime(_present(DraftScope(100, 401), 21, "newer")).publication_changed
    conn.close()


def test_snapshot_floor_reconciles_absence_and_race_supersedes_snapshot(
    projection: tuple[sqlite3.Connection, SQLiteDraftProjection],
) -> None:
    conn, repository = projection
    scope = DraftScope(100, 200)
    repository.apply_realtime(_present(scope, 10, "old"))
    claim = _claim(repository, 11)
    snapshot = DraftObservation(
        scope=scope,
        disposition=DraftDisposition.PRESENT,
        source=DraftObservationSource.SNAPSHOT,
        observed_at=_at(12),
        composition=DraftComposition(text="snapshot", date=_at(12)),
        source_order_at=_at(12),
    )
    assert repository.apply_snapshot(
        [snapshot],
        SnapshotCoverage(100, True, 1, source_order_at=_at(12)),
        claim_token=claim,
    ).accepted
    assert (
        conn.execute("SELECT source_order_floor,text FROM draft_sync_state JOIN draft_current").fetchone() is not None
    )
    assert repository.apply_realtime(_present(scope, 13, "after")).publication_changed

    claim = _claim(repository, 14)
    before = _fetchone(conn, "SELECT text,projection_revision FROM draft_current")
    assert before is not None
    live = repository.apply_realtime(_present(scope, 15, "live"))
    assert live.publication_changed
    displaced = repository.apply_snapshot(
        [snapshot],
        SnapshotCoverage(100, True, 1, source_order_at=_at(12)),
        claim_token=claim,
    )
    assert displaced.decision == "snapshot_superseded"
    assert conn.execute("SELECT text,projection_revision FROM draft_current").fetchone() != before
    assert conn.execute("SELECT text FROM draft_current").fetchone() == ("live",)


def test_advancing_claim_collision_persists_clear_and_rejects_delayed_older_present(
    projection: tuple[sqlite3.Connection, SQLiteDraftProjection],
) -> None:
    conn, repository = projection
    scope = DraftScope(100, 210)
    repository.apply_realtime(_present(scope, 10, "draft"))
    _claim(repository, 11)
    clear = repository.apply_realtime(
        DraftObservation(
            scope=scope,
            disposition=DraftDisposition.TOMBSTONE,
            source=DraftObservationSource.REALTIME,
            observed_at=_at(20),
            source_order_at=_at(20),
        )
    )
    delayed = repository.apply_realtime(_present(scope, 15, "old"))
    assert clear.publication_changed
    assert delayed.decision == "stale"
    assert conn.execute("SELECT state,source_order_at FROM draft_current WHERE dialog_id=210").fetchone() == (
        "empty",
        _at(20).timestamp(),
    )


def test_no_date_empty_preserves_visible_draft_and_requests_recovery(
    projection: tuple[sqlite3.Connection, SQLiteDraftProjection],
) -> None:
    conn, repository = projection
    scope = DraftScope(100, 211)
    repository.apply_realtime(_present(scope, 20, "visible"))
    no_date_clear = DraftObservation(
        scope=scope,
        disposition=DraftDisposition.TOMBSTONE,
        source=DraftObservationSource.REALTIME,
        observed_at=_at(100),
    )
    result = repository.apply_realtime(no_date_clear)
    assert result.decision == "missing_source_order"
    assert conn.execute("SELECT state,text FROM draft_current WHERE dialog_id=211").fetchone() == (
        "present",
        "visible",
    )
    assert repository.recovery_due_at() is not None


def test_no_date_identical_empty_invalidates_claim_without_revision_and_supersedes_snapshot(
    projection: tuple[sqlite3.Connection, SQLiteDraftProjection],
) -> None:
    conn, repository = projection
    scope = DraftScope(100, 212)
    repository.apply_realtime(_empty(scope, 10))
    revision = _fetchone(conn, "SELECT projection_revision FROM draft_current WHERE dialog_id=212")
    assert revision is not None
    claim = _claim(repository, 11)

    observation = normalize_update_draft(
        types.UpdateDraftMessage(types.PeerUser(212), types.DraftMessageEmpty()),
        account_id=100,
        source=DraftObservationSource.REALTIME,
        observed_at=_at(100),
    )
    assert observation is not None
    result = repository.apply_realtime(observation)

    assert result.accepted
    assert result.ambiguous
    assert result.decision == "missing_source_order"
    assert conn.execute("SELECT state,projection_revision FROM draft_current WHERE dialog_id=212").fetchone() == (
        "empty",
        revision[0],
    )
    delayed_snapshot = _present(DraftScope(100, 212), 20, "created", source=DraftObservationSource.SNAPSHOT)
    displaced = repository.apply_snapshot(
        [delayed_snapshot],
        _coverage(1, second=20),
        claim_token=claim,
    )
    assert displaced.decision == "snapshot_superseded"
    assert conn.execute("SELECT state,projection_revision FROM draft_current WHERE dialog_id=212").fetchone() == (
        "empty",
        revision[0],
    )
    delayed_realtime = _present(DraftScope(100, 212), 20, "created", source=DraftObservationSource.REALTIME)
    blocked = repository.apply_realtime(delayed_realtime)
    assert blocked.decision == "scope_order_uncertain"
    assert not blocked.publication_changed
    assert conn.execute("SELECT state,projection_revision FROM draft_current WHERE dialog_id=212").fetchone() == (
        "empty",
        revision[0],
    )


def test_scope_uncertainty_survives_restart_and_unrelated_scope_progresses(
    tmp_path: Path,
) -> None:
    database = tmp_path / "restart.db"
    ensure_sync_schema(database)
    conn = sqlite3.connect(database)
    repository = SQLiteDraftProjection(conn, DraftRecoveryConfig())
    repository.bind_account(100)
    uncertain_scope = DraftScope(100, 213)
    unrelated_scope = DraftScope(100, 214)
    repository.apply_realtime(_empty(uncertain_scope, 10))
    repository.apply_realtime(
        DraftObservation(
            scope=uncertain_scope,
            disposition=DraftDisposition.TOMBSTONE,
            source=DraftObservationSource.REALTIME,
            observed_at=_at(100),
        )
    )
    conn.close()

    restarted_conn = sqlite3.connect(database)
    restarted = SQLiteDraftProjection(restarted_conn, DraftRecoveryConfig())
    restarted.bind_account(100)
    blocked = restarted.apply_realtime(_present(uncertain_scope, 20, "resurrected"))
    progressed = restarted.apply_realtime(_present(unrelated_scope, 20, "independent"))
    assert blocked.decision == "scope_order_uncertain"
    assert progressed.publication_changed
    assert restarted_conn.execute(
        "SELECT text FROM draft_current WHERE account_id=100 AND dialog_id=214"
    ).fetchone() == ("independent",)
    restarted_conn.close()


def test_authoritative_snapshot_clears_scope_uncertainty_and_allows_newer_realtime(
    projection: tuple[sqlite3.Connection, SQLiteDraftProjection],
) -> None:
    conn, repository = projection
    scope = DraftScope(100, 215)
    repository.apply_realtime(_empty(scope, 10))
    repository.apply_realtime(
        DraftObservation(
            scope=scope,
            disposition=DraftDisposition.TOMBSTONE,
            source=DraftObservationSource.REALTIME,
            observed_at=_at(100),
        )
    )
    repository.mark_recovery_needed(reason="test", observed_at=_at(30))
    due = repository.recovery_due_at()
    assert due is not None
    claim = repository.claim_recovery(now=due)
    assert claim is not None
    result = repository.apply_snapshot([], _coverage(0, second=30), claim_token=claim)
    assert result.accepted
    assert repository.apply_realtime(_present(scope, 31, "newer")).publication_changed
    assert conn.execute("SELECT state,text FROM draft_current WHERE dialog_id=215").fetchone() == (
        "present",
        "newer",
    )


def test_authoritative_confirmation_advances_scope_order_without_revision(
    projection: tuple[sqlite3.Connection, SQLiteDraftProjection],
) -> None:
    conn, repository = projection
    scope = DraftScope(100, 216)
    first = repository.apply_realtime(_empty(scope, 10))
    claim = _claim(repository, 30)
    confirmed = repository.apply_snapshot(
        [_empty(scope, 30, source=DraftObservationSource.SNAPSHOT)],
        _coverage(1, second=30),
        claim_token=claim,
    )
    assert confirmed.accepted
    assert conn.execute(
        "SELECT state,source_order_at,projection_revision FROM draft_current WHERE dialog_id=216"
    ).fetchone() == ("empty", _at(30).timestamp(), first.revision)

    repository.apply_realtime(
        DraftObservation(
            scope=scope,
            disposition=DraftDisposition.TOMBSTONE,
            source=DraftObservationSource.REALTIME,
            observed_at=_at(100),
        )
    )
    due = repository.recovery_due_at()
    assert due is not None
    next_claim = repository.claim_recovery(now=due)
    assert next_claim is not None
    changed = repository.apply_snapshot(
        [_present(scope, 20, "draft", source=DraftObservationSource.SNAPSHOT)],
        _coverage(1, second=40),
        claim_token=next_claim,
    )
    assert changed.accepted
    assert conn.execute("SELECT source_order_at FROM draft_current WHERE dialog_id=216").fetchone() == (
        _at(40).timestamp(),
    )
    assert repository.apply_realtime(_present(scope, 20, "draft")).decision == "stale"


def test_authoritative_present_confirmation_advances_order_without_revision(
    projection: tuple[sqlite3.Connection, SQLiteDraftProjection],
) -> None:
    conn, repository = projection
    scope = DraftScope(100, 219)
    first = repository.apply_realtime(_present(scope, 10, "same"))
    claim = _claim(repository, 30)
    confirmed = repository.apply_snapshot(
        [_present(scope, 20, "same", source=DraftObservationSource.SNAPSHOT)],
        _coverage(1, second=40),
        claim_token=claim,
    )
    assert confirmed.accepted
    assert conn.execute(
        "SELECT text,source_order_at,projection_revision FROM draft_current WHERE dialog_id=219"
    ).fetchone() == ("same", _at(40).timestamp(), first.revision)


def test_partial_snapshot_materializes_omitted_uncertain_scope_before_account_completion(
    projection: tuple[sqlite3.Connection, SQLiteDraftProjection],
) -> None:
    conn, repository = projection
    unresolved = DraftScope(100, 217)
    omitted = DraftScope(100, 218)
    repository.apply_realtime(_present(unresolved, 50, "newer"))
    repository.apply_realtime(
        DraftObservation(
            scope=unresolved,
            disposition=DraftDisposition.TOMBSTONE,
            source=DraftObservationSource.REALTIME,
            observed_at=_at(100),
        )
    )
    repository.apply_realtime(
        DraftObservation(
            scope=omitted,
            disposition=DraftDisposition.TOMBSTONE,
            source=DraftObservationSource.REALTIME,
            observed_at=_at(100),
        )
    )
    due = repository.recovery_due_at()
    assert due is not None
    claim = repository.claim_recovery(now=due)
    assert claim is not None

    partial = repository.apply_snapshot([], _coverage(0, second=40), claim_token=claim)
    assert not partial.accepted
    assert partial.decision == "snapshot_scope_uncertain"
    assert partial.revision is not None
    assert conn.execute("SELECT state,source_order_at FROM draft_current WHERE dialog_id=218").fetchone() == (
        "cleared",
        _at(40).timestamp(),
    )
    assert conn.execute(
        "SELECT 1 FROM draft_order_uncertainty WHERE account_id=? AND dialog_id=?",
        (100, 217),
    ).fetchone() == (1,)
    assert repository.apply_realtime(_present(omitted, 20, "late")).decision == "stale"


def test_partial_snapshot_raises_floor_and_refreshes_matching_cleared_scope(
    projection: tuple[sqlite3.Connection, SQLiteDraftProjection],
) -> None:
    conn, repository = projection
    cleared_scope = DraftScope(100, 220)
    uncertain_scope = DraftScope(100, 221)
    repository.apply_realtime(_present(cleared_scope, 5, "draft"))
    initial_claim = _claim(repository, 10)
    assert repository.apply_snapshot([], _coverage(0, second=10), claim_token=initial_claim).accepted
    repository.apply_realtime(_empty(uncertain_scope, 100))
    repository.apply_realtime(
        DraftObservation(
            scope=uncertain_scope,
            disposition=DraftDisposition.TOMBSTONE,
            source=DraftObservationSource.REALTIME,
            observed_at=_at(101),
        )
    )
    repository.apply_realtime(
        DraftObservation(
            scope=cleared_scope,
            disposition=DraftDisposition.TOMBSTONE,
            source=DraftObservationSource.REALTIME,
            observed_at=_at(102),
        )
    )
    due = repository.recovery_due_at()
    assert due is not None
    claim = repository.claim_recovery(now=due)
    assert claim is not None

    partial = repository.apply_snapshot([], _coverage(0, second=90), claim_token=claim)
    assert not partial.accepted
    assert partial.decision == "snapshot_scope_uncertain"
    assert conn.execute("SELECT state,source_order_at FROM draft_current WHERE dialog_id=220").fetchone() == (
        "cleared",
        _at(90).timestamp(),
    )
    assert (
        conn.execute(
            "SELECT 1 FROM draft_order_uncertainty WHERE account_id=? AND dialog_id=?",
            (100, 220),
        ).fetchone()
        is None
    )
    assert conn.execute("SELECT source_order_floor FROM draft_sync_state WHERE account_id=100").fetchone() == (
        _at(90).timestamp(),
    )
    assert conn.execute(
        "SELECT status,coverage_status,recovery_claimed_at FROM draft_sync_state "
        "JOIN draft_projection_runtime ON draft_projection_runtime.account_id=draft_sync_state.account_id "
        "WHERE draft_sync_state.account_id=100"
    ).fetchone() == ("recovering", "incomplete", claim)
    assert repository.apply_realtime(_present(cleared_scope, 20, "late")).decision == "stale"


def test_partial_snapshot_floor_blocks_unseen_scope_without_creating_content(
    projection: tuple[sqlite3.Connection, SQLiteDraftProjection],
) -> None:
    conn, repository = projection
    uncertain_scope = DraftScope(100, 222)
    unseen_scope = DraftScope(100, 223)
    repository.apply_realtime(_empty(uncertain_scope, 100))
    repository.apply_realtime(
        DraftObservation(
            scope=uncertain_scope,
            disposition=DraftDisposition.TOMBSTONE,
            source=DraftObservationSource.REALTIME,
            observed_at=_at(101),
        )
    )
    due = repository.recovery_due_at()
    assert due is not None
    claim = repository.claim_recovery(now=due)
    assert claim is not None

    partial = repository.apply_snapshot([], _coverage(0, second=40), claim_token=claim)
    assert not partial.accepted
    assert partial.decision == "snapshot_scope_uncertain"
    assert conn.execute("SELECT source_order_floor FROM draft_sync_state WHERE account_id=100").fetchone() == (
        _at(40).timestamp(),
    )
    assert repository.apply_realtime(_present(unseen_scope, 20, "late")).decision == "stale"
    assert conn.execute("SELECT 1 FROM draft_current WHERE dialog_id=223").fetchone() is None


def test_v76_schema_has_order_columns_and_no_snapshot_baseline(
    projection: tuple[sqlite3.Connection, SQLiteDraftProjection],
) -> None:
    conn, _ = projection
    conn.execute(
        "INSERT INTO draft_current(account_id,dialog_id,state,composition_complete,source_kind,"
        "source_observed_at,observation_started_at,observation_completed_at,projection_revision,normalization_version) "
        "VALUES (100,999,'empty',1,'realtime_empty',1,1,1,1,1)"
    )
    assert conn.execute("SELECT source_order_at FROM draft_current WHERE dialog_id=999").fetchone() == (None,)
    assert conn.execute("SELECT source_order_floor FROM draft_sync_state").fetchone() == (None,)
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='draft_order_uncertainty'").fetchone() is not None
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='draft_snapshot_baseline'").fetchone() is None
