from __future__ import annotations

import logging

from pydantic import Field

from ._base import (
    DaemonNotRunningError,
    ToolArgs,
    ToolResult,
    _check_daemon_response,
    _daemon_not_running_text,
    _text_response,
    daemon_connection,
    mcp_tool,
)


logger = logging.getLogger(__name__)


class MarkDialogForSync(ToolArgs):
    """Mark or unmark a dialog for persistent sync. When marked, full message history
    will be fetched shortly. Unmarking preserves existing synced history but stops
    further sync. Use ListDialogs to find dialog IDs and current sync_status."""

    dialog_id: int = Field(description="Numeric dialog ID from ListDialogs")
    enable: bool = Field(default=True, description="True to start syncing, False to stop")


@mcp_tool("primary")
async def mark_dialog_for_sync(args: MarkDialogForSync) -> ToolResult:
    try:
        async with daemon_connection() as conn:
            response = await conn.mark_dialog_for_sync(
                dialog_id=args.dialog_id,
                enable=args.enable,
            )
    except DaemonNotRunningError:
        return ToolResult(content=_text_response(_daemon_not_running_text()))

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


@mcp_tool("secondary/helper")
async def get_sync_status(args: GetSyncStatus) -> ToolResult:
    try:
        async with daemon_connection() as conn:
            response = await conn.get_sync_status(dialog_id=args.dialog_id)
    except DaemonNotRunningError:
        return ToolResult(content=_text_response(_daemon_not_running_text()))

    if err := _check_daemon_response(response):
        return err

    data = response.get("data", {})
    lines = [
        f"dialog_id={data.get('dialog_id')}",
        f"status={data.get('status')}",
        f"message_count={data.get('message_count', 0)}",
        f"sync_progress={data.get('sync_progress', 0)}",
        f"total_messages={data.get('total_messages', 0)}",
        f"last_synced_at={data.get('last_synced_at')}",
        f"last_event_at={data.get('last_event_at')}",
        f"delete_detection={data.get('delete_detection')}",
    ]
    return ToolResult(content=_text_response("\n".join(lines)), result_count=1)


class GetSyncAlerts(ToolArgs):
    """Get sync alerts: deleted messages (with preserved text), edit history, and access-lost
    notifications. Use since (unix timestamp) to filter alerts after a specific time.
    Default since=0 returns all alerts. Deleted messages include the last known text before
    deletion. Edit history shows previous versions of edited messages."""

    since: int = Field(default=0, description="Unix timestamp — only return alerts after this time. Default 0 = all.")
    limit: int = Field(default=50, description="Maximum number of deleted messages and edits to return. Default 50.")


@mcp_tool("primary")
async def get_sync_alerts(args: GetSyncAlerts) -> ToolResult:
    try:
        async with daemon_connection() as conn:
            response = await conn.get_sync_alerts(since=args.since, limit=args.limit)
    except DaemonNotRunningError:
        return ToolResult(content=_text_response(_daemon_not_running_text()))

    if err := _check_daemon_response(response):
        return err

    data = response.get("data", {})
    deleted = data.get("deleted_messages", [])
    edits = data.get("edits", [])
    access_lost = data.get("access_lost", [])

    sections: list[str] = []

    if deleted:
        sections.append(f"=== Deleted Messages ({len(deleted)}) ===")
        for d in deleted:
            sections.append(
                f"  dialog={d['dialog_id']} msg={d['message_id']} "
                f"deleted_at={d['deleted_at']}"
            )

    if edits:
        sections.append(f"=== Edits ({len(edits)}) ===")
        for e in edits:
            sections.append(
                f"  dialog={e['dialog_id']} msg={e['message_id']} "
                f"v{e['version']} edit_date={e['edit_date']}"
            )

    if access_lost:
        sections.append(f"=== Access Lost ({len(access_lost)}) ===")
        for a in access_lost:
            sections.append(
                f"  dialog={a['dialog_id']} lost_at={a.get('access_lost_at')}"
            )

    if not sections:
        text = "No sync alerts."
        if args.since > 0:
            text += f" (since={args.since})"
        return ToolResult(content=_text_response(text))

    total = len(deleted) + len(edits) + len(access_lost)
    return ToolResult(content=_text_response("\n".join(sections)), result_count=total)
