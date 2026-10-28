"""Draft history retains complete observations without becoming desired state."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from typing import cast

import pytest
from telethon.tl import types  # type: ignore[import-untyped]

from helpers import in_memory_sync_db, remove_v90_schema_for_historical_fixture
from mcp_telegram.config import DraftRecoveryConfig
from mcp_telegram.drafts.contracts import DraftObservation, DraftObservationSource, DraftScope, SnapshotCoverage
from mcp_telegram.drafts.history import (
    HISTORY_LIMIT,
    HISTORY_RETENTION_SECONDS,
    DraftHistoryAccountError,
    DraftOperationConflictError,
    DraftRestoreUnavailableError,
    DraftVersionConflictError,
    backup_current_draft,
    finish_draft_operation,
    get_draft_history,
    interrupt_draft_operations,
    list_draft_history,
    prepare_draft_operation,
    prune_draft_history,
    prune_draft_operation_receipts,
    read_current_draft,
    read_draft_operation,
)
from mcp_telegram.drafts.snapshot import decode_restore_snapshot
from mcp_telegram.drafts.sqlite_projection import SQLiteDraftProjection
from mcp_telegram.drafts.telethon_adapter import normalize_update_draft
from mcp_telegram.sync_db import _apply_migration_90
from mcp_telegram.sync_transactions import write_transaction

_SCOPE = DraftScope(100, 200)
_NOW = 1_000


@pytest.fixture()
def store(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[sqlite3.Connection, SQLiteDraftProjection]]:
    monkeypatch.setattr("mcp_telegram.drafts.history.time.time", lambda: float(_NOW))
    conn = in_memory_sync_db()
    projection = SQLiteDraftProjection(conn, DraftRecoveryConfig())
    projection.bind_account(_SCOPE.account_id)
    try:
        yield conn, projection
    finally:
        conn.close()


def _at(value: int) -> datetime:
    return datetime.fromtimestamp(value, UTC)


def _observation(
    text: str,
    order: int,
    *,
    scope: DraftScope = _SCOPE,
    url: str | None = None,
    quote: str | None = None,
) -> DraftObservation:
    draft = types.DraftMessage(
        text,
        _at(order),
        entities=None if url is None else [types.MessageEntityTextUrl(0, 1, url)],
        reply_to=None if quote is None else types.InputReplyToMessage(9, quote_text=quote),
    )
    observation = normalize_update_draft(
        types.UpdateDraftMessage(types.PeerUser(scope.dialog_id), draft, top_msg_id=scope.top_message_id),
        account_id=scope.account_id,
        source=DraftObservationSource.REALTIME,
        observed_at=_at(order),
    )
    assert observation is not None and observation.restore_snapshot is not None
    return observation


def _version(conn: sqlite3.Connection, scope: DraftScope = _SCOPE) -> str:
    version = read_current_draft(conn, scope).expected_version
    assert version is not None
    return version


def _full_snapshot(projection: SQLiteDraftProjection, order: int) -> None:
    claim = projection.claim_mutation_preflight(now=float(order))
    assert claim is not None
    result = projection.apply_snapshot(
        (), SnapshotCoverage(100, True, 0, source_order_at=_at(order)), claim_token=claim
    )
    assert result.accepted


@pytest.mark.parametrize("field", ["url", "quote"])
def test_complete_fingerprint_versions_url_and_quote_only_changes(
    store: tuple[sqlite3.Connection, SQLiteDraftProjection], field: str
) -> None:
    conn, projection = store
    first = _observation("x", 10, url="https://first.example" if field == "url" else None, quote="first")
    second = _observation(
        "x",
        20,
        url="https://second.example" if field == "url" else None,
        quote="second" if field == "quote" else "first",
    )
    projection.apply_realtime(first)
    original = _version(conn)
    projection.apply_realtime(second)
    assert _version(conn) != original
    history = list_draft_history(conn, _SCOPE)
    assert len(history) == 1
    assert history[0].restore_snapshot == first.restore_snapshot
    assert history[0].restore_snapshot is not None
    restored = decode_restore_snapshot(history[0].restore_snapshot)
    assert isinstance(restored, types.DraftMessage)
    assert isinstance(restored.reply_to, types.InputReplyToMessage)
    assert restored.reply_to.quote_text == "first"
    if field == "url":
        assert restored.entities is not None
        assert isinstance(restored.entities[0], types.MessageEntityTextUrl)
        assert restored.entities[0].url == "https://first.example"


def test_date_refresh_keeps_semantic_revision_but_refreshes_full_snapshot(
    store: tuple[sqlite3.Connection, SQLiteDraftProjection],
) -> None:
    conn, projection = store
    projection.apply_realtime(_observation("body", 10))
    original = read_current_draft(conn, _SCOPE)
    refreshed = _observation("body", 20)
    result = projection.apply_realtime(refreshed)
    current = read_current_draft(conn, _SCOPE)
    assert not result.publication_changed
    assert current.revision == original.revision
    assert current.expected_version == original.expected_version
    assert current.restore_snapshot == refreshed.restore_snapshot
    assert current.observed_at == 20
    assert list_draft_history(conn, _SCOPE) == ()


def test_legacy_enrichment_does_not_invent_a_change_or_restore_snapshot(
    store: tuple[sqlite3.Connection, SQLiteDraftProjection],
) -> None:
    conn, projection = store
    full = _observation("retained", 10)
    projection.apply_realtime(replace(full, restore_snapshot=None))
    legacy = read_current_draft(conn, _SCOPE)
    assert legacy.text == "retained" and legacy.restore_snapshot is None and legacy.expected_version is None
    projection.apply_realtime(_observation("retained", 20))
    enriched = read_current_draft(conn, _SCOPE)
    assert enriched.revision == legacy.revision
    assert enriched.restore_snapshot is not None and enriched.expected_version is not None
    assert list_draft_history(conn, _SCOPE) == ()


@pytest.mark.parametrize("clear_source", ["delivered", "snapshot"])
def test_manual_clear_or_send_archives_preimage_and_stays_absent(
    store: tuple[sqlite3.Connection, SQLiteDraftProjection], clear_source: str
) -> None:
    conn, projection = store
    projection.apply_realtime(_observation("manual text", 10))
    if clear_source == "snapshot":
        _full_snapshot(projection, 20)
    else:
        observation = normalize_update_draft(
            types.UpdateDraftMessage(types.PeerUser(200), types.DraftMessageEmpty(_at(20))),
            account_id=100,
            source=DraftObservationSource.REALTIME,
            observed_at=_at(20),
        )
        assert observation is not None
        projection.apply_realtime(observation)
    current = read_current_draft(conn, _SCOPE)
    assert current.presence == "absent"
    assert current.text is None
    assert [entry.text for entry in list_draft_history(conn, _SCOPE)] == ["manual text"]
    projection.apply_realtime(_observation("stale text", 5))
    assert read_current_draft(conn, _SCOPE).presence == "absent"


def test_virtual_absence_requires_complete_coverage_and_versions_bind_scope(
    store: tuple[sqlite3.Connection, SQLiteDraftProjection],
) -> None:
    conn, projection = store
    assert read_current_draft(conn, _SCOPE).presence == "unknown"
    _full_snapshot(projection, 20)
    absent = read_current_draft(conn, _SCOPE)
    assert absent.presence == "absent" and absent.revision == 0 and absent.restore_snapshot is None
    assert absent.expected_version != read_current_draft(conn, DraftScope(100, 201)).expected_version
    assert absent.expected_version != read_current_draft(conn, DraftScope(100, 200, 1)).expected_version
    with write_transaction(conn):
        assert backup_current_draft(conn, _SCOPE, _version(conn)) is None
    assert conn.execute("SELECT COUNT(*) FROM draft_current").fetchone()[0] == 0


def test_prebackup_deduplicates_with_publication_and_failed_attempt_keeps_ten_past_states(
    store: tuple[sqlite3.Connection, SQLiteDraftProjection],
) -> None:
    conn, projection = store
    for order in range(1, HISTORY_LIMIT + 2):
        projection.apply_realtime(_observation(str(order), order))
    assert len(list_draft_history(conn, _SCOPE)) == HISTORY_LIMIT
    with write_transaction(conn):
        first = backup_current_draft(conn, _SCOPE, _version(conn))
        assert first == backup_current_draft(conn, _SCOPE, _version(conn))
    assert len(list_draft_history(conn, _SCOPE)) == HISTORY_LIMIT
    projection.apply_realtime(_observation("next", 20))
    history = list_draft_history(conn, _SCOPE)
    assert len(history) == HISTORY_LIMIT and history[0].history_id == first
    assert conn.execute("SELECT COUNT(*) FROM draft_versions").fetchone()[0] == HISTORY_LIMIT


def test_history_expires_without_changing_current_draft_or_reading_other_scope(
    store: tuple[sqlite3.Connection, SQLiteDraftProjection],
) -> None:
    conn, projection = store
    projection.apply_realtime(_observation("before", 10))
    projection.apply_realtime(_observation("current", 20))
    entry = list_draft_history(conn, _SCOPE)[0]
    assert get_draft_history(conn, DraftScope(100, 201), entry.history_id) is None
    later = _NOW + HISTORY_RETENTION_SECONDS
    assert list_draft_history(conn, _SCOPE, now=later) == ()
    assert get_draft_history(conn, _SCOPE, entry.history_id, now=later) is None
    with write_transaction(conn):
        assert prune_draft_history(conn, now=later, limit=1) == 1
    assert read_current_draft(conn, _SCOPE).text == "current"
    assert read_current_draft(conn, _SCOPE).restore_snapshot is not None


def test_account_switch_fences_retained_current_and_history(
    store: tuple[sqlite3.Connection, SQLiteDraftProjection],
) -> None:
    conn, projection = store
    projection.apply_realtime(_observation("one", 10))
    projection.apply_realtime(_observation("two", 20))
    projection.bind_account(101)
    with pytest.raises(DraftHistoryAccountError):
        read_current_draft(conn, _SCOPE)
    with pytest.raises(DraftHistoryAccountError):
        list_draft_history(conn, _SCOPE)
    projection.bind_account(100)
    assert read_current_draft(conn, _SCOPE).text == "two"
    assert list_draft_history(conn, _SCOPE)[0].text == "one"


def test_prepare_checks_version_and_recovers_duplicate_receipt_across_interruption(
    store: tuple[sqlite3.Connection, SQLiteDraftProjection],
) -> None:
    conn, projection = store
    projection.apply_realtime(_observation("one", 10))
    original = _version(conn)
    with write_transaction(conn):
        receipt = prepare_draft_operation(conn, _SCOPE, "operation", "fingerprint", original)
        assert receipt.outcome == "pending"
        assert receipt == prepare_draft_operation(conn, _SCOPE, "operation", "fingerprint", "old-version")
        with pytest.raises(DraftOperationConflictError):
            prepare_draft_operation(conn, _SCOPE, "operation", "different", original)
        assert interrupt_draft_operations(conn, 100) == 1
    interrupted = read_draft_operation(conn, 100, "operation")
    assert interrupted is not None and interrupted.outcome == "unknown"
    projection.apply_realtime(_observation("manual change", 20))
    with write_transaction(conn):
        with pytest.raises(DraftVersionConflictError):
            prepare_draft_operation(conn, _SCOPE, "stale", "fingerprint", original)
        newer = prepare_draft_operation(conn, _SCOPE, "new", "fingerprint", _version(conn))
    assert newer.outcome == "pending"
    assert read_draft_operation(conn, 100, "stale") is None


def test_noop_receipt_avoids_backup_and_terminal_receipt_retention_is_bounded(
    store: tuple[sqlite3.Connection, SQLiteDraftProjection],
) -> None:
    conn, projection = store
    projection.apply_realtime(_observation("same", 10))
    with write_transaction(conn):
        noop = prepare_draft_operation(conn, _SCOPE, "noop", "same", _version(conn), backup=False)
        assert noop.backup_history_id is None
        result = {"text": "\x01" * 65_536}
        finished = finish_draft_operation(conn, 100, "noop", "unchanged", result)
        assert finished.result == result
        prepare_draft_operation(conn, _SCOPE, "pending", "same", _version(conn), backup=False)
        assert prune_draft_operation_receipts(conn, now=_NOW + HISTORY_RETENTION_SECONDS) == 1
    assert read_draft_operation(conn, 100, "noop") is None
    assert read_draft_operation(conn, 100, "pending") is not None
    assert conn.execute("SELECT COUNT(*) FROM draft_versions").fetchone()[0] == 0


def test_prebackup_requires_owned_transaction_and_recoverable_current(
    store: tuple[sqlite3.Connection, SQLiteDraftProjection],
) -> None:
    conn, projection = store
    observation = _observation("opaque", 10)
    assert observation.restore_snapshot is not None
    projection.apply_realtime(
        replace(observation, restore_snapshot=replace(observation.restore_snapshot, restore_reason="unsupported"))
    )
    with pytest.raises(RuntimeError, match="owned write_transaction"):
        backup_current_draft(conn, _SCOPE, _version(conn))
    with write_transaction(conn), pytest.raises(DraftRestoreUnavailableError):
        backup_current_draft(conn, _SCOPE, _version(conn))


def test_mutation_preflight_does_not_displace_active_snapshot_claim(
    store: tuple[sqlite3.Connection, SQLiteDraftProjection],
) -> None:
    conn, projection = store
    first = projection.claim_mutation_preflight(now=100)
    assert first == 100
    before = cast(tuple[object, ...] | None, conn.execute("SELECT * FROM draft_projection_runtime").fetchone())
    assert projection.claim_mutation_preflight(now=200) is None
    assert conn.execute("SELECT * FROM draft_projection_runtime").fetchone() == before
    assert projection.apply_snapshot(
        (), SnapshotCoverage(100, True, 0, source_order_at=_at(100)), claim_token=100
    ).accepted


def test_migration_90_keeps_legacy_fact_without_inventing_restore_payload(
    store: tuple[sqlite3.Connection, SQLiteDraftProjection],
) -> None:
    conn, projection = store
    projection.apply_realtime(replace(_observation("old local knowledge", 10), restore_snapshot=None))
    with write_transaction(conn):
        remove_v90_schema_for_historical_fixture(conn)
        conn.execute("DELETE FROM schema_version WHERE version=90")
    conn.execute("PRAGMA query_only=OFF")
    assert _apply_migration_90(conn, 89) == 90
    assert _apply_migration_90(conn, 90) == 90
    row = cast(
        tuple[str, None, None],
        conn.execute("SELECT text,restore_payload,semantic_fingerprint FROM draft_current").fetchone(),
    )
    assert row == ("old local knowledge", None, None)
    assert conn.execute("SELECT COUNT(*) FROM draft_versions").fetchone()[0] == 0
