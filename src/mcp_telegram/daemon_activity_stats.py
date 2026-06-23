"""Activity and stats service extracted from daemon_api."""

import sqlite3
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, cast


class _LoggerLike(Protocol):
    def debug(self, msg: str, *_args: object, **_kwargs: object) -> None: ...

    def info(self, msg: str, *_args: object, **_kwargs: object) -> None: ...

    def warning(self, msg: str, *_args: object, **_kwargs: object) -> None: ...

    def error(self, msg: str, *_args: object, **_kwargs: object) -> None: ...

    def exception(self, msg: str, *_args: object, **_kwargs: object) -> None: ...


DEFAULT_ACTIVITY_DIALOG_KINDS = ("group", "forum")
_ALLOWED_ACTIVITY_DIALOG_KINDS = {"all", "user", "bot", "group", "forum", "channel", "unknown"}
_ACTIVITY_DIALOG_KIND_ALIASES = {
    "dm": ("user", "bot"),
    "dms": ("user", "bot"),
    "private": ("user", "bot"),
    "personal": ("user", "bot"),
    "direct": ("user", "bot"),
    "groups": ("group", "forum"),
    "supergroup": ("group",),
    "supergroups": ("group",),
    "chat": ("group",),
    "chats": ("group",),
    "forums": ("forum",),
}

_SELECT_SYNC_STATUS_SQL = "SELECT status FROM synced_dialogs WHERE dialog_id = ?"
_GET_DIALOG_TOP_REACTIONS_SQL = (
    "SELECT emoji, SUM(count) AS cnt "
    "FROM message_reactions WHERE dialog_id = ? "
    "GROUP BY emoji ORDER BY cnt DESC, emoji ASC LIMIT ?"
)
_GET_DIALOG_TOP_MENTIONS_SQL = (
    "SELECT value, COUNT(*) AS cnt "
    "FROM message_entities WHERE dialog_id = ? AND type = 'mention' "
    "GROUP BY value ORDER BY cnt DESC, value ASC LIMIT ?"
)
_GET_DIALOG_TOP_HASHTAGS_SQL = (
    "SELECT value, COUNT(*) AS cnt "
    "FROM message_entities WHERE dialog_id = ? AND type = 'hashtag' "
    "GROUP BY value ORDER BY cnt DESC, value ASC LIMIT ?"
)
_GET_DIALOG_TOP_FORWARDS_SQL = (
    "SELECT fwd_from_peer_id, fwd_from_name, COUNT(*) AS cnt "
    "FROM message_forwards WHERE dialog_id = ? "
    "GROUP BY fwd_from_peer_id, fwd_from_name "
    "ORDER BY cnt DESC, fwd_from_name ASC LIMIT ?"
)


@dataclass(frozen=True)
class DaemonActivityStatsDeps:
    """Dependency container for activity/statistics orchestration."""

    conn: sqlite3.Connection
    resolve_dialog_id: Callable[[int, str | None], Awaitable[int | dict]]
    logger: _LoggerLike


@dataclass(frozen=True, slots=True)
class _RecentActivityRequest:
    since_hours: int
    limit: int
    dialog_kinds: list[str]
    sent_after: str | None
    sent_before: str | None
    text_query: str | None
    sent_after_ts: int | None
    sent_before_ts: int | None
    since_ts: int
    query_params: list[object]
    typed_activity_filter_sql: str
    kinded_activity_filter_sql: str


@dataclass(frozen=True, slots=True)
class _RecentActivityQueryParts:
    since_ts: int
    sent_after_ts: int | None
    sent_before_ts: int | None
    normalized_text_query: str | None
    dialog_kinds: list[str]
    limit: int


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(value, high))


def _coerce_int(value: object, default: int) -> int:
    try:
        return int(cast(int | str, value))
    except TypeError, ValueError:
        return default


def _query_usage_stats(cursor: sqlite3.Cursor, since: int) -> dict[str, object]:
    tool_dist = dict(
        cursor.execute(
            "SELECT tool_name, COUNT(*) FROM telemetry_events "
            "WHERE timestamp >= ? GROUP BY tool_name ORDER BY COUNT(*) DESC",
            (since,),
        ).fetchall()
    )

    error_dist = dict(
        cursor.execute(
            "SELECT error_type, COUNT(*) FROM telemetry_events "
            "WHERE timestamp >= ? AND error_type IS NOT NULL "
            "GROUP BY error_type ORDER BY COUNT(*) DESC",
            (since,),
        ).fetchall()
    )

    def _scalar(sql: str, params: tuple[object, ...] = (since,), default: int = 0) -> int:
        row = cast(tuple[object] | None, cursor.execute(sql, params).fetchone())
        return default if row is None or row[0] is None else int(cast(int | str, row[0]))

    max_depth = _scalar(
        "SELECT MAX(page_depth) FROM telemetry_events WHERE timestamp >= ?",
    )
    filter_count = _scalar(
        "SELECT COUNT(*) FROM telemetry_events WHERE timestamp >= ? AND has_filter = 1",
    )
    total_calls = _scalar(
        "SELECT COUNT(*) FROM telemetry_events WHERE timestamp >= ?",
    )

    latencies = cast(
        list[tuple[int]],
        cursor.execute(
            "SELECT duration_ms FROM telemetry_events WHERE timestamp >= ? ORDER BY duration_ms",
            (since,),
        ).fetchall(),
    )

    latency_median_ms = 0
    latency_p95_ms = 0
    if latencies:
        latency_values_ms = [lat[0] for lat in latencies]
        latency_median_ms = latency_values_ms[len(latency_values_ms) // 2]
        p95_idx = int(len(latency_values_ms) * 0.95)
        latency_p95_ms = latency_values_ms[p95_idx] if p95_idx < len(latency_values_ms) else latency_values_ms[-1]

    return {
        "tool_distribution": tool_dist,
        "error_distribution": error_dist,
        "max_page_depth": max_depth,
        "total_calls": total_calls,
        "filter_count": filter_count,
        "latency_median_ms": latency_median_ms,
        "latency_p95_ms": latency_p95_ms,
    }


def _coerce_activity_dialog_kind_values(value: object) -> tuple[list[object] | None, str | None]:
    if value is None:
        return list(DEFAULT_ACTIVITY_DIALOG_KINDS), None
    if isinstance(value, str):
        raw_values: list[object] = [value]
    elif isinstance(value, (list, tuple, set)):
        raw_values = list(value)
    else:
        return None, "dialog_kinds must be a list of strings"

    return raw_values, None


def _normalize_activity_dialog_kind_values(raw_values: list[object]) -> tuple[list[str] | None, str | None]:
    normalized_values: list[str] = []
    for raw in raw_values:
        if not isinstance(raw, str):
            return None, "dialog_kinds entries must be strings"
        normalized = raw.strip().lower()
        if not normalized:
            continue
        expanded = _ACTIVITY_DIALOG_KIND_ALIASES.get(normalized, (normalized,))
        for kind in expanded:
            if kind not in _ALLOWED_ACTIVITY_DIALOG_KINDS:
                allowed = ", ".join(sorted(_ALLOWED_ACTIVITY_DIALOG_KINDS))
                return None, f"dialog_kinds entries must be one of: {allowed}"
            if kind not in normalized_values:
                normalized_values.append(kind)

    if "all" in normalized_values:
        return ["all"], None
    if not normalized_values:
        return None, "dialog_kinds must include at least one kind"
    return normalized_values, None


def _normalize_activity_dialog_kinds(value: object) -> tuple[list[str] | None, str | None]:
    raw_values, error = _coerce_activity_dialog_kind_values(value)
    if error is not None or raw_values is None:
        return None, error
    return _normalize_activity_dialog_kind_values(raw_values)


def _parse_recent_activity_time_bound(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    try:
        if value.endswith("Z"):
            parsed = datetime.fromisoformat(value[:-1]).replace(tzinfo=UTC)
        else:
            parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp())


def _recent_activity_time_error(field: str) -> dict[str, object]:
    return {"ok": False, "error": "invalid_time_bound", "message": f"{field} is invalid"}


def _recent_activity_text_query(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized or None


def _parse_recent_activity_bounds(
    req: Mapping[str, object],
) -> tuple[int | None, int | None, str | None, dict[str, object] | None]:
    sent_after = req.get("sent_after")
    sent_before = req.get("sent_before")
    text_query = req.get("text_query")
    sent_after_ts = _parse_recent_activity_time_bound(sent_after)
    if sent_after is not None and sent_after_ts is None:
        return None, None, None, _recent_activity_time_error("sent_after")
    sent_before_ts = _parse_recent_activity_time_bound(sent_before)
    if sent_before is not None and sent_before_ts is None:
        return None, None, None, _recent_activity_time_error("sent_before")
    if text_query is not None and not isinstance(text_query, str):
        return None, None, None, {"ok": False, "error": "invalid_text_query", "message": "text_query must be a string"}
    return sent_after_ts, sent_before_ts, _recent_activity_text_query(text_query), None


def _parse_recent_activity_limits(
    req: Mapping[str, object],
) -> tuple[int | None, int | None, list[str] | None, dict[str, object] | None]:
    since_hours = _clamp(_coerce_int(req.get("since_hours", 168), 168), 1, 8760)
    limit = _clamp(_coerce_int(req.get("limit", 500), 500), 1, 2000)

    dialog_kinds, dialog_kind_error = _normalize_activity_dialog_kinds(
        req.get("dialog_kinds", list(DEFAULT_ACTIVITY_DIALOG_KINDS))
    )
    if dialog_kind_error is not None or dialog_kinds is None:
        return (
            None,
            None,
            None,
            {
                "ok": False,
                "error": "invalid_dialog_kinds",
                "message": dialog_kind_error or "invalid dialog_kinds",
            },
        )
    return since_hours, limit, dialog_kinds, None


def _recent_activity_query_parts(request: _RecentActivityQueryParts) -> tuple[str, str, list[object]]:
    typed_activity_filters = ["m.out = 1", "m.is_service = 0", "m.is_deleted = 0", "m.sent_at >= ?"]
    kinded_activity_filters: list[str] = []
    query_params: list[object] = [request.since_ts]
    if request.sent_after_ts is not None:
        typed_activity_filters.append("m.sent_at >= ?")
        query_params.append(request.sent_after_ts)
    if request.sent_before_ts is not None:
        typed_activity_filters.append("m.sent_at <= ?")
        query_params.append(request.sent_before_ts)
    if request.normalized_text_query is not None:
        typed_activity_filters.append("instr(lower(COALESCE(m.text, '')), ?) > 0")
        query_params.append(request.normalized_text_query)
    if request.dialog_kinds != ["all"]:
        dialog_kind_placeholders = ",".join("?" for _ in request.dialog_kinds)
        kinded_activity_filters.append(f"dialog_kind IN ({dialog_kind_placeholders})")
        query_params.extend(request.dialog_kinds)
    query_params.append(request.limit)
    return " AND ".join(typed_activity_filters), " AND ".join(kinded_activity_filters), query_params


def _build_recent_activity_request(req: Mapping[str, object]) -> tuple[_RecentActivityRequest | None, dict | None]:
    since_hours, limit, dialog_kinds, error = _parse_recent_activity_limits(req)
    if error is not None:
        return None, error
    assert since_hours is not None
    assert limit is not None
    assert dialog_kinds is not None
    sent_after_ts, sent_before_ts, normalized_text_query, error = _parse_recent_activity_bounds(req)
    if error is not None:
        return None, error
    sent_after_raw = req.get("sent_after")
    sent_before_raw = req.get("sent_before")

    since_ts = int(time.time()) - since_hours * 3600
    typed_activity_filter_sql, kinded_activity_filter_sql, query_params = _recent_activity_query_parts(
        _RecentActivityQueryParts(
            since_ts=since_ts,
            sent_after_ts=sent_after_ts,
            sent_before_ts=sent_before_ts,
            normalized_text_query=normalized_text_query,
            dialog_kinds=dialog_kinds,
            limit=limit,
        )
    )

    return (
        _RecentActivityRequest(
            since_hours=since_hours,
            limit=limit,
            dialog_kinds=dialog_kinds,
            sent_after=sent_after_raw if isinstance(sent_after_raw, str) else None,
            sent_before=sent_before_raw if isinstance(sent_before_raw, str) else None,
            text_query=normalized_text_query,
            sent_after_ts=sent_after_ts,
            sent_before_ts=sent_before_ts,
            since_ts=since_ts,
            query_params=query_params,
            typed_activity_filter_sql=typed_activity_filter_sql,
            kinded_activity_filter_sql=kinded_activity_filter_sql,
        ),
        None,
    )


def _parse_recent_activity_request(req: Mapping[str, object]) -> tuple[_RecentActivityRequest | None, dict | None]:
    return _build_recent_activity_request(req)


def _where_clause(filters: str) -> str:
    return f"WHERE {filters}" if filters else ""


def _build_recent_activity_rows_query(typed_activity_filter_sql: str, kinded_activity_filter_sql: str) -> str:
    return (
        "WITH typed_activity AS ("
        "SELECT m.dialog_id AS dialog_id, m.message_id AS message_id, "
        "       m.sent_at AS sent_at, m.text AS text, "
        "       COALESCE(e.name, d.name, CAST(m.dialog_id AS TEXT)) AS dialog_name, "
        "       CASE "
        "         WHEN lower(COALESCE(e.type, '')) = 'bot' THEN 'bot' "
        "         WHEN d.type IS NOT NULL AND d.type != '' THEN d.type "
        "         WHEN e.type IS NOT NULL AND e.type != '' THEN e.type "
        "         ELSE 'unknown' "
        "       END AS dialog_type, "
        "       CASE "
        "         WHEN COALESCE(m.reply_count, 0) >= COALESCE(dr.direct_reply_count, 0) "
        "         THEN COALESCE(m.reply_count, 0) "
        "         ELSE COALESCE(dr.direct_reply_count, 0) "
        "       END AS reply_count, "
        "       sd.status AS sync_status "
        "FROM messages m "
        "LEFT JOIN ("
        "  SELECT dialog_id, reply_to_msg_id AS message_id, COUNT(*) AS direct_reply_count "
        "  FROM messages "
        "  WHERE reply_to_msg_id IS NOT NULL AND is_service = 0 AND is_deleted = 0 "
        "  GROUP BY dialog_id, reply_to_msg_id"
        ") dr ON dr.dialog_id = m.dialog_id AND dr.message_id = m.message_id "
        "LEFT JOIN entities e ON e.id = m.dialog_id "
        "LEFT JOIN dialogs d ON d.dialog_id = m.dialog_id "
        "LEFT JOIN synced_dialogs sd ON sd.dialog_id = m.dialog_id "
        # out=1: authored by the account owner.
        # is_service=0: exclude join/leave/group-created system events
        #   — activity_comments never held these rows, so the read
        #   path must not start surfacing them after unification.
        # is_deleted=0: exclude tombstones for messages the user
        #   deleted — same pre-v15 behavior preservation rationale.
        f"{_where_clause(typed_activity_filter_sql)} "
        "), kinded_activity AS ("
        "SELECT ta.*, "
        "       CASE "
        "         WHEN lower(ta.dialog_type) = 'bot' THEN 'bot' "
        "         WHEN lower(ta.dialog_type) = 'user' THEN 'user' "
        "         WHEN lower(ta.dialog_type) = 'forum' THEN 'forum' "
        "         WHEN EXISTS (SELECT 1 FROM topic_metadata tm WHERE tm.dialog_id = ta.dialog_id) THEN 'forum' "
        "         WHEN lower(ta.dialog_type) IN ('group', 'supergroup', 'chat') THEN 'group' "
        "         WHEN lower(ta.dialog_type) = 'channel' THEN 'channel' "
        "         WHEN lower(ta.dialog_type) = 'unknown' AND ta.dialog_id > 0 THEN 'user' "
        "         WHEN lower(ta.dialog_type) = 'unknown' AND ta.dialog_id < 0 THEN 'group' "
        "         ELSE 'unknown' "
        "       END AS dialog_kind "
        "FROM typed_activity ta"
        ") "
        "SELECT * FROM ("
        "SELECT * FROM kinded_activity "
        f"{_where_clause(kinded_activity_filter_sql)} "
        "ORDER BY sent_at DESC, dialog_id DESC, message_id DESC "
        "LIMIT ?"
        ") ORDER BY sent_at ASC, dialog_id ASC, message_id ASC"
    )


class DaemonActivityStatsService:
    """Activity and stats orchestration for daemon-side query handling."""

    def __init__(self, deps: DaemonActivityStatsDeps) -> None:
        self._deps = deps

    async def get_usage_stats(self, req: Mapping[str, object]) -> dict[str, object]:
        since = _coerce_int(req.get("since", int(time.time()) - 30 * 86400), int(time.time()) - 30 * 86400)
        try:
            stats = _query_usage_stats(self._deps.conn.cursor(), since)
            return {"ok": True, "data": stats}
        except Exception as exc:
            self._deps.logger.error("get_usage_stats failed: %s", exc, exc_info=True)
            return {"ok": False, "error": "internal", "message": "internal error"}

    async def get_dialog_stats(self, req: Mapping[str, object]) -> dict[str, object]:
        dialog_id = _coerce_int(req.get("dialog_id", 0), 0)
        dialog_obj = req.get("dialog")
        dialog = dialog_obj if isinstance(dialog_obj, str) else None
        limit = _clamp(_coerce_int(req.get("limit", 5), 5), 1, 20)

        resolved = await self._deps.resolve_dialog_id(dialog_id, dialog)
        if isinstance(resolved, dict):
            return resolved
        dialog_id = resolved
        if not dialog_id:
            return {
                "ok": False,
                "error": "missing_dialog",
                "message": "Either dialog_id or dialog name is required for get_dialog_stats",
            }

        row = cast(tuple[object] | None, self._deps.conn.execute(_SELECT_SYNC_STATUS_SQL, (dialog_id,)).fetchone())
        if row is None or row[0] not in ("synced", "syncing", "access_lost"):
            return {
                "ok": False,
                "error": "not_synced",
                "message": "GetDialogStats requires a synced dialog. Use MarkDialogForSync first.",
            }

        reactions = [
            {"emoji": r[0], "count": int(cast(int | str, r[1]))}
            for r in cast(
                list[tuple[object, object]],
                self._deps.conn.execute(_GET_DIALOG_TOP_REACTIONS_SQL, (dialog_id, limit)).fetchall(),
            )
        ]
        mentions = [
            {"value": r[0], "count": int(cast(int | str, r[1]))}
            for r in cast(
                list[tuple[object, object]],
                self._deps.conn.execute(_GET_DIALOG_TOP_MENTIONS_SQL, (dialog_id, limit)).fetchall(),
            )
        ]
        hashtags = [
            {"value": r[0], "count": int(cast(int | str, r[1]))}
            for r in cast(
                list[tuple[object, object]],
                self._deps.conn.execute(_GET_DIALOG_TOP_HASHTAGS_SQL, (dialog_id, limit)).fetchall(),
            )
        ]
        forwards = [
            {"peer_id": r[0], "name": r[1], "count": int(cast(int | str, r[2]))}
            for r in cast(
                list[tuple[object, object, object]],
                self._deps.conn.execute(_GET_DIALOG_TOP_FORWARDS_SQL, (dialog_id, limit)).fetchall(),
            )
        ]
        return {
            "ok": True,
            "data": {
                "dialog_id": dialog_id,
                "top_reactions": reactions,
                "top_mentions": mentions,
                "top_hashtags": hashtags,
                "top_forwards": forwards,
            },
        }

    async def get_my_recent_activity(self, req: Mapping[str, object]) -> dict[str, object]:
        parsed, error = _parse_recent_activity_request(req)
        if error is not None or parsed is None:
            return error or {"ok": False, "error": "internal", "message": "internal error"}

        rows = cast(
            list[tuple[object, object, object, object, object, object, object, object, object]],
            self._deps.conn.execute(
                _build_recent_activity_rows_query(
                    parsed.typed_activity_filter_sql,
                    parsed.kinded_activity_filter_sql,
                ),
                parsed.query_params,
            ).fetchall(),
        )

        state_rows = dict(
            cast(
                list[tuple[str, str]], self._deps.conn.execute("SELECT key, value FROM activity_sync_state").fetchall()
            )
        )
        backfill_complete = state_rows.get("backfill_complete") == "1"
        backfill_started = state_rows.get("backfill_started_at") is not None
        last_sync_at_str = state_rows.get("last_sync_at")
        last_sync_at: int | None = _coerce_int(last_sync_at_str, 0) if last_sync_at_str else None

        if backfill_complete:
            scan_status = "complete"
        elif backfill_started:
            scan_status = "in_progress"
        else:
            scan_status = "never_run"

        reactions_by_msg: dict[tuple[int, int], list[dict]] = {}
        if rows:
            rx_params: list[int] = []
            for row in rows:
                rx_params.extend([int(cast(int | str, row[0])), int(cast(int | str, row[1]))])
            rx_placeholders = ",".join("(?,?)" for _ in rows)
            for rx in cast(
                list[tuple[object, object, object, object]],
                self._deps.conn.execute(
                    f"SELECT dialog_id, message_id, emoji, count FROM message_reactions "
                    f"WHERE (dialog_id, message_id) IN (VALUES {rx_placeholders}) "
                    f"ORDER BY count DESC",
                    rx_params,
                ).fetchall(),
            ):
                reactions_by_msg.setdefault((int(cast(int | str, rx[0])), int(cast(int | str, rx[1]))), []).append(
                    {"emoji": rx[2], "count": int(cast(int | str, rx[3]))}
                )

        comments = [
            {
                "dialog_id": int(cast(int | str, row[0])),
                "message_id": int(cast(int | str, row[1])),
                "sent_at": int(cast(int | str, row[2])),
                "text": row[3],
                "dialog_name": row[4] or str(row[0]),
                "dialog_type": row[5],
                "dialog_category": row[8],
                "reply_count": int(cast(int | str, row[6])),
                "sync_status": row[7],
                "reactions": reactions_by_msg.get((int(cast(int | str, row[0])), int(cast(int | str, row[1]))), []),
            }
            for row in rows
        ]

        return {
            "ok": True,
            "data": {
                "comments": comments,
                "dialog_kinds": parsed.dialog_kinds,
                "sent_after": parsed.sent_after,
                "sent_before": parsed.sent_before,
                "text_query": parsed.text_query,
                "scan_status": scan_status,
                "scanned_at": last_sync_at,
            },
        }
