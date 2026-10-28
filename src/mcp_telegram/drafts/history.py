"""Account-scoped draft preimages and finite command receipts in sync.db.

Current facts never expire. Historical preimages and settled command receipts
last seven days; a scope retains at most ten displaced states. Receipts are not
a work queue and never authorize a remote retry.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast

from mcp_telegram.drafts.contracts import DraftScope, RestoreSnapshot
from mcp_telegram.sync_transactions import require_write_transaction

HISTORY_LIMIT = 10
HISTORY_RETENTION_SECONDS = 7 * 86_400
_RESULT_MAX_BYTES = 512 * 1024
_MAX_PRUNE_BATCH = 1_000
_SCOPE_WHERE = "account_id=? AND dialog_id=? AND top_message_id=? AND subdialog_peer_id=?"


class DraftVersionConflictError(ValueError):
    """The caller's observed draft no longer matches the local preflight."""


class DraftRestoreUnavailableError(ValueError):
    """A complete recoverable preimage is unavailable."""


class DraftOperationConflictError(ValueError):
    """A caller operation key was reused for a different command."""


class DraftHistoryAccountError(ValueError):
    """A read or write targets an account outside the active draft binding."""


@dataclass(frozen=True, slots=True)
class DraftState:
    scope: DraftScope
    presence: Literal["present", "absent", "unknown"]
    revision: int | None
    expected_version: str | None
    restore_snapshot: RestoreSnapshot | None
    text: str | None
    observed_at: int | None
    uncertain: bool


@dataclass(frozen=True, slots=True)
class DraftHistoryEntry:
    history_id: int
    scope: DraftScope
    revision: int
    presence: Literal["present", "absent"]
    restore_snapshot: RestoreSnapshot | None
    text: str | None
    archived_at: int


@dataclass(frozen=True, slots=True)
class DraftOperationReceipt:
    scope: DraftScope
    operation_key: str
    request_fingerprint: str
    outcome: str
    backup_history_id: int | None
    result: dict[str, object]
    updated_at: int


def _scope_values(scope: DraftScope) -> tuple[int, int, int, int]:
    return scope.account_id, scope.dialog_id, scope.top_message_id or 0, scope.subdialog_peer_id or 0


def _now(value: int | None) -> int:
    if value is None:
        return int(time.time())
    if type(value) is not int or value < 0:
        raise ValueError("now must be a non-negative integer")
    return value


def _one(conn: sqlite3.Connection, sql: str, values: tuple[object, ...]) -> dict[str, object] | None:
    cursor = conn.execute(sql, values)
    row = cast(tuple[object, ...] | None, cursor.fetchone())
    if row is None:
        return None
    assert cursor.description is not None
    return dict(zip((column[0] for column in cursor.description), row, strict=True))


def _require_account(conn: sqlite3.Connection, account_id: int) -> None:
    row = _one(conn, "SELECT account_id FROM draft_projection_runtime WHERE singleton=1", ())
    if account_id <= 0 or row is None or row["account_id"] != account_id:
        raise DraftHistoryAccountError("draft account does not match the active account")


def _snapshot(row: Mapping[str, object]) -> RestoreSnapshot | None:
    payload = row.get("restore_payload")
    fingerprint = row.get("semantic_fingerprint")
    if not isinstance(payload, bytes) or not isinstance(fingerprint, str):
        return None
    return RestoreSnapshot(payload, fingerprint, cast(str | None, row.get("restore_reason")))


def _version(scope: DraftScope, revision: int, fingerprint: str) -> str:
    identity = ":".join(str(value) for value in (*_scope_values(scope), revision))
    return "draft-v1-" + hashlib.sha256(f"{identity}:{fingerprint}".encode()).hexdigest()


def _scope_uncertain(conn: sqlite3.Connection, scope: DraftScope) -> bool:
    return (
        conn.execute(f"SELECT 1 FROM draft_order_uncertainty WHERE {_SCOPE_WHERE}", _scope_values(scope)).fetchone()
        is not None
    )


def read_current_draft(conn: sqlite3.Connection, scope: DraftScope) -> DraftState:
    """Read retained facts; complete account coverage can establish virtual absence."""
    _require_account(conn, scope.account_id)
    row = _one(conn, f"SELECT * FROM draft_current WHERE {_SCOPE_WHERE}", _scope_values(scope))
    uncertain = _scope_uncertain(conn, scope)
    if row is None:
        return _unobserved_scope_state(conn, scope, uncertain=uncertain)
    return _current_row_state(scope, row, uncertain=uncertain)


def _unobserved_scope_state(conn: sqlite3.Connection, scope: DraftScope, *, uncertain: bool) -> DraftState:
    coverage = _one(conn, "SELECT * FROM draft_sync_state WHERE account_id=?", (scope.account_id,))
    known = (
        not uncertain
        and coverage is not None
        and coverage["coverage_status"] == "complete"
        and coverage["status"] == "ready"
    )
    return DraftState(
        scope,
        "absent" if known else "unknown",
        0 if known else None,
        _version(scope, 0, "absent") if known else None,
        None,
        None,
        cast(int | None, coverage["observation_completed_at"]) if coverage is not None else None,
        uncertain,
    )


def _current_row_state(scope: DraftScope, row: Mapping[str, object], *, uncertain: bool) -> DraftState:
    present = row["state"] == "present"
    revision = cast(int, row["projection_revision"])
    snapshot = _snapshot(row)
    fingerprint = snapshot.semantic_fingerprint if present and snapshot is not None else "absent"
    version_known = not uncertain and (not present or snapshot is not None)
    return DraftState(
        scope,
        "present" if present else "absent",
        revision,
        _version(scope, revision, fingerprint) if version_known else None,
        snapshot,
        cast(str | None, row["text"]),
        cast(int, row["source_observed_at"]),
        uncertain,
    )


def _history_entry(scope: DraftScope, row: Mapping[str, object]) -> DraftHistoryEntry:
    return DraftHistoryEntry(
        cast(int, row["history_id"]),
        scope,
        cast(int, row["source_revision"]),
        "present" if row["state"] == "present" else "absent",
        _snapshot(row),
        cast(str | None, row["text"]),
        cast(int, row["archived_at"]),
    )


def list_draft_history(
    conn: sqlite3.Connection, scope: DraftScope, *, now: int | None = None, limit: int = HISTORY_LIMIT
) -> tuple[DraftHistoryEntry, ...]:
    """List only unexpired displaced states, not a prepared copy of the current state."""
    _require_account(conn, scope.account_id)
    if type(limit) is not int or not 1 <= limit <= HISTORY_LIMIT:
        raise ValueError("history limit must be between 1 and 10")
    cursor = conn.execute(
        f"SELECT * FROM draft_versions WHERE {_SCOPE_WHERE} AND archived_at>? "
        f"AND source_revision!=COALESCE((SELECT projection_revision FROM draft_current WHERE {_SCOPE_WHERE}),-1) "
        "ORDER BY history_id DESC LIMIT ?",
        (*_scope_values(scope), _now(now) - HISTORY_RETENTION_SECONDS, *_scope_values(scope), limit),
    )
    assert cursor.description is not None
    columns = tuple(column[0] for column in cursor.description)
    return tuple(
        _history_entry(scope, dict(zip(columns, row, strict=True))) for row in cast(list[tuple[object, ...]], cursor)
    )


def get_draft_history(
    conn: sqlite3.Connection, scope: DraftScope, history_id: int, *, now: int | None = None
) -> DraftHistoryEntry | None:
    _require_account(conn, scope.account_id)
    row = _one(
        conn,
        f"SELECT * FROM draft_versions WHERE {_SCOPE_WHERE} AND history_id=? AND archived_at>?",
        (*_scope_values(scope), history_id, _now(now) - HISTORY_RETENTION_SECONDS),
    )
    return None if row is None else _history_entry(scope, row)


def archive_current_draft(conn: sqlite3.Connection, scope: DraftScope, *, now: int | None = None) -> int | None:
    """Copy the current preimage once per semantic revision, in its publication transaction."""
    require_write_transaction(conn)
    _require_account(conn, scope.account_id)
    row = _one(conn, f"SELECT * FROM draft_current WHERE {_SCOPE_WHERE}", _scope_values(scope))
    if row is None:
        return None
    cursor = conn.execute(
        "INSERT INTO draft_versions(account_id,dialog_id,top_message_id,subdialog_peer_id,source_revision,"
        "state,text,restore_payload,semantic_fingerprint,restore_reason,source_observed_at,source_order_at,archived_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(account_id,dialog_id,top_message_id,subdialog_peer_id,source_revision) "
        "DO UPDATE SET archived_at=excluded.archived_at RETURNING history_id",
        (
            *_scope_values(scope),
            row["projection_revision"],
            row["state"],
            row["text"],
            row["restore_payload"],
            row["semantic_fingerprint"],
            row["restore_reason"],
            row["source_observed_at"],
            row["source_order_at"],
            _now(now),
        ),
    )
    history = cast(tuple[int] | None, cursor.fetchone())
    assert history is not None
    trim_draft_history(conn, scope)
    return history[0]


def trim_draft_history(conn: sqlite3.Connection, scope: DraftScope) -> None:
    """Keep ten displaced states; a prepared current preimage consumes no history slot."""
    require_write_transaction(conn)
    conn.execute(
        "DELETE FROM draft_versions WHERE history_id IN ("
        f"SELECT history_id FROM draft_versions WHERE {_SCOPE_WHERE} "
        f"AND source_revision!=COALESCE((SELECT projection_revision FROM draft_current WHERE {_SCOPE_WHERE}),-1) "
        "ORDER BY history_id DESC LIMIT -1 OFFSET ?)",
        (*_scope_values(scope), *_scope_values(scope), HISTORY_LIMIT),
    )


def backup_current_draft(
    conn: sqlite3.Connection, scope: DraftScope, expected_version: str, *, now: int | None = None
) -> int | None:
    """Check the latest committed revision and retain its full preimage before dispatch."""
    require_write_transaction(conn)
    current = read_current_draft(conn, scope)
    if current.expected_version is None or expected_version != current.expected_version:
        raise DraftVersionConflictError("draft changed or its current version is unknown")
    if current.presence == "present" and (
        current.restore_snapshot is None or current.restore_snapshot.restore_reason is not None
    ):
        raise DraftRestoreUnavailableError("current draft cannot be fully restored")
    return archive_current_draft(conn, scope, now=now)


def _bounded_text(value: str, name: str, limit: int) -> None:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ValueError(f"{name} must be a non-empty bounded string")


def _receipt(row: Mapping[str, object]) -> DraftOperationReceipt:
    result = cast(object, json.loads(cast(str, row["result_json"])))
    if not isinstance(result, dict):
        raise RuntimeError("invalid retained draft operation result")
    return DraftOperationReceipt(
        DraftScope(
            cast(int, row["account_id"]),
            cast(int, row["dialog_id"]),
            cast(int, row["top_message_id"]) or None,
            cast(int, row["subdialog_peer_id"]) or None,
        ),
        cast(str, row["operation_key"]),
        cast(str, row["request_fingerprint"]),
        cast(str, row["outcome"]),
        cast(int | None, row["backup_history_id"]),
        cast(dict[str, object], result),
        cast(int, row["updated_at"]),
    )


def read_draft_operation(conn: sqlite3.Connection, account_id: int, operation_key: str) -> DraftOperationReceipt | None:
    _require_account(conn, account_id)
    row = _one(
        conn,
        "SELECT * FROM draft_operation_receipts WHERE account_id=? AND operation_key=?",
        (account_id, operation_key),
    )
    return None if row is None else _receipt(row)


def prepare_draft_operation(  # noqa: PLR0913 - atomic caller identity, content and version check
    conn: sqlite3.Connection,
    scope: DraftScope,
    operation_key: str,
    request_fingerprint: str,
    expected_version: str,
    *,
    now: int | None = None,
    backup: bool = True,
) -> DraftOperationReceipt:
    require_write_transaction(conn)
    _bounded_text(operation_key, "operation_key", 128)
    _bounded_text(request_fingerprint, "request_fingerprint", 256)
    previous = read_draft_operation(conn, scope.account_id, operation_key)
    if previous is not None:
        if previous.scope != scope or previous.request_fingerprint != request_fingerprint:
            raise DraftOperationConflictError("operation key belongs to a different draft command")
        return previous
    timestamp = _now(now)
    if backup:
        backup_id = backup_current_draft(conn, scope, expected_version, now=timestamp)
    else:
        current = read_current_draft(conn, scope)
        if current.expected_version is None or expected_version != current.expected_version:
            raise DraftVersionConflictError("draft changed or its current version is unknown")
        backup_id = None
    conn.execute(
        "INSERT INTO draft_operation_receipts(account_id,dialog_id,top_message_id,subdialog_peer_id,operation_key,"
        "request_fingerprint,outcome,backup_history_id,created_at,updated_at) VALUES (?,?,?,?,?,?,'pending',?,?,?)",
        (*_scope_values(scope), operation_key, request_fingerprint, backup_id, timestamp, timestamp),
    )
    receipt = read_draft_operation(conn, scope.account_id, operation_key)
    assert receipt is not None
    return receipt


def finish_draft_operation(  # noqa: PLR0913 - explicit receipt identity and result
    conn: sqlite3.Connection,
    account_id: int,
    operation_key: str,
    outcome: str,
    result: Mapping[str, object],
    *,
    now: int | None = None,
) -> DraftOperationReceipt:
    require_write_transaction(conn)
    _bounded_text(outcome, "outcome", 64)
    if outcome == "pending":
        raise ValueError("a finished draft operation cannot be pending")
    encoded = json.dumps(dict(result), ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)
    if len(encoded.encode()) > _RESULT_MAX_BYTES:
        raise ValueError("draft operation result exceeds its storage bound")
    previous = read_draft_operation(conn, account_id, operation_key)
    if previous is None:
        raise ValueError("draft operation was not prepared")
    if previous.outcome not in {"pending", "unknown"}:
        if previous.outcome == outcome and previous.result == result:
            return previous
        raise DraftOperationConflictError("draft operation already has a final result")
    conn.execute(
        "UPDATE draft_operation_receipts SET outcome=?,result_json=?,updated_at=? "
        "WHERE account_id=? AND operation_key=?",
        (outcome, encoded, _now(now), account_id, operation_key),
    )
    receipt = read_draft_operation(conn, account_id, operation_key)
    assert receipt is not None
    return receipt


def interrupt_draft_operations(conn: sqlite3.Connection, account_id: int, *, now: int | None = None) -> int:
    """At startup, expose interrupted commands as unknown without replaying or scope locks."""
    require_write_transaction(conn)
    _require_account(conn, account_id)
    return conn.execute(
        "UPDATE draft_operation_receipts SET outcome='unknown',updated_at=? WHERE account_id=? AND outcome='pending'",
        (_now(now), account_id),
    ).rowcount


def _prune_limit(limit: int) -> None:
    if type(limit) is not int or not 1 <= limit <= _MAX_PRUNE_BATCH:
        raise ValueError("prune limit must be between 1 and 1000")


def prune_draft_history(conn: sqlite3.Connection, *, now: int | None = None, limit: int = 100) -> int:
    require_write_transaction(conn)
    _prune_limit(limit)
    return conn.execute(
        "DELETE FROM draft_versions WHERE history_id IN ("
        "SELECT history_id FROM draft_versions WHERE archived_at<=? ORDER BY archived_at,history_id LIMIT ?)",
        (_now(now) - HISTORY_RETENTION_SECONDS, limit),
    ).rowcount


def prune_draft_operation_receipts(conn: sqlite3.Connection, *, now: int | None = None, limit: int = 100) -> int:
    """Bound settled replay receipts; an in-progress command is never pruned."""
    require_write_transaction(conn)
    _prune_limit(limit)
    return conn.execute(
        "DELETE FROM draft_operation_receipts WHERE (account_id,operation_key) IN ("
        "SELECT account_id,operation_key FROM draft_operation_receipts WHERE outcome!='pending' AND updated_at<=? "
        "ORDER BY updated_at,account_id,operation_key LIMIT ?)",
        (_now(now) - HISTORY_RETENTION_SECONDS, limit),
    ).rowcount
