"""Pure Account Trace helpers extracted from the daemon API server."""

import dataclasses
import json
import sqlite3
import time
from datetime import UTC, datetime

from .activity_peer_sweep import enroll_activity_dialog
from .models import DialogType
from .sync_worker import ExtractedMessage

_TRACE_FRAGMENT_STATUSES = {
    "pending",
    "partial",
    "complete",
    "flood_wait",
    "access_lost",
    "unsupported",
    "budget_exceeded",
}
_TRACE_PARTIAL_SYNC_STATUSES = {"fragment", "own_only", "syncing", "access_lost"}
_TRACE_PARTIAL_FRAGMENT_STATUSES = {
    "pending",
    "partial",
    "flood_wait",
    "access_lost",
    "unsupported",
    "budget_exceeded",
}
_TRACE_GAP_SEVERITIES = {"info", "warning", "action_required"}

_TRACE_ENRICHMENT_MAX_DIALOGS = 10
_TRACE_ENRICHMENT_MAX_PER_DIALOG = 100
_TRACE_ENRICHMENT_DEADLINE_MS = 15_000
_TRACE_ENRICHMENT_CONCURRENCY = 2

_TRACE_MESSAGE_BASE_FIELDS = (
    "dialog_id",
    "message_id",
    "sent_at",
    "text",
    "sender_id",
    "sender_first_name",
    "media_description",
    "reply_to_msg_id",
    "reply_count",
    "forum_topic_id",
    "edit_date",
    "grouped_id",
    "reply_to_peer_id",
    "out",
    "is_service",
    "post_author",
)
_TRACE_MESSAGE_COMPARE_FIELDS = (*_TRACE_MESSAGE_BASE_FIELDS, "is_deleted")


@dataclasses.dataclass(frozen=True, slots=True)
class _TraceCoverageFragmentUpsertRequest:
    conn: sqlite3.Connection
    target_user_id: int
    dialog_id: int
    status: str
    topic_id: int | None = None
    coverage_kind: str = "authored_message"
    fetched_at: int | None = None
    checkpoint: str | None = None
    last_error: str | None = None
    next_retry_at: int | None = None
    now: int | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class _TraceGapCandidate:
    kind: str
    severity: str
    detail: str
    dialog_id: int | None = None
    topic_id: int | None = None
    action: dict | None = None
    next_action: dict | None = None
    extra: dict | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class _TraceGapBuildRequest:
    conn: sqlite3.Connection
    target_user_id: int
    evidence: list[dict]
    coverage: dict
    exact_dialog_id: int | None = None
    exact_topic_id: int | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class _TraceCandidateBuildRequest:
    conn: sqlite3.Connection
    target_user_id: int
    observed_rows: list[sqlite3.Row] | list[dict]
    exact_dialog_id: int | None = None
    exact_topic_id: int | None = None
    max_dialogs: int = _TRACE_ENRICHMENT_MAX_DIALOGS
    linked_chat_map: dict[int, int] | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class _TraceCandidateBuildState:
    request: _TraceCandidateBuildRequest
    candidates: list[dict]
    seen: set[int]
    linked_chat_map: dict[int, int]


@dataclasses.dataclass(frozen=True, slots=True)
class _TraceMessageQueryRequest:
    target_user_id: int
    self_id: int | None
    limit: int
    post_author_aliases: list[str] | None = None
    exact_dialog_id: int | None = None
    exact_topic_id: int | None = None
    sent_after_ts: int | None = None
    sent_before_ts: int | None = None
    navigation: dict[str, int] | None = None
    scope_dialog_ids: list[int] | None = None


def _parse_trace_int(value: object) -> int | None:
    """Return an int for a signed numeric trace selector, otherwise None."""
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return None
    selector = value.strip()
    if not selector:
        return None
    if selector.isdigit():
        return int(selector)
    if selector[0] in "+-" and selector[1:].isdigit():
        return int(selector)
    return None


def _trace_account_from_entity_row(row: sqlite3.Row, *, resolution_source: str) -> dict:
    """Convert an entities row into the Account Trace resolution envelope."""
    account_id = int(row["id"])
    display_name = row["name"]
    username = row["username"]
    display_aliases = _unique_trace_aliases(
        display_name,
        username,
        f"@{username}" if username else None,
        row["name_normalized"],
    )
    return {
        "confidence": "resolved",
        "account_id": account_id,
        "display_name": display_name,
        "username": username,
        "candidate_ids": [],
        "display_aliases": display_aliases,
        "resolution_source": resolution_source,
    }


def _unresolved_trace_account(
    *,
    query: object,
    resolution_source: str,
    candidate_ids: list[int] | None = None,
    display_aliases: list[str] | None = None,
    confidence: str = "unresolved",
) -> dict:
    """Build a normal non-exception trace resolution failure envelope."""
    return {
        "confidence": confidence,
        "account_id": None,
        "display_name": str(query) if query is not None else None,
        "username": None,
        "candidate_ids": candidate_ids or [],
        "display_aliases": display_aliases or [],
        "resolution_source": resolution_source,
    }


def _parse_trace_time_bound(value: object) -> int | None:
    """Parse a trace time bound as unix seconds or ISO datetime string."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return None
    return _parse_trace_time_bound_from_string(value)


def _parse_trace_time_bound_from_string(value: str) -> int | None:
    text = value.strip()
    if not text:
        return None
    parsed_int = _parse_trace_int(text)
    if parsed_int is not None:
        return parsed_int
    iso_text = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(iso_text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp())


def _get_trace_coverage_fragments(
    conn: sqlite3.Connection,
    *,
    target_user_id: int,
    exact_dialog_id: int | None = None,
    exact_topic_id: int | None = None,
    coverage_kind: str = "authored_message",
) -> list[dict]:
    """Read target-specific Account Trace coverage fragment rows."""
    sql = (
        "SELECT target_user_id, dialog_id, topic_id, coverage_kind, status, "
        "fetched_at, checkpoint, last_error, next_retry_at, created_at, updated_at "
        "FROM trace_coverage_fragments "
        "WHERE target_user_id = :target_user_id AND coverage_kind = :coverage_kind"
    )
    params: dict[str, object] = {
        "target_user_id": target_user_id,
        "coverage_kind": coverage_kind,
    }
    if exact_dialog_id is not None:
        sql += " AND dialog_id = :exact_dialog_id"
        params["exact_dialog_id"] = exact_dialog_id
    if exact_topic_id is not None:
        sql += " AND topic_id = :exact_topic_id"
        params["exact_topic_id"] = exact_topic_id
    rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def _sanitize_trace_last_error(last_error: str | None) -> str | None:
    if last_error is None:
        return None
    compact = " ".join(last_error.split())
    return compact[:120]


def _upsert_trace_coverage_fragment(
    request: _TraceCoverageFragmentUpsertRequest,
) -> None:
    """Insert/update one target-specific coverage fragment."""
    conn = request.conn
    if request.status not in _TRACE_FRAGMENT_STATUSES:
        raise ValueError(f"invalid trace coverage status: {request.status}")
    timestamp = request.now if request.now is not None else int(time.time())
    conn.execute(
        """
        INSERT INTO trace_coverage_fragments
            (target_user_id, dialog_id, topic_id, coverage_kind, status,
             fetched_at, checkpoint, last_error, next_retry_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(target_user_id, dialog_id, topic_id, coverage_kind)
        DO UPDATE SET
            status = excluded.status,
            fetched_at = excluded.fetched_at,
            checkpoint = excluded.checkpoint,
            last_error = excluded.last_error,
            next_retry_at = excluded.next_retry_at,
            updated_at = excluded.updated_at
        """,
        (
            request.target_user_id,
            request.dialog_id,
            0 if request.topic_id is None else request.topic_id,
            request.coverage_kind,
            request.status,
            request.fetched_at,
            request.checkpoint,
            _sanitize_trace_last_error(request.last_error),
            request.next_retry_at,
            timestamp,
            timestamp,
        ),
    )


def _row_value(row: sqlite3.Row | dict, key: str) -> object:
    if isinstance(row, dict):
        return row.get(key)
    return row[key]


def _row_int(row: sqlite3.Row | dict, key: str) -> int:
    value = _row_value(row, key)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    msg = f"{key} must be an integer"
    raise ValueError(msg)


def _dialog_status_map(conn: sqlite3.Connection, dialog_ids: set[int]) -> dict[int, str | None]:
    if not dialog_ids:
        return {}
    placeholders = ",".join("?" * len(dialog_ids))
    rows = conn.execute(
        f"SELECT dialog_id, status FROM synced_dialogs WHERE dialog_id IN ({placeholders})",
        tuple(dialog_ids),
    ).fetchall()
    result: dict[int, str | None] = {int(row[0]): str(row[1]) for row in rows}
    for dialog_id in dialog_ids:
        result.setdefault(dialog_id, None)
    return result


def _build_trace_coverage(
    conn: sqlite3.Connection,
    target_user_id: int,
    rows: list[sqlite3.Row] | list[dict],
    *,
    exact_dialog_id: int | None = None,
    exact_topic_id: int | None = None,
) -> dict:
    """Build bounded Account Trace coverage semantics for the current response."""
    observed_dialogs = {_row_int(row, "dialog_id") for row in rows}
    fragments = _get_trace_coverage_fragments(
        conn,
        target_user_id=target_user_id,
        exact_dialog_id=exact_dialog_id,
        exact_topic_id=exact_topic_id,
    )
    fragment_dialogs = {int(fragment["dialog_id"]) for fragment in fragments}

    if exact_dialog_id is not None:
        considered_dialogs = {exact_dialog_id}
        basis = "exact_dialog_scope"
    else:
        access_lost_dialogs = {
            int(row[0])
            for row in conn.execute("SELECT dialog_id FROM synced_dialogs WHERE status = 'access_lost'").fetchall()
        }
        considered_dialogs = observed_dialogs | fragment_dialogs | access_lost_dialogs
        basis = "evidence_or_fragments_or_access_lost" if considered_dialogs else "none"

    status_by_dialog = _dialog_status_map(conn, considered_dialogs)
    gap_dialogs: set[int] = set()
    for dialog_id, status in status_by_dialog.items():
        if status is None or status in _TRACE_PARTIAL_SYNC_STATUSES:
            gap_dialogs.add(dialog_id)
    for fragment in fragments:
        if str(fragment["status"]) in _TRACE_PARTIAL_FRAGMENT_STATUSES:
            gap_dialogs.add(int(fragment["dialog_id"]))

    if not considered_dialogs:
        state = "unknown"
    elif gap_dialogs:
        state = "partial"
    else:
        state = "complete"

    return {
        "state": state,
        "observed_message_count": len(rows),
        "dialogs_considered": len(considered_dialogs),
        "dialogs_considered_basis": basis,
        "dialogs_with_hits": len(observed_dialogs),
        "dialogs_with_gaps": len(gap_dialogs),
        "as_of": int(time.time()),
    }


def _trace_gap_for_dialog_status(
    *,
    status: str | None,
    dialog_id: int,
    exact_dialog_id: int | None,
    exact_topic_id: int | None,
) -> _TraceGapCandidate | None:
    topic_id = exact_topic_id if dialog_id == exact_dialog_id else None
    if status is None or status == "not_synced":
        return _TraceGapCandidate(
            kind="dialog_not_synced",
            severity="action_required",
            detail="This dialog has not been synced for Account Trace evidence.",
            dialog_id=dialog_id,
            topic_id=topic_id,
            action={"tool": "mark_dialog_for_sync", "arguments": {"dialog_id": dialog_id}},
        )
    if status == "access_lost":
        return _TraceGapCandidate(
            kind="access_lost",
            severity="warning",
            detail="The local archive has no current access to this dialog.",
            dialog_id=dialog_id,
            topic_id=topic_id,
        )
    if status in {"fragment", "own_only"}:
        return _TraceGapCandidate(
            kind="fragment_only",
            severity="warning",
            detail=f"Dialog coverage is {status}; Account Trace may be incomplete.",
            dialog_id=dialog_id,
            topic_id=topic_id,
        )
    if status == "syncing":
        return _TraceGapCandidate(
            kind="history_incomplete",
            severity="warning",
            detail="Dialog sync is still in progress.",
            dialog_id=dialog_id,
            topic_id=topic_id,
        )
    return None


def _trace_gap_for_fragment_status(
    status: str,
    dialog_id: int,
    topic_id: int | None,
) -> _TraceGapCandidate | None:
    if status == "flood_wait":
        return _TraceGapCandidate(
            kind="flood_wait",
            severity="warning",
            detail="Targeted trace enrichment is waiting for Telegram rate-limit cooldown.",
            dialog_id=dialog_id,
            topic_id=topic_id,
            extra={},
        )
    if status == "budget_exceeded":
        return _TraceGapCandidate(
            kind="budget_exceeded",
            severity="warning",
            detail="Bounded trace enrichment exhausted its request budget.",
            dialog_id=dialog_id,
            topic_id=topic_id,
        )
    if status == "unsupported":
        return _TraceGapCandidate(
            kind="history_incomplete",
            severity="warning",
            detail="This dialog type is not supported for targeted enrichment.",
            dialog_id=dialog_id,
            topic_id=topic_id,
        )
    return None


def _trace_gap(request: _TraceGapCandidate) -> dict:
    if request.severity not in _TRACE_GAP_SEVERITIES:
        raise ValueError(f"invalid trace gap severity: {request.severity}")
    gap: dict[str, object] = {
        "kind": request.kind,
        "severity": request.severity,
        "detail": request.detail,
    }
    if request.dialog_id is not None:
        gap["dialog_id"] = request.dialog_id
    if request.topic_id is not None:
        gap["topic_id"] = request.topic_id
    if request.action is not None:
        gap["action"] = request.action
    if request.next_action is not None:
        gap["next_action"] = request.next_action
    if request.extra:
        gap.update(request.extra)
    return gap


def _build_trace_gaps(
    request: _TraceGapBuildRequest,
) -> list[dict]:
    """Build controlled Account Trace coverage gaps and actions."""
    fragment_rows = _get_trace_coverage_fragments(
        request.conn,
        target_user_id=request.target_user_id,
        exact_dialog_id=request.exact_dialog_id,
        exact_topic_id=request.exact_topic_id,
    )
    considered_dialogs = _collect_trace_gap_dialogs(request, fragment_rows=fragment_rows)
    status_by_dialog = _dialog_status_map(request.conn, considered_dialogs)
    gaps = _collect_trace_gaps_for_dialog_statuses(
        request=request,
        status_by_dialog=status_by_dialog,
        considered_dialogs=considered_dialogs,
    )
    gaps.extend(_collect_trace_gaps_for_hidden_dialogs(request=request, considered_dialogs=considered_dialogs))
    gaps.extend(_collect_trace_gaps_for_fragment_rows(request=request, fragment_rows=fragment_rows))
    gaps.extend(_collect_trace_gaps_for_evidence(request.evidence))
    if not request.evidence and not gaps:
        gaps.append(_build_observed_zero_trace_gap())
    return gaps


def _collect_trace_gap_dialogs(
    request: _TraceGapBuildRequest,
    fragment_rows: list[dict],
) -> set[int]:
    dialog_ids = {int(item["dialog_id"]) for item in request.evidence}
    dialog_ids.update(int(row["dialog_id"]) for row in fragment_rows)
    if request.exact_dialog_id is not None:
        dialog_ids.add(request.exact_dialog_id)
    elif request.coverage.get("dialogs_considered", 0):
        dialog_ids.update(
            int(row[0])
            for row in request.conn.execute(
                "SELECT dialog_id FROM synced_dialogs WHERE status = 'access_lost'"
            ).fetchall()
        )
    return dialog_ids


def _collect_trace_gaps_for_dialog_statuses(
    request: _TraceGapBuildRequest,
    status_by_dialog: dict[int, str | None],
    *,
    considered_dialogs: set[int],
) -> list[dict]:
    gaps: list[dict] = []
    for dialog_id in sorted(considered_dialogs):
        candidate = _trace_gap_for_dialog_status(
            status=status_by_dialog.get(dialog_id),
            dialog_id=dialog_id,
            exact_dialog_id=request.exact_dialog_id,
            exact_topic_id=request.exact_topic_id,
        )
        if candidate is not None:
            gaps.append(_trace_gap(candidate))
    return gaps


def _collect_trace_gaps_for_hidden_dialogs(
    request: _TraceGapBuildRequest,
    *,
    considered_dialogs: set[int],
) -> list[dict]:
    hidden_rows = request.conn.execute("SELECT dialog_id FROM dialogs WHERE hidden = 1").fetchall()
    hidden_dialogs = {int(row[0]) for row in hidden_rows}
    return [
        _trace_gap(
            _TraceGapCandidate(
                kind="hidden_dialog",
                severity="warning",
                detail="Dialog is hidden in the local mirror.",
                dialog_id=dialog_id,
            )
        )
        for dialog_id in sorted(considered_dialogs & hidden_dialogs)
    ]


def _collect_trace_gaps_for_fragment_rows(
    request: _TraceGapBuildRequest,
    *,
    fragment_rows: list[dict],
) -> list[dict]:
    gaps: list[dict] = []
    for fragment in fragment_rows:
        candidate = _trace_gap_for_fragment(request=request, fragment=fragment)
        if candidate is None:
            continue
        gaps.append(_trace_gap(candidate))
    return gaps


def _trace_gap_for_fragment(
    request: _TraceGapBuildRequest,
    *,
    fragment: dict,
) -> _TraceGapCandidate | None:
    dialog_id = int(fragment["dialog_id"])
    topic_id = int(fragment["topic_id"])
    status = str(fragment["status"])
    candidate = _trace_gap_for_fragment_status(
        status,
        dialog_id=dialog_id,
        topic_id=None if topic_id == 0 else topic_id,
    )
    if candidate is None:
        return None
    if status == "flood_wait":
        return dataclasses.replace(
            candidate,
            extra={"next_retry_at": fragment.get("next_retry_at"), **(candidate.extra or {})},
        )
    return candidate


def _collect_trace_gaps_for_evidence(evidence: list[dict]) -> list[dict]:
    if not any(item.get("authorship_basis") == "post_author_signature" for item in evidence):
        return []
    return [
        _trace_gap(
            _TraceGapCandidate(
                kind="channel_signature_ambiguous",
                severity="info",
                detail="Channel post signatures are author text, not numeric Telegram user identity proof.",
            )
        )
    ]


def _build_observed_zero_trace_gap() -> dict:
    return _trace_gap(
        _TraceGapCandidate(
            kind="observed_zero",
            severity="info",
            detail="No authored-message evidence was observed in the considered local coverage.",
        )
    )


def _trace_strategy_for_dialog(dialog_type: str, *, status: str | None, hidden: bool) -> str:
    if hidden:
        return "hidden"
    if status == "access_lost":
        return "access_lost"
    dt = DialogType.parse(dialog_type)
    if dt in (DialogType.USER, DialogType.BOT):
        return "dialog_scan"
    if dt in (DialogType.SUPERGROUP, DialogType.FORUM, DialogType.GROUP):
        return "author_search"
    if dt == DialogType.CHANNEL:
        return "signature_only"
    return "unsupported"


def _trace_dialog_metadata(conn: sqlite3.Connection, dialog_id: int) -> dict:
    row = conn.execute(
        """
        SELECT
            COALESCE(d.type, e.type, 'Unknown') AS dialog_type,
            COALESCE(sd.status, 'not_synced') AS status,
            COALESCE(d.hidden, 0) AS hidden
        FROM (SELECT ? AS dialog_id) x
        LEFT JOIN dialogs d ON d.dialog_id = x.dialog_id
        LEFT JOIN entities e ON e.id = x.dialog_id
        LEFT JOIN synced_dialogs sd ON sd.dialog_id = x.dialog_id
        """,
        (dialog_id,),
    ).fetchone()
    return {
        "dialog_type": str(row[0]) if row else "Unknown",
        "status": str(row[1]) if row else "not_synced",
        "hidden": bool(row[2]) if row else False,
    }


def _trace_common_chat_ids(conn: sqlite3.Connection, target_user_id: int) -> list[int]:
    row = conn.execute(
        "SELECT detail_json FROM entity_details WHERE entity_id = ?",
        (target_user_id,),
    ).fetchone()
    if row is None:
        return []
    try:
        detail = json.loads(row[0])
    except TypeError, json.JSONDecodeError:
        return []
    common_chats = detail.get("common_chats", [])
    if not isinstance(common_chats, list):
        return []
    ids: list[int] = []
    for item in common_chats:
        if not isinstance(item, dict):
            continue
        raw_id = item.get("id")
        if raw_id is None:
            continue
        try:
            ids.append(int(raw_id))
        except TypeError, ValueError:
            continue
    return ids


def _trace_candidate_dialogs(
    request: _TraceCandidateBuildRequest,
) -> list[dict]:
    """Select deterministic bounded Account Trace enrichment candidates."""
    now = int(time.time())
    state = _TraceCandidateBuildState(
        request=request,
        candidates=[],
        seen=set(),
        linked_chat_map=request.linked_chat_map or {},
    )
    _collect_trace_candidate_dialogs(
        state=state,
        now=now,
    )
    return state.candidates


def _collect_trace_candidate_dialogs(
    *,
    state: _TraceCandidateBuildState,
    now: int,
) -> None:
    request = state.request
    if request.exact_dialog_id is not None:
        _add_trace_candidate_dialog(
            state=state,
            dialog_id=request.exact_dialog_id,
            origin="exact_dialog",
            include_inaccessible=True,
        )
    for row in request.observed_rows:
        _add_trace_candidate_dialog(
            state=state,
            dialog_id=_row_int(row, "dialog_id"),
            origin="observed_evidence",
        )
    _add_trace_candidate_fragments(
        state=state,
        now=now,
    )
    _add_trace_candidate_common_chats(
        state=state,
    )
    _add_trace_candidate_visible_synced(
        state=state,
    )


def _add_trace_candidate_dialog(
    state: _TraceCandidateBuildState,
    *,
    dialog_id: int,
    origin: str,
    include_inaccessible: bool = False,
) -> None:
    request = state.request
    if dialog_id in state.seen or len(state.candidates) >= request.max_dialogs:
        return
    meta = _trace_dialog_metadata(request.conn, dialog_id)
    if not include_inaccessible and (meta["status"] == "access_lost" or meta["hidden"]):
        return
    strategy = _trace_strategy_for_dialog(
        meta["dialog_type"],
        status=meta["status"],
        hidden=bool(meta["hidden"]),
    )
    state.candidates.append(
        {
            "dialog_id": dialog_id,
            "dialog_type": meta["dialog_type"],
            "status": meta["status"],
            "hidden": bool(meta["hidden"]),
            "strategy": strategy,
            "origin": origin,
            "topic_id": request.exact_topic_id if request.exact_dialog_id == dialog_id else None,
        }
    )
    state.seen.add(dialog_id)

    linked_chat_map = state.linked_chat_map
    if strategy == "signature_only" and dialog_id in linked_chat_map:
        linked_id = linked_chat_map[dialog_id]
        if linked_id not in state.seen:
            enroll_activity_dialog(request.conn, linked_id, source="linked_chat")
            _add_trace_candidate_dialog(
                state=state,
                dialog_id=linked_id,
                origin="linked_chat",
            )


def _add_trace_candidate_fragments(
    state: _TraceCandidateBuildState,
    *,
    now: int,
) -> None:
    request = state.request
    fragment_rows = request.conn.execute(
        """
        SELECT dialog_id
        FROM trace_coverage_fragments
        WHERE target_user_id = ?
          AND status != 'complete'
          AND (next_retry_at IS NULL OR next_retry_at <= ?)
        ORDER BY updated_at ASC, dialog_id ASC
        """,
        (request.target_user_id, now),
    ).fetchall()
    for row in fragment_rows:
        _add_trace_candidate_dialog(
            state=state,
            dialog_id=int(row[0]),
            origin="trace_fragment_retry",
        )


def _add_trace_candidate_common_chats(
    state: _TraceCandidateBuildState,
) -> None:
    request = state.request
    for dialog_id in _trace_common_chat_ids(request.conn, request.target_user_id):
        _add_trace_candidate_dialog(
            state=state,
            dialog_id=dialog_id,
            origin="cached_common_chat",
        )


def _add_trace_candidate_visible_synced(
    state: _TraceCandidateBuildState,
) -> None:
    request = state.request
    visible_rows = request.conn.execute(
        """
        SELECT sd.dialog_id
        FROM synced_dialogs sd
        LEFT JOIN dialogs d ON d.dialog_id = sd.dialog_id
        WHERE sd.status != 'access_lost'
          AND COALESCE(d.hidden, 0) = 0
        ORDER BY sd.dialog_id ASC
        """
    ).fetchall()
    for row in visible_rows:
        _add_trace_candidate_dialog(
            state=state,
            dialog_id=int(row[0]),
            origin="visible_synced",
        )


def _trace_existing_message_bundle(
    conn: sqlite3.Connection,
    *,
    dialog_id: int,
    message_id: int,
) -> dict | None:
    columns = ", ".join(_TRACE_MESSAGE_COMPARE_FIELDS)
    row = conn.execute(
        f"SELECT {columns} FROM messages WHERE dialog_id = ? AND message_id = ?",
        (dialog_id, message_id),
    ).fetchone()
    if row is None:
        return None
    return {
        "message": {field: row[index] for index, field in enumerate(_TRACE_MESSAGE_COMPARE_FIELDS)},
        "reactions": sorted(
            tuple(item)
            for item in conn.execute(
                """
                SELECT emoji, count FROM message_reactions
                WHERE dialog_id = ? AND message_id = ?
                ORDER BY emoji, count
                """,
                (dialog_id, message_id),
            ).fetchall()
        ),
        "entities": sorted(
            tuple(item)
            for item in conn.execute(
                """
                SELECT offset, length, type, value FROM message_entities
                WHERE dialog_id = ? AND message_id = ?
                ORDER BY offset, length, type, value
                """,
                (dialog_id, message_id),
            ).fetchall()
        ),
        "forward": (
            tuple(forward_row)
            if (
                forward_row := conn.execute(
                    """
                    SELECT fwd_from_peer_id, fwd_from_name, fwd_date, fwd_channel_post
                    FROM message_forwards
                    WHERE dialog_id = ? AND message_id = ?
                    """,
                    (dialog_id, message_id),
                ).fetchone()
            )
            else None
        ),
    }


def _messages_row_equal(existing: dict | None, candidate: ExtractedMessage) -> bool:
    """Compare existing base/child rows with one extracted candidate bundle."""
    if existing is None:
        return False

    existing_message = existing.get("message", {})
    if existing_message.get("is_deleted") != 0:
        return False

    candidate_message = dataclasses.asdict(candidate.message)
    candidate_message["is_deleted"] = 0
    for field in _TRACE_MESSAGE_COMPARE_FIELDS:
        if existing_message.get(field) != candidate_message.get(field):
            return False

    candidate_reactions = sorted((item.emoji, item.count) for item in candidate.reactions)
    if existing.get("reactions", []) != candidate_reactions:
        return False

    candidate_entities = sorted((item.offset, item.length, item.type, item.value) for item in candidate.entities)
    if existing.get("entities", []) != candidate_entities:
        return False

    if candidate.forward is None:
        candidate_forward = None
    else:
        candidate_forward = (
            candidate.forward.fwd_from_peer_id,
            candidate.forward.fwd_from_name,
            candidate.forward.fwd_date,
            candidate.forward.fwd_channel_post,
        )
    return existing.get("forward") == candidate_forward


def _trace_enrichment_result(
    *,
    deadline_ms: int,
    concurrency: int,
    max_dialogs: int,
    max_per_dialog: int,
) -> dict:
    return {
        "dialogs_attempted": 0,
        "dialogs_skipped": 0,
        "messages_seen": 0,
        "messages_persisted": 0,
        "duplicates_skipped": 0,
        "deadline_ms": deadline_ms,
        "concurrency": concurrency,
        "coverage_bounds": {
            "max_dialogs": max_dialogs,
            "max_per_dialog": max_per_dialog,
            "deadline_ms": deadline_ms,
        },
        "fragment_status_counts": {},
    }


def _trace_increment_status(result: dict, status: str) -> None:
    counts = result.setdefault("fragment_status_counts", {})
    counts[status] = counts.get(status, 0) + 1


def _build_trace_account_messages_query(
    request: _TraceMessageQueryRequest,
) -> tuple[str, dict]:
    """Build the baseline Account Trace query over canonical message rows."""
    params: dict[str, object] = {
        "target_user_id": request.target_user_id,
        "self_id": request.self_id,
        "limit": request.limit,
    }
    sql = (
        "SELECT "
        "m.dialog_id, "
        "m.message_id, "
        "m.sent_at, "
        "m.text, "
        "m.sender_id, "
        "m.media_description, "
        "m.forum_topic_id AS topic_id, "
        "COALESCE(d.name, e_dialog.name, CAST(m.dialog_id AS TEXT)) AS dialog_title, "
        "COALESCE(d.type, e_dialog.type) AS dialog_type, "
        "tm.title AS topic_title, "
        "m.post_author AS author_signature, "
        f"{EFFECTIVE_SENDER_ID_SQL}, "
        "CASE "
        f"WHEN {_EFFECTIVE_SENDER_ID_EXPR} = :target_user_id THEN 'effective_sender_id' "
        "ELSE 'post_author_signature' "
        "END AS authorship_basis "
        "FROM messages m "
        "LEFT JOIN dialogs d ON d.dialog_id = m.dialog_id "
        "LEFT JOIN entities e_dialog ON e_dialog.id = m.dialog_id "
        "LEFT JOIN topic_metadata tm "
        "  ON tm.dialog_id = m.dialog_id AND tm.topic_id = m.forum_topic_id "
        "WHERE m.is_deleted = 0 AND m.is_service = 0"
    )

    authorship_predicates = [f"{_EFFECTIVE_SENDER_ID_EXPR} = :target_user_id"]
    aliases = request.post_author_aliases or []
    if aliases:
        placeholders: list[str] = []
        for idx, alias in enumerate(aliases):
            param_name = f"post_author_alias_{idx}"
            placeholders.append(f":{param_name}")
            params[param_name] = alias
        authorship_predicates.append(f"m.post_author IN ({', '.join(placeholders)})")
    sql += f" AND ({' OR '.join(authorship_predicates)})"

    if request.scope_dialog_ids:
        scope_placeholders = [f":scope_{i}" for i in range(len(request.scope_dialog_ids))]
        sql += f" AND m.dialog_id IN ({', '.join(scope_placeholders)})"
        for i, sid in enumerate(request.scope_dialog_ids):
            params[f"scope_{i}"] = sid
    elif request.exact_dialog_id is not None:
        sql += " AND m.dialog_id = :exact_dialog_id"
        params["exact_dialog_id"] = request.exact_dialog_id

    if request.exact_topic_id is not None:
        sql += " AND m.forum_topic_id = :exact_topic_id"
        params["exact_topic_id"] = request.exact_topic_id

    if request.sent_after_ts is not None:
        sql += " AND m.sent_at >= :sent_after"
        params["sent_after"] = request.sent_after_ts

    if request.sent_before_ts is not None:
        sql += " AND m.sent_at <= :sent_before"
        params["sent_before"] = request.sent_before_ts

    if request.navigation is not None:
        sql += (
            " AND ("
            "m.sent_at < :nav_sent_at "
            "OR (m.sent_at = :nav_sent_at AND m.dialog_id < :nav_dialog_id) "
            "OR (m.sent_at = :nav_sent_at AND m.dialog_id = :nav_dialog_id "
            "AND m.message_id < :nav_message_id)"
            ")"
        )
        params["nav_sent_at"] = request.navigation["sent_at"]
        params["nav_dialog_id"] = request.navigation["dialog_id"]
        params["nav_message_id"] = request.navigation["message_id"]

    sql += " ORDER BY m.sent_at DESC, m.dialog_id DESC, m.message_id DESC LIMIT :limit"
    return sql, params


def _unique_trace_aliases(*values: object) -> list[str]:
    """Build a stable de-duplicated non-empty alias list for post_author matching."""
    aliases: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        alias = value.strip()
        if not alias:
            continue
        for candidate in (alias, alias.removeprefix("@")):
            if candidate and candidate not in seen:
                seen.add(candidate)
                aliases.append(candidate)
    return aliases


# Keep these SQL aliases in this module to avoid an import cycle.
_EFFECTIVE_SENDER_ID_EXPR = (
    "COALESCE("
    "m.sender_id, "
    "CASE "
    "WHEN m.is_service = 1 THEN NULL "
    "WHEN m.dialog_id > 0 AND m.out = 1 THEN :self_id "
    "WHEN m.dialog_id > 0 AND m.out = 0 THEN m.dialog_id "
    "ELSE NULL "
    "END"
    ")"
)
EFFECTIVE_SENDER_ID_SQL = _EFFECTIVE_SENDER_ID_EXPR + " AS effective_sender_id"
_SENDER_FIRST_NAME_SQL = "COALESCE(e_raw.name, e_eff.name, m.sender_first_name) AS sender_first_name"
_SENDER_ENTITY_JOINS_SQL = (
    "LEFT JOIN entities e_raw ON e_raw.id = m.sender_id "
    f"LEFT JOIN entities e_eff ON e_eff.id = {_EFFECTIVE_SENDER_ID_EXPR} "
)
