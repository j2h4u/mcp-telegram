"""Focused state-machine tests for the durable current draft projection."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from mcp_telegram.config import DraftRecoveryConfig
from mcp_telegram.drafts.contracts import (
    DraftComposition,
    DraftDisposition,
    DraftObservation,
    DraftObservationSource,
    DraftScope,
    SnapshotCoverage,
)
from mcp_telegram.drafts.sqlite_projection import DraftAccountFenceError, SQLiteDraftProjection
from mcp_telegram.sync_db import _apply_migration_74, _apply_migration_75, ensure_sync_schema


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
    return datetime(2026, 9, 22, 0, 0, second, tzinfo=UTC)


def _present(scope: DraftScope, second: int, text: str, *, source: DraftObservationSource) -> DraftObservation:
    return DraftObservation(
        scope=scope,
        disposition=DraftDisposition.PRESENT,
        source=source,
        observed_at=_at(second),
        composition=DraftComposition(text=text, date=None),
    )


def _empty(scope: DraftScope, second: int, *, source: DraftObservationSource) -> DraftObservation:
    return DraftObservation(
        scope=scope,
        disposition=DraftDisposition.TOMBSTONE,
        source=source,
        observed_at=_at(second),
    )


def _claim(repository: SQLiteDraftProjection, second: int) -> int:
    repository.mark_recovery_needed(reason="test_recovery", observed_at=_at(second))
    claim_token = repository.claim_recovery(now=_at(second).timestamp())
    assert claim_token is not None
    return claim_token


def _current(conn: sqlite3.Connection, scope: DraftScope) -> tuple[object, ...]:
    row = _fetchone(
        conn,
        "SELECT state,text,source_kind,source_observed_at,projection_revision,entities_json "
        "FROM draft_current WHERE account_id=? AND dialog_id=? AND top_message_id=? AND subdialog_peer_id=?",
        (scope.account_id, scope.dialog_id, scope.top_message_id or 0, scope.subdialog_peer_id or 0),
    )
    assert row is not None
    return row


def _fetchone(
    conn: sqlite3.Connection, statement: str, parameters: tuple[object, ...] = ()
) -> tuple[object, ...] | None:
    return cast(tuple[object, ...] | None, conn.execute(statement, parameters).fetchone())


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = cast(list[tuple[object, ...]], conn.execute(f"PRAGMA table_info({table})").fetchall())
    return {str(row[1]) for row in rows}


def test_realtime_orders_duplicates_and_equal_conflicts(
    projection: tuple[sqlite3.Connection, SQLiteDraftProjection],
) -> None:
    conn, repository = projection
    scope = DraftScope(100, 200)
    first = _present(scope, 1, "first", source=DraftObservationSource.REALTIME)
    applied = repository.apply_realtime(first)
    assert applied.accepted and applied.revision is not None
    duplicate = repository.apply_realtime(first)
    assert duplicate.accepted and duplicate.revision == applied.revision
    assert _current(conn, scope)[4] == applied.revision
    older = repository.apply_realtime(_present(scope, 0, "older", source=DraftObservationSource.REALTIME))
    assert not older.accepted
    assert _current(conn, scope)[1] == "first"
    conflict = repository.apply_realtime(_present(scope, 1, "different", source=DraftObservationSource.REALTIME))
    assert not conflict.accepted and conflict.ambiguous
    assert _current(conn, scope)[1] == "first"
    assert repository.recovery_due_at() is not None


def test_delayed_snapshot_cannot_overwrite_newer_realtime_clear(
    projection: tuple[sqlite3.Connection, SQLiteDraftProjection],
) -> None:
    conn, repository = projection
    scope = DraftScope(100, 200, top_message_id=7)
    repository.apply_realtime(_present(scope, 1, "old", source=DraftObservationSource.REALTIME))
    claim_token = _claim(repository, 2)
    baselines = repository.snapshot_baselines(100)
    repository.apply_realtime(_empty(scope, 2, source=DraftObservationSource.REALTIME))
    result = repository.apply_snapshot(
        [_present(scope, 1, "snapshot-old", source=DraftObservationSource.SNAPSHOT)],
        SnapshotCoverage(account_id=100, response_complete=True, update_count=1),
        baselines,
        claim_token=claim_token,
    )
    assert result.accepted
    state, text, source_kind, *_ = _current(conn, scope)
    assert (state, text, source_kind) == ("empty", None, "realtime_empty")


def test_authoritative_absence_creates_bodyless_cleared_tombstone(
    projection: tuple[sqlite3.Connection, SQLiteDraftProjection],
) -> None:
    conn, repository = projection
    scope = DraftScope(100, 200)
    repository.apply_realtime(_present(scope, 1, "body", source=DraftObservationSource.REALTIME))
    claim_token = _claim(repository, 2)
    result = repository.apply_snapshot(
        [],
        SnapshotCoverage(account_id=100, response_complete=True, update_count=0),
        repository.snapshot_baselines(100),
        claim_token=claim_token,
    )
    assert result.accepted
    state, text, source_kind, _, _, entities = _current(conn, scope)
    assert (state, text, source_kind, entities) == ("cleared", None, "snapshot_absence", None)


def test_identical_authoritative_snapshots_do_not_bump_revision_for_observation_time(
    projection: tuple[sqlite3.Connection, SQLiteDraftProjection],
) -> None:
    conn, repository = projection
    scope = DraftScope(100, 200)
    first_claim_token = _claim(repository, 1)
    first = repository.apply_snapshot(
        [_present(scope, 1, "body", source=DraftObservationSource.SNAPSHOT)],
        SnapshotCoverage(account_id=100, response_complete=True, update_count=1),
        repository.snapshot_baselines(100),
        claim_token=first_claim_token,
    )
    assert first.revision is not None
    second_claim_token = _claim(repository, 2)
    second = repository.apply_snapshot(
        [_present(scope, 2, "body", source=DraftObservationSource.SNAPSHOT)],
        SnapshotCoverage(account_id=100, response_complete=True, update_count=1),
        repository.snapshot_baselines(100),
        claim_token=second_claim_token,
    )

    assert second.revision is None
    assert _current(conn, scope)[4] == first.revision


def test_realtime_empty_tombstone_uses_observation_ordering(
    projection: tuple[sqlite3.Connection, SQLiteDraftProjection],
) -> None:
    conn, repository = projection
    scope = DraftScope(100, 200)
    repository.apply_realtime(_present(scope, 1, "body", source=DraftObservationSource.REALTIME))

    result = repository.apply_realtime(_empty(scope, 2, source=DraftObservationSource.REALTIME))

    assert result.accepted
    assert _current(conn, scope)[0] == "empty"


def test_snapshot_does_not_clear_a_later_recovery_signal(
    projection: tuple[sqlite3.Connection, SQLiteDraftProjection],
) -> None:
    conn, repository = projection
    scope = DraftScope(100, 200)
    repository.apply_realtime(_present(scope, 1, "body", source=DraftObservationSource.REALTIME))
    claim_token = _claim(repository, 2)
    baselines = repository.snapshot_baselines(100)
    repository.mark_recovery_needed(reason="reconnect_observed", observed_at=_at(3))

    result = repository.apply_snapshot(
        [_present(scope, 1, "body", source=DraftObservationSource.SNAPSHOT)],
        SnapshotCoverage(account_id=100, response_complete=True, update_count=1),
        baselines,
        claim_token=claim_token,
    )

    assert result.accepted
    assert conn.execute(
        "SELECT recovery_due_at,recovery_claimed_at FROM draft_projection_runtime WHERE singleton=1"
    ).fetchone() == (int(_at(3).timestamp()), None)
    assert conn.execute(
        "SELECT status,coverage_status,reason,observation_completed_at FROM draft_sync_state WHERE account_id=100"
    ).fetchone() == ("recovery_needed", "unknown", "reconnect_observed", int(_at(3).timestamp()))


def test_snapshot_with_identical_realtime_content_does_not_bump_revision_for_source_kind(
    projection: tuple[sqlite3.Connection, SQLiteDraftProjection],
) -> None:
    conn, repository = projection
    scope = DraftScope(100, 200)
    realtime = repository.apply_realtime(_present(scope, 1, "body", source=DraftObservationSource.REALTIME))
    assert realtime.revision is not None
    claim_token = _claim(repository, 2)

    snapshot = repository.apply_snapshot(
        [_present(scope, 2, "body", source=DraftObservationSource.SNAPSHOT)],
        SnapshotCoverage(account_id=100, response_complete=True, update_count=1),
        repository.snapshot_baselines(100),
        claim_token=claim_token,
    )

    assert snapshot.revision is None
    assert _current(conn, scope)[4] == realtime.revision
    assert _current(conn, scope)[2] == "realtime_present"


def test_failed_snapshot_never_establishes_absence(
    projection: tuple[sqlite3.Connection, SQLiteDraftProjection],
) -> None:
    conn, repository = projection
    scope = DraftScope(100, 200)
    repository.apply_realtime(_present(scope, 1, "body", source=DraftObservationSource.REALTIME))
    claim_token = _claim(repository, 2)
    result = repository.apply_snapshot(
        [],
        SnapshotCoverage(account_id=100, response_complete=False, update_count=0),
        repository.snapshot_baselines(100),
        claim_token=claim_token,
    )
    assert not result.accepted
    assert _current(conn, scope)[0] == "present"
    assert conn.execute("SELECT coverage_status FROM draft_sync_state WHERE account_id=100").fetchone() == ("unknown",)


def test_account_fence_rejects_unbound_observation(
    projection: tuple[sqlite3.Connection, SQLiteDraftProjection],
) -> None:
    _, repository = projection
    with pytest.raises(DraftAccountFenceError):
        repository.apply_realtime(_present(DraftScope(101, 200), 1, "body", source=DraftObservationSource.REALTIME))


def test_optional_scope_zero_sentinel_and_constraints(
    projection: tuple[sqlite3.Connection, SQLiteDraftProjection],
) -> None:
    conn, repository = projection
    scope = DraftScope(100, -200, subdialog_peer_id=-300)
    repository.apply_realtime(_present(scope, 1, "body", source=DraftObservationSource.REALTIME))
    assert conn.execute(
        "SELECT top_message_id,subdialog_peer_id FROM draft_current WHERE account_id=100 AND dialog_id=-200"
    ).fetchone() == (0, -300)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO draft_current(account_id,dialog_id,state,composition_complete,source_kind,source_observed_at,"
            "observation_started_at,observation_completed_at,projection_revision,normalization_version) "
            "VALUES (100,0,'empty',1,'realtime_empty',0,0,0,0,1)"
        )


def test_v74_upgrade_removes_previews_without_promoting_them(tmp_path: Path) -> None:
    database = tmp_path / "sync.db"
    ensure_sync_schema(database)
    conn = sqlite3.connect(database)
    try:
        conn.execute("ALTER TABLE dialogs ADD COLUMN draft_text TEXT")
        conn.execute("ALTER TABLE dialog_directory_staging ADD COLUMN draft_text TEXT")
        conn.execute("INSERT INTO dialogs(dialog_id,draft_text) VALUES (200,'old truncated preview')")
        conn.execute("DELETE FROM schema_version WHERE version=74")
        conn.commit()
        assert _apply_migration_74(conn, 73) == 74
        assert "draft_text" not in _table_columns(conn, "dialogs")
        assert "draft_text" not in _table_columns(conn, "dialog_directory_staging")
        assert conn.execute("SELECT COUNT(*) FROM draft_current").fetchone() == (0,)
        assert _apply_migration_74(conn, 74) == 74
    finally:
        conn.close()


def test_v75_adds_durable_recovery_backoff_and_removes_key_prefix_index(tmp_path: Path) -> None:
    database = tmp_path / "sync.db"
    ensure_sync_schema(database)
    conn = sqlite3.connect(database)
    try:
        conn.execute("ALTER TABLE draft_projection_runtime DROP COLUMN recovery_failure_count")
        conn.execute(
            "CREATE INDEX idx_draft_current_account_dialog "
            "ON draft_current(account_id,dialog_id,top_message_id,subdialog_peer_id)"
        )
        conn.execute("DELETE FROM schema_version WHERE version=75")
        conn.commit()

        assert _apply_migration_75(conn, 74) == 75
        assert "recovery_failure_count" in _table_columns(conn, "draft_projection_runtime")
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='idx_draft_current_account_dialog'"
        ).fetchone() == (0,)
    finally:
        conn.close()


def test_recovery_rearm_uses_bounded_durable_backoff(
    projection: tuple[sqlite3.Connection, SQLiteDraftProjection],
) -> None:
    conn, repository = projection

    repository.mark_recovery_needed(reason="test_recovery", observed_at=datetime.fromtimestamp(100, UTC))
    claim_token = repository.claim_recovery(now=100)
    assert claim_token == 100
    assert repository.rearm_recovery(reason="snapshot_fetch_failed", now=100, claim_token=claim_token)
    assert conn.execute(
        "SELECT recovery_due_at,recovery_failure_count FROM draft_projection_runtime WHERE singleton=1"
    ).fetchone() == (101, 1)
    assert conn.execute("SELECT observation_completed_at FROM draft_sync_state WHERE account_id=100").fetchone() == (
        100,
    )
    claim_token = repository.claim_recovery(now=101)
    assert claim_token == 101
    assert repository.rearm_recovery(reason="snapshot_fetch_failed", now=101, claim_token=claim_token)
    assert conn.execute(
        "SELECT recovery_due_at,recovery_failure_count FROM draft_projection_runtime WHERE singleton=1"
    ).fetchone() == (103, 2)
    for now in (103, 107, 115, 131, 163):
        claim_token = repository.claim_recovery(now=now)
        assert claim_token == now
        assert repository.rearm_recovery(reason="snapshot_fetch_failed", now=now, claim_token=claim_token)
    assert conn.execute(
        "SELECT recovery_due_at,recovery_failure_count FROM draft_projection_runtime WHERE singleton=1"
    ).fetchone() == (223, 6)
