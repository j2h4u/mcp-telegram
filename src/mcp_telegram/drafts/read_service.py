"""Local-only draft reads shared with the daemon's MCP boundary."""

import sqlite3
from collections.abc import Mapping
from typing import cast

from mcp_telegram.drafts.contracts import DraftScope
from mcp_telegram.drafts.history import (
    DraftHistoryAccountError,
    DraftHistoryEntry,
    DraftState,
    get_draft_history,
    list_draft_history,
    read_current_draft,
)


def current_draft_output(state: DraftState) -> dict[str, object]:
    return {
        key: value
        for key, value in {
            "presence": state.presence,
            "expected_version": state.expected_version,
            "text": state.text,
            "observed_at": state.observed_at,
            "uncertain": state.uncertain,
        }.items()
        if value is not None
    }


def _history_output(entry: DraftHistoryEntry) -> dict[str, object]:
    snapshot = entry.restore_snapshot
    restorable = entry.presence == "absent" or (snapshot is not None and snapshot.restore_reason is None)
    result: dict[str, object] = {
        "history_id": entry.history_id,
        "presence": entry.presence,
        "archived_at": entry.archived_at,
        "restorable": restorable,
    }
    if entry.text is not None:
        result["text"] = entry.text
    if not restorable:
        result["restore_reason"] = snapshot.restore_reason if snapshot else "restore_snapshot_unavailable"
    return result


def _optional_id(value: object, *, positive: bool) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value == 0 or (positive and value < 0):
        raise ValueError("Use exact nonzero draft IDs.")
    return value


def _read_scope(request: Mapping[str, object], account_id: int) -> tuple[DraftScope, int | None]:
    dialog_id = _optional_id(request.get("dialog_id"), positive=False)
    if dialog_id is None:
        raise ValueError("dialog_id is required.")
    return (
        DraftScope(
            account_id,
            dialog_id,
            _optional_id(request.get("topic_id"), positive=True),
            _optional_id(request.get("subdialog_peer_id"), positive=False),
        ),
        _optional_id(request.get("history_id"), positive=True),
    )


def _same_account(value: object, account_id: int) -> bool:
    return type(value) is int and value == account_id


def _sync_evidence(conn: sqlite3.Connection, account_id: int) -> dict[str, object]:
    row = cast(
        tuple[str, str] | None,
        conn.execute(
            "SELECT status,coverage_status FROM draft_sync_state WHERE account_id=?", (account_id,)
        ).fetchone(),
    )
    return {
        "acquisition_status": "unknown" if row is None else row[0],
        "coverage_status": "unknown" if row is None else row[1],
    }


def read_draft_request(
    conn: sqlite3.Connection, request: Mapping[str, object], *, account_id: int | None
) -> dict[str, object]:
    """Never resolve peers, schedule acquisition or perform Telegram calls."""
    if account_id is None:
        return {"ok": False, "error": "daemon_not_ready", "message": "Account identity is not ready."}
    requested_account = request.get("account_id", account_id)
    if not _same_account(requested_account, account_id):
        return {"ok": False, "error": "account_mismatch", "message": "Use the connected Telegram account."}
    try:
        scope, history_id = _read_scope(request, account_id)
    except ValueError as exc:
        return {"ok": False, "error": "invalid_params", "message": str(exc)}
    try:
        state = read_current_draft(conn, scope)
        history = list_draft_history(conn, scope)
        selected = None if history_id is None else get_draft_history(conn, scope, history_id)
    except DraftHistoryAccountError:
        return {"ok": False, "error": "account_mismatch", "message": "Draft account binding is unavailable."}
    if history_id is not None and selected is None:
        return {
            "ok": False,
            "error": "history_unavailable",
            "message": "That previous state is unavailable or expired. Action: read get_draft for retained history.",
        }
    result: dict[str, object] = {
        "ok": True,
        "current": current_draft_output(state),
        **_sync_evidence(conn, account_id),
        "history": [_history_output(entry) for entry in history],
        "next_action": "Use manage_draft explicitly to change a draft. Review and send manually in Telegram.",
    }
    if selected is not None:
        result["selected_history"] = _history_output(selected)
    return result
