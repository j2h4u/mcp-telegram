import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast

from pydantic import ConfigDict, Field, StrictInt, model_validator

from ..dialog_selector import DialogSelectorError, required_dialog_selector
from ..sync_read_model import (
    CoverageState,
    HistoryDepthState,
    HistoryScope,
    HistorySyncState,
    SyncReadModel,
    SyncReadModelContractError,
    SyncStatus,
    decode_sync_read_model,
)
from ._base import (
    DaemonConnection,
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
from .dialog_resolution import project_dialog_resolution_error
from .structured import (
    FOLDER_SNAPSHOT_OUTPUT_SCHEMA,
    StructuredWarning,
    structured_warning,
    telegram_content,
)

logger = logging.getLogger(__name__)

TELEGRAM_CONTENT_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "is_telegram_content": {"type": "boolean"},
        "content_kind": {"type": "string"},
    },
    "required": ["text", "is_telegram_content", "content_kind"],
    "additionalProperties": False,
}

NULLABLE_TELEGRAM_CONTENT_OUTPUT_SCHEMA = {
    "type": ["object", "null"],
    "properties": TELEGRAM_CONTENT_OUTPUT_SCHEMA["properties"],
    "required": TELEGRAM_CONTENT_OUTPUT_SCHEMA["required"],
    "additionalProperties": False,
}

LIST_DIALOGS_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "dialogs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "name": {"type": ["string", "null"]},
                    "type": {"type": ["string", "null"]},
                    "last_message_at": {"type": ["integer", "string", "null"]},
                    "unread_count": {"type": ["integer", "null"]},
                    "sync_status": {"type": "string", "enum": [item.value for item in SyncStatus]},
                    "synced": {"type": "boolean"},
                    "sync_coverage_pct": {"type": ["integer", "null"]},
                    "saved_message_count": {"type": "integer"},
                    "history_scope": {"type": "string", "enum": [item.value for item in HistoryScope]},
                    "history_depth_state": {
                        "type": "string",
                        "enum": [item.value for item in HistoryDepthState],
                    },
                    "history_sync_state": {
                        "type": "string",
                        "enum": [item.value for item in HistorySyncState],
                    },
                    "history_complete_at": {"type": ["integer", "null"]},
                    "last_delta_checked_at": {"type": ["integer", "null"]},
                    "coverage_state": {"type": "string", "enum": [item.value for item in CoverageState]},
                    "local_knowledge_at": {"type": ["integer", "null"]},
                    "local_knowledge_age_seconds": {"type": ["integer", "null"]},
                    "access_lost_at": {"type": ["integer", "null"]},
                    "members": {"type": ["integer", "null"]},
                    "created": {"type": ["integer", "string", "null"]},
                    "unread_in": {"type": ["integer", "null"]},
                    "unread_out": {"type": ["integer", "null"]},
                    "unread_mentions_count": {"type": "integer"},
                    "unread_reactions_count": {"type": "integer"},
                    "draft_text": {"type": ["string", "null"]},
                    "draft_content": NULLABLE_TELEGRAM_CONTENT_OUTPUT_SCHEMA,
                    "scheduled_count": {
                        "type": "integer",
                        "description": "Count of pending author-only scheduled messages in this dialog.",
                    },
                    "next_scheduled_at": {
                        "type": ["integer", "null"],
                        "description": "Earliest pending scheduled publication timestamp, if any.",
                    },
                    "inclusion_basis": {
                        "type": ["array", "null"],
                        "items": {"type": "string"},
                        "description": "Stable own-only classifier basis when this row is in own scope.",
                    },
                    "folder_ids": {"type": "array", "items": {"type": "integer"}},
                    "folders": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "integer"},
                                "title": TELEGRAM_CONTENT_OUTPUT_SCHEMA,
                            },
                            "required": ["id", "title"],
                            "additionalProperties": False,
                        },
                    },
                    "archived": {"type": "boolean"},
                },
                "required": [
                    "id",
                    "name",
                    "type",
                    "last_message_at",
                    "unread_count",
                    "sync_status",
                    "synced",
                    "sync_coverage_pct",
                    "saved_message_count",
                    "history_scope",
                    "history_depth_state",
                    "history_sync_state",
                    "history_complete_at",
                    "last_delta_checked_at",
                    "coverage_state",
                    "local_knowledge_at",
                    "local_knowledge_age_seconds",
                    "access_lost_at",
                    "members",
                    "created",
                    "unread_in",
                    "unread_out",
                    "unread_mentions_count",
                    "unread_reactions_count",
                    "draft_text",
                    "draft_content",
                    "scheduled_count",
                    "next_scheduled_at",
                    "inclusion_basis",
                    "folder_ids",
                    "folders",
                    "archived",
                ],
                "additionalProperties": False,
            },
        },
        "count": {"type": "integer"},
        "filters": {
            "type": "object",
            "properties": {
                "exclude_archived": {"type": "boolean"},
                "ignore_pinned": {"type": "boolean"},
                "filter": {"type": ["string", "null"]},
                "message_state": {
                    "type": "string",
                    "enum": ["sent", "scheduled", "all"],
                    "description": "Filter dialog summaries by pending scheduled lifecycle state.",
                },
                "scope": {"type": "string", "enum": ["all", "own_only"]},
                "folder_id": {"type": ["integer", "null"]},
                "limit": {"type": ["integer", "null"]},
            },
            "required": ["exclude_archived", "ignore_pinned", "filter", "message_state", "scope", "folder_id", "limit"],
            "additionalProperties": False,
        },
        "snapshot_age_h": {"type": ["integer", "null"]},
        "bootstrap_pending": {"type": "boolean"},
        "scope": {"type": "string", "enum": ["all", "own_only"]},
        "folder_snapshot": FOLDER_SNAPSHOT_OUTPUT_SCHEMA,
        "warnings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string"},
                    "severity": {"type": "string"},
                    "message": {"type": "string"},
                    "action": {"type": "string"},
                },
                "required": ["kind", "severity", "message"],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "dialogs",
        "count",
        "filters",
        "snapshot_age_h",
        "bootstrap_pending",
        "scope",
        "folder_snapshot",
        "warnings",
    ],
    "additionalProperties": False,
}


@dataclass(frozen=True, slots=True)
class _FolderSnapshotSurface:
    generation: int | None
    status: str
    completed_at: int | None
    age_seconds: int | None
    complete: bool

    def to_wire(self) -> dict[str, object]:
        return {
            "generation": self.generation,
            "status": self.status,
            "completed_at": self.completed_at,
            "age_seconds": self.age_seconds,
            "complete": self.complete,
        }


@dataclass(frozen=True, slots=True)
class _DialogSurface:
    model: SyncReadModel
    dialog_id: int
    name: str | None
    dialog_type: str | None
    last_message_at: int | str | None
    unread_count: int | None
    access_lost_at: int | None
    members: int | None
    created: int | str | None
    unread_in: int | None
    unread_out: int | None
    unread_mentions_count: int
    unread_reactions_count: int
    draft_text: str | None
    scheduled_count: int
    next_scheduled_at: int | None
    inclusion_basis: list[str] | None
    folder_ids: list[int]
    folders: list[tuple[int, str]]
    archived: bool


@dataclass(frozen=True, slots=True)
class _ListDialogsSurface:
    dialogs: list[_DialogSurface]
    snapshot_age_h: int | None
    bootstrap_pending: bool
    scope: str
    folder_snapshot: _FolderSnapshotSurface


def _list_dialogs_contract_error(detail: str) -> ToolResult:
    return error_result(
        f"Error: daemon_protocol_error: invalid list_dialogs contract ({detail}).\n"
        "Action: Restart the daemon with the same mcp-telegram build, then retry ListDialogs."
    )


def _required(data: Mapping[str, object], name: str, *, context: str) -> object:
    if name not in data:
        raise SyncReadModelContractError(f"missing {context} field: {name}")
    return data[name]


def _strict_int(data: Mapping[str, object], name: str, *, context: str) -> int:
    value = _required(data, name, context=context)
    if isinstance(value, bool) or not isinstance(value, int):
        raise SyncReadModelContractError(f"{context}.{name} must be an integer")
    return value


def _strict_optional_int(data: Mapping[str, object], name: str, *, context: str) -> int | None:
    value = _required(data, name, context=context)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise SyncReadModelContractError(f"{context}.{name} must be an integer or null")
    return value


def _strict_optional_string(data: Mapping[str, object], name: str, *, context: str) -> str | None:
    value = _required(data, name, context=context)
    if value is None or isinstance(value, str):
        return value
    raise SyncReadModelContractError(f"{context}.{name} must be a string or null")


def _strict_optional_timestamp(data: Mapping[str, object], name: str, *, context: str) -> int | str | None:
    value = _required(data, name, context=context)
    if value is None or isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool)):
        return value
    raise SyncReadModelContractError(f"{context}.{name} must be an integer, string, or null")


def _strict_bool(data: Mapping[str, object], name: str, *, context: str) -> bool:
    value = _required(data, name, context=context)
    if not isinstance(value, bool):
        raise SyncReadModelContractError(f"{context}.{name} must be a boolean")
    return value


def _strict_string_list_or_none(data: Mapping[str, object], name: str, *, context: str) -> list[str] | None:
    value = _required(data, name, context=context)
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SyncReadModelContractError(f"{context}.{name} must be an array of strings or null")
    return value


def _strict_int_list(data: Mapping[str, object], name: str, *, context: str) -> list[int]:
    value = _required(data, name, context=context)
    if not isinstance(value, list) or not all(isinstance(item, int) and not isinstance(item, bool) for item in value):
        raise SyncReadModelContractError(f"{context}.{name} must be an array of integers")
    return value


def _strict_folders(data: Mapping[str, object], *, context: str) -> list[tuple[int, str]]:
    value = _required(data, "folders", context=context)
    if not isinstance(value, list):
        raise SyncReadModelContractError(f"{context}.folders must be an array")
    folders: list[tuple[int, str]] = []
    for index, item in enumerate(value):
        item_context = f"{context}.folders[{index}]"
        if not isinstance(item, Mapping):
            raise SyncReadModelContractError(f"{item_context} must be an object")
        folder_id = _strict_int(item, "id", context=item_context)
        title = _required(item, "title", context=item_context)
        if not isinstance(title, str):
            raise SyncReadModelContractError(f"{item_context}.title must be a string")
        folders.append((folder_id, title))
    return folders


def _strict_folder_snapshot(value: object) -> _FolderSnapshotSurface:
    if not isinstance(value, Mapping):
        raise SyncReadModelContractError("folder_snapshot must be an object")
    context = "folder_snapshot"
    generation = _strict_optional_int(value, "generation", context=context)
    completed_at = _strict_optional_int(value, "completed_at", context=context)
    age_seconds = _strict_optional_int(value, "age_seconds", context=context)
    status = _required(value, "status", context=context)
    complete = _required(value, "complete", context=context)
    if not isinstance(status, str) or status not in {"fresh", "stale", "unavailable"}:
        raise SyncReadModelContractError("folder_snapshot.status is invalid")
    if not isinstance(complete, bool):
        raise SyncReadModelContractError("folder_snapshot.complete must be a boolean")
    return _FolderSnapshotSurface(generation, cast(str, status), completed_at, age_seconds, complete)


def _strict_dialog(value: object, index: int) -> _DialogSurface:
    context = f"dialogs[{index}]"
    if not isinstance(value, Mapping):
        raise SyncReadModelContractError(f"{context} must be an object")
    return _DialogSurface(
        model=decode_sync_read_model(value),
        dialog_id=_strict_int(value, "id", context=context),
        name=_strict_optional_string(value, "name", context=context),
        dialog_type=_strict_optional_string(value, "type", context=context),
        last_message_at=_strict_optional_timestamp(value, "last_message_at", context=context),
        unread_count=_strict_optional_int(value, "unread_count", context=context),
        access_lost_at=_strict_optional_int(value, "access_lost_at", context=context),
        members=_strict_optional_int(value, "members", context=context),
        created=_strict_optional_timestamp(value, "created", context=context),
        unread_in=_strict_optional_int(value, "unread_in", context=context),
        unread_out=_strict_optional_int(value, "unread_out", context=context),
        unread_mentions_count=_strict_int(value, "unread_mentions_count", context=context),
        unread_reactions_count=_strict_int(value, "unread_reactions_count", context=context),
        draft_text=_strict_optional_string(value, "draft_text", context=context),
        scheduled_count=_strict_int(value, "scheduled_count", context=context),
        next_scheduled_at=_strict_optional_int(value, "next_scheduled_at", context=context),
        inclusion_basis=_strict_string_list_or_none(value, "inclusion_basis", context=context),
        folder_ids=_strict_int_list(value, "folder_ids", context=context),
        folders=_strict_folders(value, context=context),
        archived=_strict_bool(value, "archived", context=context),
    )


def _strict_list_dialogs_data(
    response: Mapping[str, object],
) -> _ListDialogsSurface:
    raw_data = response.get("data")
    if not isinstance(raw_data, Mapping):
        raise SyncReadModelContractError("data must be an object")
    raw_dialogs = _required(raw_data, "dialogs", context="list_dialogs")
    if not isinstance(raw_dialogs, list):
        raise SyncReadModelContractError("dialogs must be an array")
    dialogs = [_strict_dialog(raw_dialog, index) for index, raw_dialog in enumerate(raw_dialogs)]
    bootstrap_pending = _strict_bool(raw_data, "bootstrap_pending", context="list_dialogs")
    scope = _required(raw_data, "scope", context="list_dialogs")
    if not isinstance(scope, str) or scope not in {"all", "own_only"}:
        raise SyncReadModelContractError("scope must be all or own_only")
    return _ListDialogsSurface(
        dialogs=dialogs,
        snapshot_age_h=_strict_optional_int(raw_data, "snapshot_age_h", context="list_dialogs"),
        bootstrap_pending=bootstrap_pending,
        scope=cast(str, scope),
        folder_snapshot=_strict_folder_snapshot(_required(raw_data, "folder_snapshot", context="list_dialogs")),
    )


LIST_TOPICS_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "dialog": {"type": "string"},
        "dialog_id": {"type": ["integer", "null"]},
        "topics": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "topic_id": {"type": "integer"},
                    "title": {"type": "string"},
                    "title_content": TELEGRAM_CONTENT_OUTPUT_SCHEMA,
                    "icon": {
                        "type": "object",
                        "properties": {
                            "emoji": {"type": ["string", "null"]},
                            "color": {"type": ["string", "null"]},
                        },
                        "required": ["emoji", "color"],
                        "additionalProperties": False,
                    },
                    "pinned": {"type": ["boolean", "null"]},
                    "hidden": {"type": ["boolean", "null"]},
                    "snapshot_at": {"type": ["integer", "null"]},
                },
                "required": ["topic_id", "title", "title_content"],
                "additionalProperties": False,
            },
        },
        "count": {"type": "integer"},
        "empty_reason": {"type": ["string", "null"]},
    },
    "required": ["dialog", "dialog_id", "topics", "count", "empty_reason"],
    "additionalProperties": False,
}


class ListDialogs(ToolArgs):
    """List available dialogs, chats and channels with type and last message timestamp.

    Returns both archived and non-archived dialogs by default (Telegram uses archiving as a UI
    organization tool, not data archival). Set exclude_archived=True to show only non-archived
    dialogs (equivalent to old archived=False behavior).

    Pass `filter` to narrow by dialog name: case- and script-insensitive fuzzy match.
    Order: substring in latinized space → word-initials acronym (for 2-4 char queries,
    e.g. "ЖС" → "KS x Женские Сезоны") → typo-tolerant partial ratio. Prefer a filter
    over loading the full list.

    DM rows include integer 'unread_in' (incoming unread by me) and 'unread_out' (outgoing
    unread by peer); both fields are null for non-DM rows.

    Dialog rows include a local scheduled buffer summary. Use message_state="scheduled" to
    return only dialogs with pending author-only scheduled messages; "all" is the default.
    Use scope="own_only" to return only dialogs accepted by the own-message classifier.

    sync_status values:
      - 'not_synced'  — no bulk fetch attempted
      - 'syncing'     — bulk fetch in progress
      - 'synced'      — complete historical coverage as of history_complete_at; this does not prove active realtime
      - 'own_only'    — only own-message-related history is stored
      - 'access_lost' — account no longer has access; read-only snapshot
      - 'fragment'    — no full sync; only point-fetched snippets from targeted
                        ListMessages(context_message_id=...) calls (Phase 999.1)

    Call `get_sync_status` and inspect `realtime_history` when active realtime coverage matters.
    """

    exclude_archived: bool = False
    ignore_pinned: bool = False
    filter: str | None = Field(default=None, max_length=200)
    message_state: Literal["sent", "scheduled", "all"] = "all"
    scope: Literal["all", "own_only"] = "all"
    folder_id: int | None = Field(default=None, ge=0)
    limit: int | None = Field(
        default=None,
        ge=1,
        le=500,
        description="Optional maximum number of dialogs to return after all filters are applied.",
    )


@mcp_tool(
    name="list_dialogs",
    title="List Dialogs",
    posture="secondary/helper",
    annotations=ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
    output_schema=LIST_DIALOGS_OUTPUT_SCHEMA,
)
async def list_dialogs(args: ListDialogs) -> ToolResult:
    try:
        async with daemon_connection() as conn:
            response = await conn.list_dialogs(
                exclude_archived=args.exclude_archived,
                ignore_pinned=args.ignore_pinned,
                filter=args.filter,
                message_state=args.message_state,
                scope=args.scope,
                folder_id=args.folder_id,
                limit=args.limit,
            )
    except DaemonNotRunningError as exc:
        return error_result(_daemon_not_running_text(exc))

    if err := _check_daemon_response(response):
        return err

    try:
        surface = _strict_list_dialogs_data(response)
    except SyncReadModelContractError as exc:
        return _list_dialogs_contract_error(str(exc))
    warnings: list[StructuredWarning] = []
    if surface.snapshot_age_h is not None:
        warnings.append(
            structured_warning(
                "snapshot_stale",
                f"Dialog snapshot may be stale: snapshot_age_h={surface.snapshot_age_h}.",
                severity="warning",
                action="Treat list_dialogs as a cached snapshot; call get_sync_status for critical dialogs.",
            )
        )
    structured_dialogs: list[dict[str, object]] = []
    for dialog in surface.dialogs:
        wire = dialog.model.to_wire()
        structured_dialogs.append(
            {
                "id": dialog.dialog_id,
                "name": dialog.name,
                "type": dialog.dialog_type,
                "last_message_at": dialog.last_message_at,
                "unread_count": dialog.unread_count,
                "sync_status": wire["sync_status"],
                "synced": wire["synced"],
                "sync_coverage_pct": wire["sync_coverage_pct"],
                "saved_message_count": wire["saved_message_count"],
                "history_scope": wire["history_scope"],
                "history_depth_state": wire["history_depth_state"],
                "history_sync_state": wire["history_sync_state"],
                "history_complete_at": wire["history_complete_at"],
                "last_delta_checked_at": wire["last_delta_checked_at"],
                "coverage_state": wire["coverage_state"],
                "local_knowledge_at": wire["local_knowledge_at"],
                "local_knowledge_age_seconds": wire["local_knowledge_age_seconds"],
                "access_lost_at": dialog.access_lost_at,
                "members": dialog.members,
                "created": dialog.created,
                "unread_in": dialog.unread_in,
                "unread_out": dialog.unread_out,
                "unread_mentions_count": dialog.unread_mentions_count,
                "unread_reactions_count": dialog.unread_reactions_count,
                "draft_text": dialog.draft_text,
                "draft_content": (
                    telegram_content(dialog.draft_text, "message_text") if dialog.draft_text is not None else None
                ),
                "scheduled_count": dialog.scheduled_count,
                "next_scheduled_at": dialog.next_scheduled_at,
                "inclusion_basis": dialog.inclusion_basis,
                "folder_ids": dialog.folder_ids,
                "folders": [
                    {"id": folder_id, "title": telegram_content(title, "message_text")}
                    for folder_id, title in dialog.folders
                ],
                "archived": dialog.archived,
            }
        )
    structured_content = {
        "dialogs": structured_dialogs,
        "count": len(structured_dialogs),
        "filters": {
            "exclude_archived": args.exclude_archived,
            "ignore_pinned": args.ignore_pinned,
            "filter": args.filter,
            "message_state": args.message_state,
            "scope": args.scope,
            "folder_id": args.folder_id,
            "limit": args.limit,
        },
        "snapshot_age_h": surface.snapshot_age_h,
        "bootstrap_pending": surface.bootstrap_pending,
        "scope": surface.scope,
        "folder_snapshot": surface.folder_snapshot.to_wire(),
        "warnings": warnings,
    }

    if not surface.dialogs:
        return structured_result(structured_content, result_count=0)

    return structured_result(structured_content, result_count=len(structured_dialogs))


class ListTopics(ToolArgs):
    """
    List topics/threads for one topic-capable dialog.

    Provide dialog= for a name, username, link, or numeric dialog id, or exact_dialog_id=
    when the dialog id is already known. Do not pass message sender_id values here:
    sender_id identifies a message author, while dialog_id identifies the chat/bot/channel
    whose topics should be listed.

    Use this before topic= when working with forum supergroups or bot DM topics so you
    can choose an exact topic name or numeric topic_id instead of guessing via fuzzy match.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "oneOf": [
                {"required": ["dialog"]},
                {"required": ["exact_dialog_id"]},
            ]
        }
    )

    dialog: str | None = Field(default=None, max_length=500)
    exact_dialog_id: StrictInt | None = Field(
        default=None,
        description=(
            "Known numeric dialog id. Prefer this when available; do not pass sender_id values from messages."
        ),
    )

    @model_validator(mode="after")
    def validate_dialog_selector(self) -> ListTopics:
        try:
            required_dialog_selector(exact_id=self.exact_dialog_id, dialog=self.dialog)
        except DialogSelectorError as exc:
            raise ValueError(f"{exc.code}: {exc}") from exc
        return self


def _list_topics_input_label(args: ListTopics) -> str:
    if args.exact_dialog_id is not None:
        return str(args.exact_dialog_id)
    return args.dialog or ""


def _list_topics_target(args: ListTopics) -> tuple[int | None, str | None]:
    selector = required_dialog_selector(exact_id=args.exact_dialog_id, dialog=args.dialog)
    return selector.exact_id, selector.query


async def _fetch_topics_response(
    conn: DaemonConnection,
    dialog_id: int | None,
    dialog_name: str | None,
) -> dict[str, object]:
    if dialog_id is not None:
        return await conn.list_topics(dialog_id=dialog_id)
    return await conn.list_topics(dialog=dialog_name)


def _list_topics_error_result(args: ListTopics, response: dict[str, object]) -> ToolResult:
    error_code = response.get("error", "")
    error_msg = response.get("message", "Request failed.")
    projection = project_dialog_resolution_error(
        response,
        fallback_action="Retry ListTopics with an exact dialog id.",
    )
    if projection is not None:
        err = error_result(
            projection.text,
            has_filter=True,
        )
        return ToolResult(
            content=err.content,
            is_error=True,
            structured_content=projection.structured_content,
            has_filter=True,
        )
    if error_code == "dialog_not_found":
        from ..errors import dialog_not_found_text

        return error_result(
            dialog_not_found_text(_list_topics_input_label(args), retry_tool="ListTopics"), has_filter=True
        )
    error_prefix = f"{error_code}: " if error_code else ""
    return error_result(
        f"Error: {error_prefix}{error_msg}\n"
        "Action: Retry ListTopics with a corrected dialog id/name, or call ListDialogs first.",
        has_filter=True,
    )


def _structured_topic(topic: dict[str, object]) -> dict[str, object]:
    title = topic.get("title") or ""
    structured_topic: dict[str, object] = {
        "topic_id": topic.get("topic_id", topic.get("id")),
        "title": title,
        "title_content": telegram_content(str(title), "message_text"),
    }
    if icon := _structured_topic_icon(topic):
        structured_topic["icon"] = icon
    for field in ("pinned", "hidden", "snapshot_at"):
        if field in topic:
            structured_topic[field] = topic.get(field)
    return structured_topic


def _structured_topic_icon(topic: dict[str, object]) -> dict[str, object] | None:
    emoji = topic.get("icon_emoji")
    color_value = topic.get("icon_color")
    color = f"#{color_value:06X}" if isinstance(color_value, int) else None
    if not isinstance(emoji, str) or not emoji:
        emoji = None
    if emoji is None and color is None:
        return None
    return {"emoji": emoji, "color": None if emoji is not None else color}


def _list_topics_payload(args: ListTopics, data: dict[str, object]) -> dict[str, object]:
    raw_topics = data.get("topics", [])
    topics = [_structured_topic(topic) for topic in cast(list[dict[str, object]], raw_topics)]
    return {
        "dialog": _list_topics_input_label(args),
        "dialog_id": data.get("dialog_id"),
        "topics": topics,
        "count": len(topics),
        "empty_reason": None if topics else data.get("empty_reason", "no_active_topics"),
    }


@mcp_tool(
    name="list_topics",
    title="List Topics",
    posture="secondary/helper",
    annotations=ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
    output_schema=LIST_TOPICS_OUTPUT_SCHEMA,
)
async def list_topics(args: ListTopics) -> ToolResult:
    dialog_id, dialog_name = _list_topics_target(args)

    try:
        async with daemon_connection() as conn:
            response = await _fetch_topics_response(conn, dialog_id, dialog_name)
    except DaemonNotRunningError as exc:
        return error_result(_daemon_not_running_text(exc), has_filter=True)

    if not response.get("ok"):
        return _list_topics_error_result(args, response)

    data = response.get("data", {})
    structured_content = _list_topics_payload(args, data if isinstance(data, dict) else {})
    structured_topics = cast(list[dict[str, object]], structured_content["topics"])

    if not structured_topics:
        return structured_result(structured_content, has_filter=True)

    return structured_result(
        structured_content,
        result_count=len(structured_topics),
        has_filter=True,
    )
