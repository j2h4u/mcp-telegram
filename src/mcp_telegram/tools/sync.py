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
    decode_latest_retained_message,
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
        "history_sync_state": {
            "type": "string",
            "enum": [*(item.value for item in HistorySyncState), "pending", "failed", "unknown"],
        },
        "action": {"type": "string"},
        "blocked_reason": {"type": ["string", "null"]},
        "full_history_will_be_fetched": {"type": "boolean"},
    },
    "required": [
        "dialog_id",
        "enabled",
        "history_sync_state",
        "action",
        "blocked_reason",
        "full_history_will_be_fetched",
    ],
    "additionalProperties": False,
}


_DELIVERY_STREAM_SCHEMA = {
    "type": "object",
    "properties": {
        "continuity": {
            "enum": ["unknown", "gap_observed", "no_gap_observed"],
            "description": "Protocol gap observation at the reported instant; no observed gap is not a completeness guarantee.",
        },
        "catch_up": {
            "type": "object",
            "properties": {
                "scope": {"enum": ["account", "channel"]},
                "state": {
                    "enum": ["unknown", "not_due", "due", "in_flight", "blocked"],
                    "description": "Catch-up state for this stream; in_flight requires a physical claim.",
                },
                "reason": {
                    "enum": [
                        "owner_unavailable",
                        "not_due",
                        "deadline",
                        "possible_gap",
                        "explicit_gap",
                        "intake_pending",
                        "eligibility",
                        "physical_claim",
                        "unknown",
                    ]
                },
                "overdue_seconds": {"type": "number"},
                "eligible_in_seconds": {"type": "number"},
            },
            "required": ["scope", "state", "reason"],
            "additionalProperties": False,
        },
    },
    "required": ["continuity", "catch_up"],
    "additionalProperties": False,
}


GET_SYNC_STATUS_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "dialog_id": {"type": "integer"},
        "enrollment_enabled": {"type": ["boolean", "null"]},
        "automatic_group_sync": {
            "type": "object",
            "description": "Current local automatic group eligibility; a fresh Telegram total requires a background probe.",
            "properties": {
                "eligibility": {"enum": ["eligible_for_probe", "blocked", "unknown"]},
                "reason": {"type": "string"},
                "fresh_telegram_total": {"const": "unknown"},
                "retry_at": {"type": "integer"},
            },
            "required": ["eligibility", "reason", "fresh_telegram_total"],
            "additionalProperties": False,
        },
        "realtime_history": {
            "type": "string",
            "enum": [item.value for item in RealtimeHistory],
            "description": "Legacy retention policy, not evidence that messages are arriving.",
        },
        "message_retention_policy": {
            "type": "string",
            "enum": ["all_authorized", "own_only", "none"],
            "description": "Which delivered messages may be retained; this does not establish complete delivery.",
        },
        "latest_retained_message": {
            "type": ["object", "null"],
            "description": "Newest retained nondeleted message by Telegram sent time, including incoming, outgoing and service messages; not latest local arrival or delivery completeness.",
            "properties": {"message_id": {"type": "integer"}, "sent_at": {"type": "integer"}},
            "required": ["message_id", "sent_at"],
            "additionalProperties": False,
        },
        "message_delivery": {
            "type": "object",
            "properties": {
                "observed_at_ms": {"type": "integer"},
                "account": _DELIVERY_STREAM_SCHEMA,
                "channel": _DELIVERY_STREAM_SCHEMA,
                "intake_queue_depth": {"type": "integer"},
                "pending_channels": {"type": "integer"},
            },
            "required": ["observed_at_ms", "account"],
            "additionalProperties": False,
        },
        "is_syncing": {"type": "boolean"},
        "last_synced_at": {"type": ["integer", "null"]},
        "last_event_at": {
            "type": ["integer", "null"],
            "description": "Latest observed update, not necessarily a message.",
        },
        "last_delta_checked_at": {
            "type": ["integer", "null"],
            "description": "Latest recorded reconciliation, not a continuous freshness guarantee.",
        },
        "delta_refresh_requested_at": {
            "type": ["integer", "null"],
            "description": "Requested reconciliation, not evidence of completion.",
        },
        "message_count": {"type": "integer"},
        "saved_message_count": {"type": "integer"},
        "history_scope": {"type": "string", "enum": [item.value for item in HistoryScope]},
        "history_depth_state": {"type": "string", "enum": [item.value for item in HistoryDepthState]},
        "history_sync_state": {"type": "string", "enum": [item.value for item in HistorySyncState]},
        "history_complete_at": {
            "type": ["integer", "null"],
            "description": "Recorded completed-history boundary, which can advance after reconciliation; not the exact initial completion time.",
        },
        "coverage_state": {"type": "string", "enum": [item.value for item in CoverageState]},
        "local_knowledge_at": {
            "type": ["integer", "null"],
            "description": "Latest recorded local observation, not a message time or continuous freshness guarantee.",
        },
        "local_knowledge_age_seconds": {
            "type": ["integer", "null"],
            "description": "Age of the latest recorded local observation at the status read.",
        },
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
        "enrollment_enabled",
        "automatic_group_sync",
        "realtime_history",
        "message_retention_policy",
        "latest_retained_message",
        "message_delivery",
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
    automatic_group_sync: dict[str, object]
    message_delivery: dict[str, object]
    delta_refresh_requested_at: int | None
    sync_progress: int | None
    sync_progress_message_id: int | None
    delete_detection: str
    access_lost_at: int | None
    access_last_revalidated_at: int | None
    access_next_revalidate_at: int | None
    latest_retained_message: dict[str, int] | None


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
        message_delivery=_decode_message_delivery(_surface_required(data, "message_delivery")),
        automatic_group_sync=_decode_automatic_group_status(_surface_required(data, "automatic_group_sync")),
        delta_refresh_requested_at=_surface_optional_int(data, "delta_refresh_requested_at"),
        sync_progress=_surface_optional_int(data, "sync_progress"),
        sync_progress_message_id=_surface_optional_int(data, "sync_progress_message_id"),
        delete_detection=_surface_string(data, "delete_detection"),
        access_lost_at=_surface_optional_int(data, "access_lost_at"),
        access_last_revalidated_at=_surface_optional_int(data, "access_last_revalidated_at"),
        access_next_revalidate_at=_surface_optional_int(data, "access_next_revalidate_at"),
        latest_retained_message=decode_latest_retained_message(_surface_required(data, "latest_retained_message")),
    )


def _decode_message_delivery(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or type(value.get("observed_at_ms")) is not int:
        raise SyncReadModelContractError("invalid message delivery snapshot")
    if set(value) - {"observed_at_ms", "account", "channel", "intake_queue_depth", "pending_channels"}:
        raise SyncReadModelContractError("unexpected message delivery field")
    for count in ("intake_queue_depth", "pending_channels"):
        if count in value and (type(value[count]) is not int or value[count] < 0):
            raise SyncReadModelContractError("invalid message delivery count")
    _validate_delivery_stream(value.get("account"), "account")
    if "channel" in value:
        _validate_delivery_stream(value["channel"], "channel")
    return value


def _validate_delivery_stream(stream: object, scope: str) -> None:
    if not isinstance(stream, dict) or set(stream) != {"continuity", "catch_up"}:
        raise SyncReadModelContractError("invalid message delivery stream")
    if not _is_known_value(stream["continuity"], {"unknown", "gap_observed", "no_gap_observed"}):
        raise SyncReadModelContractError("invalid protocol continuity")
    catch_up = stream["catch_up"]
    if not isinstance(catch_up, dict) or catch_up.get("scope") != scope:
        raise SyncReadModelContractError("invalid catch-up scope")
    _validate_catch_up(catch_up)


def _validate_catch_up(catch_up: dict[str, object]) -> None:
    if not _is_known_value(catch_up.get("state"), {"unknown", "not_due", "due", "in_flight", "blocked"}):
        raise SyncReadModelContractError("invalid catch-up state")
    if not _is_known_value(
        catch_up.get("reason"),
        {
            "owner_unavailable",
            "not_due",
            "deadline",
            "possible_gap",
            "explicit_gap",
            "intake_pending",
            "eligibility",
            "physical_claim",
            "unknown",
        },
    ):
        raise SyncReadModelContractError("invalid catch-up reason")
    if set(catch_up) - {"scope", "state", "reason", "overdue_seconds", "eligible_in_seconds"}:
        raise SyncReadModelContractError("unexpected catch-up field")
    for timing in ("overdue_seconds", "eligible_in_seconds"):
        if timing in catch_up:
            duration = catch_up[timing]
            if isinstance(duration, bool) or not isinstance(duration, int | float) or duration < 0:
                raise SyncReadModelContractError("invalid catch-up timing")


def _decode_automatic_group_status(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise SyncReadModelContractError("automatic_group_sync must be an object")
    eligibility = value.get("eligibility")
    if (
        not _is_known_value(eligibility, {"eligible_for_probe", "blocked", "unknown"})
        or value.get("fresh_telegram_total") != "unknown"
    ):
        raise SyncReadModelContractError("invalid automatic group eligibility")
    if not isinstance(value.get("reason"), str) or set(value) - {
        "eligibility",
        "reason",
        "fresh_telegram_total",
        "retry_at",
    }:
        raise SyncReadModelContractError("invalid automatic group status")
    if "retry_at" in value and type(value["retry_at"]) is not int:
        raise SyncReadModelContractError("invalid automatic group retry time")
    return dict(value)


def _is_known_value(value: object, allowed: set[str]) -> bool:
    return isinstance(value, str) and value in allowed


class MarkDialogForSync(ToolArgs):
    """Mark or unmark a dialog for persistent sync. When marked, full message history
    will be fetched shortly. Unmarking preserves existing synced history but stops
    further sync. Use ListDialogs to find dialog IDs and history state."""

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
        "history_sync_state": data["history_sync_state"],
        "action": data["action"],
        "blocked_reason": data["blocked_reason"],
        "full_history_will_be_fetched": data["full_history_will_be_fetched"],
    }
    return structured_result(structured_content, result_count=1)


class GetSyncStatus(ToolArgs):
    """Get sync status for a dialog: message count, sync progress, last sync/event timestamps,
    and delete detection reliability. delete_detection is 'reliable (channel)' for channels/supergroups
    (real-time MTProto events), 'best-effort weekly (DM)' while periodic DM verification is active,
    or a paused/stale notice when that maintenance has been suspended.
    sync_progress is the raw message_id offset cursor, not a row count. Works for any dialog —
    non-synced dialogs return status='not_synced' with zero counts.
    Retention policy says which delivered messages may be saved; it does not verify delivery.
    Automatic group eligibility describes local facts; a fresh Telegram total requires a background probe.
    The latest retained message is ordered by Telegram sent time, not local arrival."""

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
        "enrollment_enabled": wire["enrollment_enabled"],
        "automatic_group_sync": surface.automatic_group_sync,
        "realtime_history": wire["realtime_history"],
        "message_retention_policy": {
            RealtimeHistory.FULL: "all_authorized",
            RealtimeHistory.OWN_ONLY: "own_only",
            RealtimeHistory.NONE: "none",
        }[surface.model.realtime_history],
        "latest_retained_message": surface.latest_retained_message,
        "message_delivery": surface.message_delivery,
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
