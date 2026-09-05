import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TypedDict

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


class _GetSyncAlertsKwargs(TypedDict, total=False):
    since: int
    limit: int
    page_limit: int
    navigation: str


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
                    "occurred_at": {"type": ["integer", "null"]},
                    "source_id": {"type": ["integer", "null"]},
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
                    "occurred_at",
                    "source_id",
                    "severity",
                    "message",
                    "action",
                ],
                "additionalProperties": False,
            },
        },
        "deleted_messages": {
            "description": "Deprecated compatibility projection; use alerts.",
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
            "description": "Deprecated compatibility projection; use alerts.",
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
            "description": "Deprecated compatibility projection; use alerts.",
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
            "description": "Deprecated compatibility projection; counts apply to the current alerts page.",
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
        "count": {"type": "integer", "description": "Deprecated compatibility count for the current alerts page."},
        "since": {"type": "integer"},
        "limit": {"type": "integer", "description": "Deprecated page-size alias; use page_limit."},
        "page_limit": {"type": "integer", "description": "Effective page size."},
        "has_more": {"type": "boolean"},
        "next_navigation": {"type": ["string", "null"]},
        "snapshot_upper_event_at": {"type": "integer"},
        "result_count_semantics": {"type": "string"},
        "page_depth": {"type": "integer"},
        "limited_by": {
            "type": "object",
            "properties": {
                "deleted_messages": {
                    "description": "Deprecated compatibility projection; use alerts.",
                    "type": "object",
                    "properties": {
                        "since": {"type": "integer"},
                        "limit": {"type": "integer"},
                    },
                    "required": ["since", "limit"],
                    "additionalProperties": False,
                },
                "edits": {
                    "description": "Deprecated compatibility projection; use alerts.",
                    "type": "object",
                    "properties": {
                        "since": {"type": "integer"},
                        "limit": {"type": "integer"},
                    },
                    "required": ["since", "limit"],
                    "additionalProperties": False,
                },
                "access_lost": {
                    "description": "Deprecated compatibility projection; use alerts.",
                    "type": "object",
                    "properties": {
                        "since": {"type": "integer"},
                        "limit": {"type": ["integer", "null"]},
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


class GetSyncAlerts(ToolArgs):
    """Audit what changed in synced dialogs: deleted messages, edit history,
    and dialogs where access was lost after syncing.

    Use when investigating anomalies — e.g. after GetSyncStatus shows access_lost, or to
    audit what was deleted or silently edited since a given timestamp.
    Use since= (unix timestamp) to scope alerts to a time window. Default since=0 starts a full paginated traversal.
    The MCP response intentionally exposes metadata only (IDs, timestamps,
    kind, severity, and action); message text and prior text are not returned.
    New events above a cursor snapshot are excluded; late historical backfills
    at or before that snapshot can be omitted from an in-progress traversal.
    Results follow immutable newest-observed sequence order; occurred_at remains
    the source fact time, so historical reconstruction is deterministic."""

    since: int = Field(
        default=0,
        ge=0,
        strict=True,
        description=(
            "Unix timestamp — only return alerts after this time. Must be >= 0; default 0 starts a full paginated traversal. "
            "New events above a cursor snapshot are excluded; late historical backfills at or before it can be omitted. "
            "Continue with next_navigation until has_more is false."
        ),
    )
    limit: int = Field(
        default=50,
        ge=1,
        le=500,
        strict=True,
        description="Deprecated page-size alias; must be 1..500. Use page_limit for new callers.",
    )
    page_limit: int | None = Field(
        default=None,
        ge=1,
        le=500,
        strict=True,
        description="Optional canonical page size, 1..500. When supplied it is effective.",
    )
    navigation: str | None = Field(
        default=None,
        description="Opaque daemon-signed cursor returned as next_navigation.",
    )


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
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
    output_schema=GET_SYNC_ALERTS_OUTPUT_SCHEMA,
)
async def get_sync_alerts(args: GetSyncAlerts) -> ToolResult:  # noqa: PLR0912, PLR0914, PLR0915 - legacy and canonical wire projections
    error_metadata = {
        "result_count": 0,
        "has_cursor": args.navigation is not None,
        "page_depth": 1,
        "has_filter": args.since > 0,
    }
    try:
        async with daemon_connection() as conn:
            supplied = args.model_fields_set
            kwargs: _GetSyncAlertsKwargs = {}
            if "since" in supplied:
                kwargs["since"] = args.since
            if "limit" in supplied:
                kwargs["limit"] = args.limit
            if "page_limit" in supplied and args.page_limit is not None:
                kwargs["page_limit"] = args.page_limit
            if "navigation" in supplied and args.navigation is not None:
                kwargs["navigation"] = args.navigation
            response = await conn.get_sync_alerts(**kwargs)
    except DaemonNotRunningError as exc:
        return error_result(_daemon_not_running_text(exc), **error_metadata)

    if response.get("error") == "invalid_navigation":
        err = _check_daemon_response(
            response,
            action="Retry get_sync_alerts without navigation to start a new traversal; repeat the same since value when one was supplied.",
            **error_metadata,
        )
    else:
        err = _check_daemon_response(response, **error_metadata)
    if err:
        return err

    data = response.get("data", {})

    # New daemons return a canonical globally ordered page. Keep the old
    # category projections available for clients that have not migrated yet.
    if isinstance(data, dict) and isinstance(data.get("alerts"), list) and (
        "page_limit" in data or "next_navigation" in data
    ):
        raw_alerts = [item for item in data["alerts"] if isinstance(item, dict)]
        alerts = [
            {key: value for key, value in item.items() if key not in {"text", "old_text"}}
            for item in raw_alerts
        ]
        deleted_messages = [
            {
                "dialog_id": item.get("dialog_id"),
                "message_id": item.get("message_id"),
                "deleted_at": item.get("deleted_at"),
                "action": item.get("action")
                or "Inspect the dialog history around this message id if surrounding context is needed.",
            }
            for item in alerts
            if item.get("kind") == "deleted_message"
        ]
        edit_alerts = [
            {
                "dialog_id": item.get("dialog_id"),
                "message_id": item.get("message_id"),
                "version": item.get("version"),
                "edit_date": item.get("edit_date"),
                "action": item.get("action")
                or "Treat cached text as versioned; inspect edit history before relying on older wording.",
            }
            for item in alerts
            if item.get("kind") == "edit"
        ]
        access_lost_alerts = [
            {
                "dialog_id": item.get("dialog_id"),
                "access_lost_at": item.get("access_lost_at"),
                "action": item.get("action") or "Use get_sync_status for coverage details.",
            }
            for item in alerts
            if item.get("kind") == "access_lost"
        ]
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
            "since": data.get("since", args.since),
            "limit": data.get("limit", args.limit),
            "page_limit": data.get("page_limit", args.page_limit or args.limit),
            "limited_by": data.get(
                "limited_by",
                {
                    "deleted_messages": {"since": args.since, "limit": args.page_limit or args.limit},
                    "edits": {"since": args.since, "limit": args.page_limit or args.limit},
                    "access_lost": {"since": args.since, "limit": args.page_limit or args.limit},
                },
            ),
            "has_more": bool(data.get("has_more", False)),
            "next_navigation": data.get("next_navigation"),
            "snapshot_upper_event_at": data.get("snapshot_upper_event_at", 0),
            "result_count_semantics": data.get("result_count_semantics", "count=len(alerts)=sum(counts)"),
        }
        return structured_result(
            structured_content,
            result_count=len(alerts),
            has_cursor=args.navigation is not None or bool(data.get("next_navigation")),
            page_depth=int(data.get("page_depth", 1)),
            has_filter=_as_int(data.get("since", args.since)) > 0,
        )

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

    return structured_result(
        structured_content,
        result_count=len(alerts),
        has_cursor=args.navigation is not None,
        page_depth=1,
        has_filter=args.since > 0,
    )
