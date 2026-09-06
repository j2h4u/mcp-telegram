import logging
from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import Field

from ..sync_read_model import (
    CoverageState,
    HistoryDepthState,
    HistoryScope,
    HistorySyncState,
    RealtimeHistory,
    SyncReadModel,
    SyncReadModelContractError,
    SyncStatus,
    decode_sync_read_model,
)
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


MARK_DIALOG_FOR_SYNC_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "dialog_id": {"type": "integer"},
        "enabled": {"type": "boolean"},
        "enrollment_source": {"type": "string"},
        "coverage_status": {"type": ["string", "null"]},
        "action": {"type": "string"},
        "blocked_reason": {"type": ["string", "null"]},
        "full_history_will_be_fetched": {"type": "boolean"},
    },
    "required": [
        "dialog_id",
        "enabled",
        "enrollment_source",
        "coverage_status",
        "action",
        "blocked_reason",
        "full_history_will_be_fetched",
    ],
    "additionalProperties": False,
}


GET_SYNC_STATUS_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "dialog_id": {"type": "integer"},
        "coverage_status": {"type": "string", "enum": [item.value for item in SyncStatus]},
        "enrollment_enabled": {"type": ["boolean", "null"]},
        "enrollment_source": {"type": ["string", "null"]},
        "realtime_history": {"type": "string", "enum": [item.value for item in RealtimeHistory]},
        "is_syncing": {"type": "boolean"},
        "last_synced_at": {"type": ["integer", "null"]},
        "last_event_at": {"type": ["integer", "null"]},
        "last_delta_checked_at": {"type": ["integer", "null"]},
        "delta_refresh_requested_at": {"type": ["integer", "null"]},
        "message_count": {"type": "integer"},
        "saved_message_count": {"type": "integer"},
        "history_scope": {"type": "string", "enum": [item.value for item in HistoryScope]},
        "history_depth_state": {"type": "string", "enum": [item.value for item in HistoryDepthState]},
        "history_sync_state": {"type": "string", "enum": [item.value for item in HistorySyncState]},
        "history_complete_at": {"type": ["integer", "null"]},
        "coverage_state": {"type": "string", "enum": [item.value for item in CoverageState]},
        "local_knowledge_at": {"type": ["integer", "null"]},
        "local_knowledge_age_seconds": {"type": ["integer", "null"]},
        "sync_progress": {"type": ["integer", "null"]},
        "sync_progress_message_id": {"type": ["integer", "null"]},
        "total_messages": {"type": ["integer", "null"]},
        "delete_detection": {"type": "string"},
        "sync_coverage_pct": {"type": ["integer", "null"]},
        "access_lost_at": {"type": ["integer", "null"]},
        "access_last_revalidated_at": {"type": ["integer", "null"]},
        "access_next_revalidate_at": {"type": ["integer", "null"]},
        "action": {"type": "string"},
    },
    "required": [
        "dialog_id",
        "coverage_status",
        "enrollment_enabled",
        "enrollment_source",
        "realtime_history",
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


@dataclass(frozen=True, slots=True)
class _GetSyncStatusSurface:
    model: SyncReadModel
    dialog_id: int
    enrollment_source: str | None
    delta_refresh_requested_at: int | None
    sync_progress: int | None
    sync_progress_message_id: int | None
    delete_detection: str
    access_lost_at: int | None
    access_last_revalidated_at: int | None
    access_next_revalidate_at: int | None


def _surface_required(data: Mapping[str, object], name: str) -> object:
    if name not in data:
        raise SyncReadModelContractError(f"missing get_sync_status field: {name}")
    return data[name]


def _surface_optional_int(data: Mapping[str, object], name: str) -> int | None:
    value = _surface_required(data, name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise SyncReadModelContractError(f"{name} must be an integer or null")
    return value


def _surface_int(data: Mapping[str, object], name: str) -> int:
    value = _surface_required(data, name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise SyncReadModelContractError(f"{name} must be an integer")
    return value


def _surface_optional_string(data: Mapping[str, object], name: str) -> str | None:
    value = _surface_required(data, name)
    if value is None or isinstance(value, str):
        return value
    raise SyncReadModelContractError(f"{name} must be a string or null")


def _surface_string(data: Mapping[str, object], name: str) -> str:
    value = _surface_required(data, name)
    if not isinstance(value, str):
        raise SyncReadModelContractError(f"{name} must be a string")
    return value


def _decode_get_sync_status_surface(
    data: Mapping[str, object],
    *,
    expected_dialog_id: int,
) -> _GetSyncStatusSurface:
    dialog_id = _surface_int(data, "dialog_id")
    if dialog_id != expected_dialog_id:
        raise SyncReadModelContractError(
            f"dialog_id does not match request: expected {expected_dialog_id}, got {dialog_id}"
        )
    return _GetSyncStatusSurface(
        model=decode_sync_read_model(data),
        dialog_id=dialog_id,
        enrollment_source=_surface_optional_string(data, "enrollment_source"),
        delta_refresh_requested_at=_surface_optional_int(data, "delta_refresh_requested_at"),
        sync_progress=_surface_optional_int(data, "sync_progress"),
        sync_progress_message_id=_surface_optional_int(data, "sync_progress_message_id"),
        delete_detection=_surface_string(data, "delete_detection"),
        access_lost_at=_surface_optional_int(data, "access_lost_at"),
        access_last_revalidated_at=_surface_optional_int(data, "access_last_revalidated_at"),
        access_next_revalidate_at=_surface_optional_int(data, "access_next_revalidate_at"),
    )


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
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
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
        "enrollment_source": data["enrollment_source"],
        "coverage_status": data["coverage_status"],
        "action": data["action"],
        "blocked_reason": data["blocked_reason"],
        "full_history_will_be_fetched": data["full_history_will_be_fetched"],
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
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
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

    data = response.get("data")
    if not isinstance(data, Mapping):
        return _sync_read_model_error("data must be an object")
    try:
        surface = _decode_get_sync_status_surface(data, expected_dialog_id=args.dialog_id)
    except SyncReadModelContractError as exc:
        return _sync_read_model_error(str(exc))

    wire = surface.model.to_wire()
    structured_content = {
        "dialog_id": surface.dialog_id,
        "coverage_status": wire["sync_status"],
        "enrollment_enabled": wire["enrollment_enabled"],
        "enrollment_source": surface.enrollment_source,
        "realtime_history": wire["realtime_history"],
        "is_syncing": wire["is_syncing"],
        "last_synced_at": wire["last_synced_at"],
        "last_event_at": wire["last_event_at"],
        "last_delta_checked_at": wire["last_delta_checked_at"],
        "delta_refresh_requested_at": surface.delta_refresh_requested_at,
        "message_count": wire["saved_message_count"],
        "saved_message_count": wire["saved_message_count"],
        "history_scope": wire["history_scope"],
        "history_depth_state": wire["history_depth_state"],
        "history_sync_state": wire["history_sync_state"],
        "history_complete_at": wire["history_complete_at"],
        "coverage_state": wire["coverage_state"],
        "local_knowledge_at": wire["local_knowledge_at"],
        "local_knowledge_age_seconds": wire["local_knowledge_age_seconds"],
        "sync_progress": surface.sync_progress,
        "sync_progress_message_id": surface.sync_progress_message_id,
        "total_messages": wire["total_messages"],
        "delete_detection": surface.delete_detection,
        "sync_coverage_pct": wire["sync_coverage_pct"],
        "access_lost_at": surface.access_lost_at,
        "access_last_revalidated_at": surface.access_last_revalidated_at,
        "access_next_revalidate_at": surface.access_next_revalidate_at,
        "action": wire["action"],
    }
    return structured_result(structured_content, result_count=1)


def _sync_read_model_error(detail: str) -> ToolResult:
    return error_result(
        f"Error: daemon_protocol_error: invalid canonical sync read model ({detail}).\n"
        "Action: Restart the daemon with the same mcp-telegram build, then retry GetSyncStatus."
    )
