"""Local-only draft reads shared with the daemon's MCP boundary."""

import json
import sqlite3
from collections.abc import Mapping
from typing import cast

from mcp_telegram.budget import MESSAGE_RESPONSE_MAX_BYTES
from mcp_telegram.drafts.contracts import DraftScope
from mcp_telegram.drafts.history import (
    DraftHistoryAccountError,
    DraftHistoryEntry,
    DraftState,
    get_draft_history,
    list_draft_history,
    read_current_draft,
)
from mcp_telegram.rich_content import decode_rich_message, rich_media_reference_gaps


def current_draft_output(state: DraftState) -> dict[str, object]:
    rich, rich_status, text_status = decode_rich_message(state.rich_message_json)
    return {
        key: value
        for key, value in {
            "presence": state.presence,
            "expected_version": state.expected_version,
            "text": state.text,
            "observed_at": state.observed_at,
            "uncertain": state.uncertain,
            "rich_message": rich,
            "rich_media_reference_gaps": rich_media_reference_gaps(rich) if rich is not None else None,
            "rich_content_status": rich_status,
            "rich_text_status": text_status,
        }.items()
        if value is not None
    }


def _history_output(entry: DraftHistoryEntry) -> dict[str, object]:
    snapshot = entry.restore_snapshot
    restorable = entry.presence == "absent" or (snapshot is not None and snapshot.restore_reason is None)
    rich, rich_status, text_status = decode_rich_message(entry.rich_message_json)
    result: dict[str, object] = {
        "history_id": entry.history_id,
        "presence": entry.presence,
        "archived_at": entry.archived_at,
        "restorable": restorable,
        "rich_message": rich,
        "rich_content_status": rich_status,
        "rich_text_status": text_status,
        "observed_at": entry.source_observed_at,
        "source_order_at": entry.source_order_at,
    }
    if entry.text is not None:
        result["text"] = entry.text
    if rich is not None:
        result["rich_media_reference_gaps"] = rich_media_reference_gaps(rich)
    if not restorable:
        result["restore_reason"] = snapshot.restore_reason if snapshot else "restore_snapshot_unavailable"
    return result


def _omit_content(item: dict[str, object], reason: str) -> dict[str, object]:
    return {
        **{
            key: value
            for key, value in item.items()
            if key not in {"text", "rich_message", "rich_media_reference_gaps"}
        },
        "content_included": False,
        "content_omission_reason": reason,
    }


def _response_size(result: dict[str, object]) -> int:
    return len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode())


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


def _draft_response(
    conn: sqlite3.Connection,
    account_id: int,
    state: DraftState,
    history: tuple[DraftHistoryEntry, ...],
    selected: DraftHistoryEntry | None,
) -> dict[str, object]:
    current = current_draft_output(state)
    full_history = [_history_output(entry) for entry in history]
    result: dict[str, object] = {
        "ok": True,
        "current": current,
        **_sync_evidence(conn, account_id),
        "history": [_omit_content(entry, "response_budget") for entry in full_history],
        "next_action": "Use get_draft with history_id to read omitted history content. "
        "Use manage_draft explicitly to change a draft; send manually in Telegram.",
    }
    if selected is not None:
        result["selected_history"] = _history_output(selected)
        if _response_size(result) > MESSAGE_RESPONSE_MAX_BYTES:
            result["current"] = _omit_content(current, "response_budget")
    previews = cast(list[dict[str, object]], result["history"])
    for index, entry in enumerate(full_history):
        if selected is not None and entry["history_id"] == selected.history_id:
            previews[index] = _omit_content(entry, "selected_history")
            continue
        previous = previews[index]
        previews[index] = entry
        if _response_size(result) > MESSAGE_RESPONSE_MAX_BYTES:
            previews[index] = previous
    return result


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
            "message": "That previous state is unavailable or expired.\nAction: read get_draft for retained history.",
        }
    return _draft_response(conn, account_id, state, history, selected)
