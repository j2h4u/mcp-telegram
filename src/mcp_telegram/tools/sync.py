import logging

from pydantic import Field

from ._base import (
    DaemonNotRunningError,
    ToolAnnotations,
    ToolArgs,
    ToolResult,
    _check_daemon_response,
    _daemon_not_running_text,
    daemon_connection,
    error_result,
    mcp_tool,
    structured_result,
)

logger = logging.getLogger(__name__)
_HISTORY_SCOPE_BY_STATUS = {
    "synced": "full",
    "syncing": "full",
    "own_only": "own_only",
    "fragment": "fragment",
    "access_lost": "access_lost",
}
_HISTORY_DEPTH_BY_STATUS = {
    "synced": "complete",
    "syncing": "partial",
    "own_only": "partial",
    "fragment": "partial",
    "access_lost": "partial",
    "not_synced": "none",
}
_HISTORY_SYNC_BY_STATUS = {
    "synced": "complete_as_of_last_sync",
    "syncing": "syncing",
    "own_only": "own_messages_only",
    "fragment": "fragment_only",
    "access_lost": "access_lost_archive",
    "not_synced": "not_synced",
}


MARK_DIALOG_FOR_SYNC_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "dialog_id": {"type": "integer"},
        "enabled": {"type": "boolean"},
        "status": {"type": "string"},
        "action": {"type": "string"},
        "expected_next_state": {"type": "string"},
        "full_history_will_be_fetched": {"type": "boolean"},
    },
    "required": [
        "dialog_id",
        "enabled",
        "status",
        "action",
        "expected_next_state",
        "full_history_will_be_fetched",
    ],
    "additionalProperties": False,
}


GET_SYNC_STATUS_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "dialog_id": {"type": ["integer", "null"]},
        "status": {"type": "string"},
        "raw_status": {"type": "string"},
        "is_syncing": {"type": "boolean"},
        "last_synced_at": {"type": ["integer", "null"]},
        "last_event_at": {"type": ["integer", "null"]},
        "last_delta_checked_at": {"type": ["integer", "null"]},
        "delta_refresh_requested_at": {"type": ["integer", "null"]},
        "message_count": {"type": ["integer", "null"]},
        "saved_message_count": {"type": ["integer", "null"]},
        "history_scope": {"type": "string"},
        "history_depth_state": {"type": "string"},
        "history_sync_state": {"type": "string"},
        "history_complete_at": {"type": ["integer", "null"]},
        "coverage_state": {"type": "string"},
        "local_knowledge_at": {"type": ["integer", "null"]},
        "local_knowledge_age_seconds": {"type": ["integer", "null"]},
        "sync_progress": {"type": ["integer", "null"]},
        "sync_progress_message_id": {"type": ["integer", "null"]},
        "total_messages": {"type": ["integer", "null"]},
        "delete_detection": {"type": ["string", "null"]},
        "sync_coverage_pct": {"type": ["integer", "null"]},
        "access_lost_at": {"type": ["integer", "null"]},
        "access_last_revalidated_at": {"type": ["integer", "null"]},
        "access_next_revalidate_at": {"type": ["integer", "null"]},
        "action": {"type": ["string", "null"]},
    },
    "required": [
        "dialog_id",
        "status",
        "raw_status",
        "is_syncing",
        "last_synced_at",
        "last_event_at",
        "last_delta_checked_at",
        "delta_refresh_requested_at",
        "message_count",
        "saved_message_count",
        "history_scope",
        "history_depth_state",
        "history_sync_state",
        "history_complete_at",
        "coverage_state",
        "local_knowledge_at",
        "local_knowledge_age_seconds",
        "sync_progress",
        "sync_progress_message_id",
        "total_messages",
        "delete_detection",
        "sync_coverage_pct",
        "access_lost_at",
        "access_last_revalidated_at",
        "access_next_revalidate_at",
        "action",
    ],
    "additionalProperties": False,
}


GET_SYNC_ALERTS_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "alerts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string"},
                    "dialog_id": {"type": ["integer", "null"]},
                    "message_id": {"type": ["integer", "null"]},
                    "deleted_at": {"type": ["integer", "null"]},
                    "version": {"type": ["integer", "null"]},
                    "edit_date": {"type": ["integer", "null"]},
                    "access_lost_at": {"type": ["integer", "null"]},
                    "severity": {"type": "string"},
                    "message": {"type": "string"},
                    "action": {"type": ["string", "null"]},
                },
                "required": [
                    "kind",
                    "dialog_id",
                    "message_id",
                    "deleted_at",
                    "version",
                    "edit_date",
                    "access_lost_at",
                    "severity",
                    "message",
                    "action",
                ],
                "additionalProperties": False,
            },
        },
        "deleted_messages": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "dialog_id": {"type": ["integer", "null"]},
                    "message_id": {"type": ["integer", "null"]},
                    "deleted_at": {"type": ["integer", "null"]},
                    "action": {"type": "string"},
                },
                "required": ["dialog_id", "message_id", "deleted_at", "action"],
                "additionalProperties": False,
            },
        },
        "edits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "dialog_id": {"type": ["integer", "null"]},
                    "message_id": {"type": ["integer", "null"]},
                    "version": {"type": ["integer", "null"]},
                    "edit_date": {"type": ["integer", "null"]},
                    "action": {"type": "string"},
                },
                "required": ["dialog_id", "message_id", "version", "edit_date", "action"],
                "additionalProperties": False,
            },
        },
        "access_lost": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "dialog_id": {"type": ["integer", "null"]},
                    "access_lost_at": {"type": ["integer", "null"]},
                    "action": {"type": "string"},
                },
                "required": ["dialog_id", "access_lost_at", "action"],
                "additionalProperties": False,
            },
        },
        "counts": {
            "type": "object",
            "properties": {
                "deleted_messages": {"type": "integer"},
                "edits": {"type": "integer"},
                "access_lost": {"type": "integer"},
                "total": {"type": "integer"},
            },
            "required": ["deleted_messages", "edits", "access_lost", "total"],
            "additionalProperties": False,
        },
        "count": {"type": "integer"},
        "since": {"type": "integer"},
        "limit": {"type": "integer"},
        "limited_by": {
            "type": "object",
            "properties": {
                "deleted_messages": {
                    "type": "object",
                    "properties": {
                        "since": {"type": "integer"},
                        "limit": {"type": "integer"},
                    },
                    "required": ["since", "limit"],
                    "additionalProperties": False,
                },
                "edits": {
                    "type": "object",
                    "properties": {
                        "since": {"type": "integer"},
                        "limit": {"type": "integer"},
                    },
                    "required": ["since", "limit"],
                    "additionalProperties": False,
                },
                "access_lost": {
                    "type": "object",
                    "properties": {
                        "since": {"type": "integer"},
                        "limit": {"type": "null"},
                    },
                    "required": ["since", "limit"],
                    "additionalProperties": False,
                },
            },
            "required": ["deleted_messages", "edits", "access_lost"],
            "additionalProperties": False,
        },
    },
    "required": [
        "alerts",
        "deleted_messages",
        "edits",
        "access_lost",
        "counts",
        "count",
        "since",
        "limit",
        "limited_by",
    ],
    "additionalProperties": False,
}


class MarkDialogForSync(ToolArgs):
    """Mark or unmark a dialog for persistent sync. When marked, full message history
    will be fetched shortly. Unmarking preserves existing synced history but stops
    further sync. Use ListDialogs to find dialog IDs and current sync_status."""

    dialog_id: int = Field(description="Numeric dialog ID from ListDialogs")
    enable: bool = Field(default=True, description="True to start syncing, False to stop")


@mcp_tool(
    name="mark_dialog_for_sync",
    title="Mark Sync",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
    output_schema=MARK_DIALOG_FOR_SYNC_OUTPUT_SCHEMA,
)
async def mark_dialog_for_sync(args: MarkDialogForSync) -> ToolResult:
    try:
        async with daemon_connection() as conn:
            response = await conn.mark_dialog_for_sync(
                dialog_id=args.dialog_id,
                enable=args.enable,
            )
    except DaemonNotRunningError as exc:
        return error_result(_daemon_not_running_text(exc))

    if err := _check_daemon_response(response):
        return err

    logger.info("mark_dialog_for_sync dialog_id=%d enable=%s", args.dialog_id, args.enable)
    data = response.get("data", {})
    structured_content = {
        "dialog_id": args.dialog_id,
        "enabled": args.enable,
        "status": "accepted",
        "action": data.get("action", "mark_for_sync" if args.enable else "unmark_from_sync"),
        "expected_next_state": data.get("expected_next_state", "syncing" if args.enable else "not_synced"),
        "full_history_will_be_fetched": data.get("full_history_will_be_fetched", args.enable),
    }
    return structured_result(structured_content, result_count=1)


class GetSyncStatus(ToolArgs):
    """Get sync status for a dialog: message count, sync progress, last sync/event timestamps,
    and delete detection reliability. delete_detection is 'reliable (channel)' for channels/supergroups
    (real-time MTProto events) or 'best-effort weekly (DM)' for personal chats (periodic gap scan).
    sync_progress is the raw message_id offset cursor, not a row count. Works for any dialog —
    non-synced dialogs return status='not_synced' with zero counts."""

    dialog_id: int = Field(description="Numeric dialog ID from ListDialogs")


@mcp_tool(
    name="get_sync_status",
    title="Sync Status",
    posture="secondary/helper",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
    output_schema=GET_SYNC_STATUS_OUTPUT_SCHEMA,
)
async def get_sync_status(args: GetSyncStatus) -> ToolResult:
    try:
        async with daemon_connection() as conn:
            response = await conn.get_sync_status(dialog_id=args.dialog_id)
    except DaemonNotRunningError as exc:
        return error_result(_daemon_not_running_text(exc))

    if err := _check_daemon_response(response):
        return err

    data = response.get("data", {})
    status = data.get("status") or "unknown"
    message_count = data.get("message_count")
    total_messages = data.get("total_messages")
    saved_message_count = data.get("saved_message_count", message_count)
    coverage_state = data.get("coverage_state", _coverage_state(saved_message_count, total_messages))
    last_synced_at = data.get("last_synced_at")
    last_event_at = data.get("last_event_at")
    last_delta_checked_at = data.get("last_delta_checked_at")
    delta_refresh_requested_at = data.get("delta_refresh_requested_at")
    local_knowledge_at = data.get(
        "local_knowledge_at",
        _local_knowledge_at(last_synced_at, last_event_at, last_delta_checked_at),
    )
    sync_progress_message_id = data.get("sync_progress_message_id", data.get("sync_progress"))
    structured_content = {
        "dialog_id": data.get("dialog_id"),
        "status": status,
        "raw_status": data.get("raw_status", status),
        "is_syncing": status == "syncing",
        "last_synced_at": last_synced_at,
        "last_event_at": last_event_at,
        "last_delta_checked_at": last_delta_checked_at,
        "delta_refresh_requested_at": delta_refresh_requested_at,
        "message_count": message_count,
        "saved_message_count": saved_message_count,
        "history_scope": data.get("history_scope", _history_scope(status)),
        "history_depth_state": data.get("history_depth_state", _history_depth_state(status)),
        "history_sync_state": data.get("history_sync_state", _history_sync_state(status)),
        "history_complete_at": data.get("history_complete_at", last_synced_at if status == "synced" else None),
        "coverage_state": coverage_state,
        "local_knowledge_at": local_knowledge_at,
        "local_knowledge_age_seconds": data.get("local_knowledge_age_seconds"),
        "sync_progress": data.get("sync_progress"),
        "sync_progress_message_id": sync_progress_message_id,
        "total_messages": total_messages,
        "delete_detection": data.get("delete_detection"),
        "sync_coverage_pct": data.get("sync_coverage_pct"),
        "access_lost_at": data.get("access_lost_at"),
        "access_last_revalidated_at": data.get("access_last_revalidated_at"),
        "access_next_revalidate_at": data.get("access_next_revalidate_at"),
        "action": _sync_status_action(status, message_count, total_messages, coverage_state),
    }
    return structured_result(structured_content, result_count=1)


def _coverage_state(message_count: object, total_messages: object) -> str:
    if total_messages is None:
        return "telegram_total_unknown"
    if not isinstance(total_messages, int) or total_messages < 0:
        return "telegram_total_invalid"
    if isinstance(message_count, int) and message_count > total_messages:
        return "telegram_total_not_comparable"
    return "telegram_total_comparable"


def _local_knowledge_at(
    last_synced_at: object,
    last_event_at: object,
    last_delta_checked_at: object = None,
) -> object | None:
    timestamps = [ts for ts in (last_synced_at, last_event_at, last_delta_checked_at) if isinstance(ts, int)]
    return max(timestamps) if timestamps else None


def _history_scope(status: object) -> str:
    return _HISTORY_SCOPE_BY_STATUS.get(status, "none") if isinstance(status, str) else "none"


def _history_depth_state(status: object) -> str:
    return _HISTORY_DEPTH_BY_STATUS.get(status, "unknown") if isinstance(status, str) else "unknown"


def _history_sync_state(status: object) -> str:
    return _HISTORY_SYNC_BY_STATUS.get(status, "unknown") if isinstance(status, str) else "unknown"


def _sync_status_action(status: object, message_count: object, total_messages: object, coverage_state: object) -> str:
    parts = [_history_action(status), "sync_progress is a message_id offset, not a count."]
    parts.append(_sync_coverage_action(message_count, total_messages))
    if coverage_state == "telegram_total_not_comparable":
        parts.append("Stored Telegram total_messages is suspect; percentage coverage is diagnostic-only.")
    return " ".join(parts)


def _history_action(status: object) -> str:
    if status == "synced":
        return "Full history was fetched as of last_synced_at; ongoing freshness is represented by local_knowledge_at."
    if status == "own_only":
        return "Only own-message-related history is stored for this dialog."
    if status == "not_synced":
        return "This dialog is not enrolled for history sync."
    if status == "access_lost":
        return "This dialog is an access-lost local archive, not a current live mirror."
    return "History sync state is represented by history_sync_state."


def _sync_coverage_action(message_count: object, total_messages: object) -> str:
    if total_messages is None:
        return "Coverage is unknown without Telegram total_messages."
    if isinstance(message_count, int) and isinstance(total_messages, int) and message_count > total_messages:
        return "Local message_count exceeds Telegram total_messages, so coverage is not comparable."
    if total_messages == 0:
        return "Empty dialogs are complete; non-empty local counts would be inconsistent."
    return "Treat sync_coverage_pct as an approximate local-vs-Telegram ratio."


class GetSyncAlerts(ToolArgs):
    """Audit what changed in synced dialogs: deleted messages (text preserved), edit history,
    and dialogs where access was lost after syncing.

    Use when investigating anomalies — e.g. after GetSyncStatus shows access_lost, or to
    audit what was deleted or silently edited since a given timestamp.
    Use since= (unix timestamp) to scope alerts to a time window. Default since=0 returns all.
    Deleted messages include the last known text before deletion.
    Edit history shows previous versions of edited messages."""

    since: int = Field(default=0, description="Unix timestamp — only return alerts after this time. Default 0 = all.")
    limit: int = Field(default=50, description="Maximum deleted messages and edits to return. Default 50.")


def _as_int(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def _alert_timestamp(alert: dict[str, object]) -> tuple[int, int, int, str]:
    timestamp = alert.get("deleted_at") or alert.get("edit_date") or alert.get("access_lost_at") or 0
    return (
        _as_int(timestamp),
        _as_int(alert.get("dialog_id")),
        _as_int(alert.get("message_id")),
        str(alert.get("kind") or ""),
    )


@mcp_tool(
    name="get_sync_alerts",
    title="Sync Alerts",
    posture="secondary/helper",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
    output_schema=GET_SYNC_ALERTS_OUTPUT_SCHEMA,
)
async def get_sync_alerts(args: GetSyncAlerts) -> ToolResult:
    try:
        async with daemon_connection() as conn:
            response = await conn.get_sync_alerts(since=args.since, limit=args.limit)
    except DaemonNotRunningError as exc:
        return error_result(_daemon_not_running_text(exc))

    if err := _check_daemon_response(response):
        return err

    data = response.get("data", {})
    deleted = data.get("deleted_messages", [])
    edits = data.get("edits", [])
    access_lost = data.get("access_lost", [])

    alerts: list[dict[str, object]] = []
    deleted_messages: list[dict[str, object]] = []
    edit_alerts: list[dict[str, object]] = []
    access_lost_alerts: list[dict[str, object]] = []

    if deleted:
        for d in deleted:
            message = f"Deleted message msg={d['message_id']} deleted_at={d['deleted_at']}"
            action = "Inspect the dialog history around this message id if surrounding context is needed."
            deleted_messages.append(
                {
                    "dialog_id": d.get("dialog_id"),
                    "message_id": d.get("message_id"),
                    "deleted_at": d.get("deleted_at"),
                    "action": action,
                }
            )
            alerts.append(
                {
                    "kind": "deleted_message",
                    "dialog_id": d.get("dialog_id"),
                    "message_id": d.get("message_id"),
                    "deleted_at": d.get("deleted_at"),
                    "version": None,
                    "edit_date": None,
                    "access_lost_at": None,
                    "severity": "medium",
                    "message": message,
                    "action": action,
                }
            )

    if edits:
        for e in edits:
            message = f"Edited message msg={e['message_id']} v{e['version']} edit_date={e['edit_date']}"
            action = "Treat cached text as versioned; inspect edit history before relying on older wording."
            edit_alerts.append(
                {
                    "dialog_id": e.get("dialog_id"),
                    "message_id": e.get("message_id"),
                    "version": e.get("version"),
                    "edit_date": e.get("edit_date"),
                    "action": action,
                }
            )
            alerts.append(
                {
                    "kind": "edit",
                    "dialog_id": e.get("dialog_id"),
                    "message_id": e.get("message_id"),
                    "deleted_at": None,
                    "version": e.get("version"),
                    "edit_date": e.get("edit_date"),
                    "access_lost_at": None,
                    "severity": "low",
                    "message": message,
                    "action": action,
                }
            )

    if access_lost:
        for a in access_lost:
            action = "Use get_sync_status for coverage details."
            access_lost_alerts.append(
                {
                    "dialog_id": a.get("dialog_id"),
                    "access_lost_at": a.get("access_lost_at"),
                    "action": action,
                }
            )
            alerts.append(
                {
                    "kind": "access_lost",
                    "dialog_id": a.get("dialog_id"),
                    "message_id": None,
                    "deleted_at": None,
                    "version": None,
                    "edit_date": None,
                    "access_lost_at": a.get("access_lost_at"),
                    "severity": "high",
                    "message": f"Access lost at {a.get('access_lost_at')}",
                    "action": action,
                }
            )

    deleted_messages.sort(key=_alert_timestamp)
    edit_alerts.sort(key=_alert_timestamp)
    access_lost_alerts.sort(key=_alert_timestamp)
    alerts.sort(key=_alert_timestamp)

    structured_content = {
        "alerts": alerts,
        "deleted_messages": deleted_messages,
        "edits": edit_alerts,
        "access_lost": access_lost_alerts,
        "counts": {
            "deleted_messages": len(deleted_messages),
            "edits": len(edit_alerts),
            "access_lost": len(access_lost_alerts),
            "total": len(alerts),
        },
        "count": len(alerts),
        "since": args.since,
        "limit": args.limit,
        "limited_by": {
            "deleted_messages": {"since": args.since, "limit": args.limit},
            "edits": {"since": args.since, "limit": args.limit},
            "access_lost": {"since": args.since, "limit": None},
        },
    }

    return structured_result(structured_content, result_count=len(alerts))
