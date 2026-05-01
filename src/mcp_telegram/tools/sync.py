import logging

from pydantic import Field

from ._base import (
    DaemonNotRunningError,
    ToolAnnotations,
    ToolArgs,
    ToolResult,
    _check_daemon_response,
    _daemon_not_running_text,
    _text_response,
    daemon_connection,
    error_result,
    mcp_tool,
)

logger = logging.getLogger(__name__)


GET_SYNC_STATUS_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "dialog_id": {"type": ["integer", "null"]},
        "status": {"type": "string"},
        "is_syncing": {"type": "boolean"},
        "last_synced_at": {"type": ["integer", "null"]},
        "message_count": {"type": ["integer", "null"]},
        "action": {"type": ["string", "null"]},
    },
    "required": ["dialog_id", "status", "is_syncing", "last_synced_at", "message_count", "action"],
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
                    "dialog_id": {"type": ["integer", "null"]},
                    "severity": {"type": "string"},
                    "message": {"type": "string"},
                    "action": {"type": ["string", "null"]},
                },
                "required": ["dialog_id", "severity", "message", "action"],
                "additionalProperties": False,
            },
        },
        "count": {"type": "integer"},
    },
    "required": ["alerts", "count"],
    "additionalProperties": False,
}


class MarkDialogForSync(ToolArgs):
    """Mark or unmark a dialog for persistent sync. When marked, full message history
    will be fetched shortly. Unmarking preserves existing synced history but stops
    further sync. Use ListDialogs to find dialog IDs and current sync_status."""

    dialog_id: int = Field(description="Numeric dialog ID from ListDialogs")
    enable: bool = Field(default=True, description="True to start syncing, False to stop")


@mcp_tool(name="mark_dialog_for_sync", title="Mark Sync", annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True))
async def mark_dialog_for_sync(args: MarkDialogForSync) -> ToolResult:
    try:
        async with daemon_connection() as conn:
            response = await conn.mark_dialog_for_sync(
                dialog_id=args.dialog_id,
                enable=args.enable,
            )
    except DaemonNotRunningError:
        return error_result(_daemon_not_running_text())

    if err := _check_daemon_response(response):
        return err

    action = "marked for sync" if args.enable else "unmarked from sync"
    logger.info("mark_dialog_for_sync dialog_id=%d enable=%s", args.dialog_id, args.enable)
    text = f"Dialog {args.dialog_id} {action}."
    if args.enable:
        text += " Full message history will be fetched shortly."
    return ToolResult(content=_text_response(text), result_count=1)


class GetSyncStatus(ToolArgs):
    """Get sync status for a dialog: message count, sync progress, last sync/event timestamps,
    and delete detection reliability. delete_detection is 'reliable (channel)' for channels/supergroups
    (real-time MTProto events) or 'best-effort weekly (DM)' for personal chats (periodic gap scan).
    Works for any dialog — non-synced dialogs return status='not_synced' with zero counts."""

    dialog_id: int = Field(description="Numeric dialog ID from ListDialogs")


@mcp_tool(
    name="get_sync_status",
    title="Sync Status",
    posture="secondary/helper",
    annotations=ToolAnnotations(readOnlyHint=True),
    output_schema=GET_SYNC_STATUS_OUTPUT_SCHEMA,
)
async def get_sync_status(args: GetSyncStatus) -> ToolResult:
    try:
        async with daemon_connection() as conn:
            response = await conn.get_sync_status(dialog_id=args.dialog_id)
    except DaemonNotRunningError:
        return error_result(_daemon_not_running_text())

    if err := _check_daemon_response(response):
        return err

    data = response.get("data", {})
    status = data.get("status") or "unknown"
    structured_content = {
        "dialog_id": data.get("dialog_id"),
        "status": status,
        "is_syncing": status == "syncing",
        "last_synced_at": data.get("last_synced_at"),
        "message_count": data.get("message_count"),
        "action": data.get("action"),
    }
    lines = [
        f"dialog_id={structured_content['dialog_id']}",
        f"status={structured_content['status']}",
        f"message_count={data.get('message_count', 0)}",
        f"sync_progress={data.get('sync_progress', 0)}",
        f"total_messages={data.get('total_messages', 0)}",
        f"last_synced_at={data.get('last_synced_at')}",
        f"last_event_at={data.get('last_event_at')}",
        f"delete_detection={data.get('delete_detection')}",
    ]
    return ToolResult(content=_text_response("\n".join(lines)), structured_content=structured_content, result_count=1)


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


@mcp_tool(
    name="get_sync_alerts",
    title="Sync Alerts",
    posture="secondary/helper",
    annotations=ToolAnnotations(readOnlyHint=True),
    output_schema=GET_SYNC_ALERTS_OUTPUT_SCHEMA,
)
async def get_sync_alerts(args: GetSyncAlerts) -> ToolResult:
    try:
        async with daemon_connection() as conn:
            response = await conn.get_sync_alerts(since=args.since, limit=args.limit)
    except DaemonNotRunningError:
        return error_result(_daemon_not_running_text())

    if err := _check_daemon_response(response):
        return err

    data = response.get("data", {})
    deleted = data.get("deleted_messages", [])
    edits = data.get("edits", [])
    access_lost = data.get("access_lost", [])

    sections: list[str] = []
    alerts: list[dict[str, object]] = []

    if deleted:
        sections.append(f"=== Deleted Messages ({len(deleted)}) ===")
        for d in deleted:
            message = f"Deleted message msg={d['message_id']} deleted_at={d['deleted_at']}"
            sections.append(f"  dialog={d['dialog_id']} msg={d['message_id']} deleted_at={d['deleted_at']}")
            alerts.append(
                {
                    "dialog_id": d.get("dialog_id"),
                    "severity": "medium",
                    "message": message,
                    "action": None,
                }
            )

    if edits:
        sections.append(f"=== Edits ({len(edits)}) ===")
        for e in edits:
            message = f"Edited message msg={e['message_id']} v{e['version']} edit_date={e['edit_date']}"
            sections.append(
                f"  dialog={e['dialog_id']} msg={e['message_id']} v{e['version']} edit_date={e['edit_date']}"
            )
            alerts.append(
                {
                    "dialog_id": e.get("dialog_id"),
                    "severity": "low",
                    "message": message,
                    "action": None,
                }
            )

    if access_lost:
        sections.append(f"=== Access Lost ({len(access_lost)}) ===")
        for a in access_lost:
            sections.append(f"  dialog={a['dialog_id']} lost_at={a.get('access_lost_at')}")
            alerts.append(
                {
                    "dialog_id": a.get("dialog_id"),
                    "severity": "high",
                    "message": f"Access lost at {a.get('access_lost_at')}",
                    "action": "Use get_sync_status for coverage details.",
                }
            )

    structured_content = {"alerts": alerts, "count": len(alerts)}

    if not sections:
        text = "No sync alerts."
        if args.since > 0:
            text += f" (since={args.since})"
        return ToolResult(content=_text_response(text), structured_content=structured_content)

    return ToolResult(content=_text_response("\n".join(sections)), structured_content=structured_content, result_count=len(alerts))
