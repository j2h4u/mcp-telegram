"""Activity and stats service extracted from daemon_api."""

import sqlite3
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import TracebackType
from typing import Protocol, cast

type _ExcInfoType = (
    bool
    | BaseException
    | tuple[type[BaseException], BaseException, TracebackType | None]
    | tuple[None, None, None]
    | None
)


class _LoggerLike(Protocol):
    def debug(
        self,
        msg: str,
        *args: object,
        exc_info: _ExcInfoType = None,
        stack_info: bool = False,
        stacklevel: int = 1,
        extra: Mapping[str, object] | None = None,
    ) -> None: ...

    def info(
        self,
        msg: str,
        *args: object,
        exc_info: _ExcInfoType = None,
        stack_info: bool = False,
        stacklevel: int = 1,
        extra: Mapping[str, object] | None = None,
    ) -> None: ...

    def warning(
        self,
        msg: str,
        *args: object,
        exc_info: _ExcInfoType = None,
        stack_info: bool = False,
        stacklevel: int = 1,
        extra: Mapping[str, object] | None = None,
    ) -> None: ...

    def error(
        self,
        msg: str,
        *args: object,
        exc_info: _ExcInfoType = None,
        stack_info: bool = False,
        stacklevel: int = 1,
        extra: Mapping[str, object] | None = None,
    ) -> None: ...

    def exception(
        self,
        msg: str,
        *args: object,
        exc_info: _ExcInfoType = None,
        stack_info: bool = False,
        stacklevel: int = 1,
        extra: Mapping[str, object] | None = None,
    ) -> None: ...


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
    since_ts: int
    query_params: list[object]
    dialog_kind_filter_sql: str


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


def _parse_recent_activity_request(req: Mapping[str, object]) -> tuple[_RecentActivityRequest | None, dict | None]:
    since_hours = _coerce_int(req.get("since_hours", 168), 168)
    since_hours = _clamp(since_hours, 1, 8760)

    limit = _coerce_int(req.get("limit", 500), 500)
    limit = _clamp(limit, 1, 2000)

    dialog_kinds, dialog_kind_error = _normalize_activity_dialog_kinds(
        req.get("dialog_kinds", list(DEFAULT_ACTIVITY_DIALOG_KINDS))
    )
    if dialog_kind_error is not None or dialog_kinds is None:
        return None, {
            "ok": False,
            "error": "invalid_dialog_kinds",
            "message": dialog_kind_error or "invalid dialog_kinds",
        }

    since_ts = int(time.time()) - since_hours * 3600
    dialog_kind_filter_sql = ""
    query_params: list[object] = [since_ts]
    if dialog_kinds != ["all"]:
        dialog_kind_placeholders = ",".join("?" for _ in dialog_kinds)
        dialog_kind_filter_sql = f"WHERE dialog_kind IN ({dialog_kind_placeholders}) "
        query_params.extend(dialog_kinds)
    query_params.append(limit)

    return (
        _RecentActivityRequest(
            since_hours=since_hours,
            limit=limit,
            dialog_kinds=dialog_kinds,
            since_ts=since_ts,
            query_params=query_params,
            dialog_kind_filter_sql=dialog_kind_filter_sql,
        ),
        None,
    )


def _build_recent_activity_rows_query(dialog_kind_filter_sql: str) -> str:
    return (
        "WITH typed_activity AS ("
        "SELECT m.dialog_id AS dialog_id, m.message_id AS message_id, "
        "       m.sent_at AS sent_at, m.text AS text, "
        "       e.name AS dialog_name, "
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
        "WHERE m.out = 1 AND m.is_service = 0 AND m.is_deleted = 0 "
        "  AND m.sent_at >= ? "
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
        f"{dialog_kind_filter_sql}"
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
                _build_recent_activity_rows_query(parsed.dialog_kind_filter_sql),
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
                "scan_status": scan_status,
                "scanned_at": last_sync_at,
            },
        }
