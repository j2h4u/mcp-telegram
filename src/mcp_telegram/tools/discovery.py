import logging
from typing import Literal, cast

from pydantic import ConfigDict, Field, model_validator

logger = logging.getLogger(__name__)

from ..resolver import parse_exact_dialog_id
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
from .structured import StructuredWarning, structured_warning, telegram_content

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
                    "sync_status": {"type": ["string", "null"]},
                    "synced": {"type": ["boolean", "null"]},
                    "sync_coverage_pct": {"type": ["integer", "null"]},
                    "saved_message_count": {"type": "integer"},
                    "history_scope": {"type": "string"},
                    "history_depth_state": {"type": "string"},
                    "history_sync_state": {"type": "string"},
                    "history_complete_at": {"type": ["integer", "null"]},
                    "last_delta_checked_at": {"type": ["integer", "null"]},
                    "coverage_state": {"type": "string"},
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
    "required": ["dialogs", "count", "filters", "snapshot_age_h", "bootstrap_pending", "scope", "warnings"],
    "additionalProperties": False,
}


def _structured_dialog_lifecycle_fields(dialog: dict) -> dict[str, object]:
    draft_text = dialog.get("draft_text")
    return {
        "draft_text": draft_text,
        "draft_content": telegram_content(str(draft_text), "message_text") if draft_text is not None else None,
        "scheduled_count": int(dialog.get("scheduled_count", 0) or 0),
        "next_scheduled_at": dialog.get("next_scheduled_at"),
        "inclusion_basis": dialog.get("inclusion_basis"),
    }


def _structured_folder_placement(dialog: dict) -> dict[str, object]:
    folders: list[dict[str, object]] = []
    for folder in dialog.get("folders", []):
        if not isinstance(folder, dict):
            continue
        raw_id = folder.get("id")
        if isinstance(raw_id, bool) or not isinstance(raw_id, int):
            continue
        folders.append(
            {
                "id": raw_id,
                "title": telegram_content(str(folder.get("title", "")), "message_text"),
            }
        )
    return {
        "folder_ids": list(dialog.get("folder_ids", [])),
        "folders": folders,
    }


def _structured_sync_read_model(dialog: dict) -> dict[str, object]:
    return {
        "saved_message_count": int(dialog.get("saved_message_count", 0) or 0),
        "history_scope": dialog.get("history_scope", "none"),
        "history_depth_state": dialog.get("history_depth_state", "unknown"),
        "history_sync_state": dialog.get("history_sync_state", "unknown"),
        "history_complete_at": dialog.get("history_complete_at"),
        "last_delta_checked_at": dialog.get("last_delta_checked_at"),
        "coverage_state": dialog.get("coverage_state", "telegram_total_unknown"),
        "local_knowledge_at": dialog.get("local_knowledge_at"),
        "local_knowledge_age_seconds": dialog.get("local_knowledge_age_seconds"),
    }


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
    unread by peer); non-DM rows omit both fields.

    Dialog rows include a local scheduled buffer summary. Use message_state="scheduled" to
    return only dialogs with pending author-only scheduled messages; "all" is the default.
    Use scope="own_only" to return only dialogs accepted by the own-message classifier.

    sync_status values:
      - 'not_synced'  — no bulk fetch attempted
      - 'syncing'     — bulk fetch in progress
      - 'synced'      — full history mirrored locally, real-time events active
      - 'access_lost' — account no longer has access; read-only snapshot
      - 'fragment'    — no full sync; only point-fetched snippets from targeted
                        ListMessages(context_message_id=...) calls (Phase 999.1)
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

    data = response.get("data", {})
    dialogs = data.get("dialogs", [])
    snapshot_age_h = data.get("snapshot_age_h")
    bootstrap_pending = bool(data.get("bootstrap_pending", False))
    warnings: list[StructuredWarning] = []
    if snapshot_age_h is not None:
        warnings.append(
            structured_warning(
                "snapshot_stale",
                f"Dialog snapshot may be stale: snapshot_age_h={snapshot_age_h}.",
                severity="warning",
                action="Treat list_dialogs as a cached snapshot; call get_sync_status for critical dialogs.",
            )
        )
    structured_dialogs: list[dict[str, object]] = []
    for d in dialogs:
        sync_status = d.get("sync_status")
        structured_dialogs.append(
            {
                "id": d.get("id"),
                "name": d.get("name", ""),
                "type": d.get("type"),
                "last_message_at": d.get("last_message_at"),
                "unread_count": d.get("unread_count"),
                "sync_status": sync_status,
                "synced": (sync_status == "synced") if sync_status is not None else None,
                "sync_coverage_pct": d.get("sync_coverage_pct"),
                **_structured_sync_read_model(d),
                "access_lost_at": d.get("access_lost_at"),
                "members": d.get("members"),
                "created": d.get("created"),
                "unread_in": d.get("unread_in"),
                "unread_out": d.get("unread_out"),
                "unread_mentions_count": int(d.get("unread_mentions_count", 0) or 0),
                "unread_reactions_count": int(d.get("unread_reactions_count", 0) or 0),
                **_structured_dialog_lifecycle_fields(d),
                **_structured_folder_placement(d),
                "archived": bool(d.get("archived", False)),
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
        "snapshot_age_h": snapshot_age_h,
        "bootstrap_pending": bootstrap_pending,
        "scope": data.get("scope", args.scope),
        "warnings": warnings,
    }

    if not dialogs:
        return structured_result(structured_content, result_count=0)

    entity_dicts: list[dict] = []

    for structured_dialog in structured_dialogs:
        dialog_id = structured_dialog.get("id")
        dialog_name = structured_dialog["name"]
        dialog_type = structured_dialog["type"] or "unknown"

        # Upsert entities into daemon for future name resolution
        if isinstance(dialog_id, int) and isinstance(dialog_name, str):
            entity_dicts.append({"id": dialog_id, "type": dialog_type, "name": dialog_name, "username": None})

    if entity_dicts:
        try:
            async with daemon_connection() as upsert_conn:
                await upsert_conn.upsert_entities(entities=entity_dicts)
        except Exception:
            logger.debug("entity_upsert_skipped", exc_info=True)

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
    exact_dialog_id: int | None = Field(
        default=None,
        description=(
            "Known numeric dialog id. Prefer this when available; do not pass sender_id values from messages."
        ),
    )

    @model_validator(mode="after")
    def validate_dialog_selector(self) -> ListTopics:
        if self.dialog is None and self.exact_dialog_id is None:
            raise ValueError("Provide either dialog or exact_dialog_id.")
        if self.dialog is not None and self.exact_dialog_id is not None:
            raise ValueError("dialog and exact_dialog_id are mutually exclusive.")
        return self


def _list_topics_input_label(args: ListTopics) -> str:
    if args.exact_dialog_id is not None:
        return str(args.exact_dialog_id)
    return args.dialog or ""


def _list_topics_target(args: ListTopics) -> tuple[int | None, str | None]:
    dialog_id = args.exact_dialog_id
    if dialog_id is None and args.dialog is not None:
        dialog_id = parse_exact_dialog_id(args.dialog)
    dialog_name = None if dialog_id is not None else args.dialog
    return dialog_id, dialog_name


async def _fetch_topics_response(
    conn: DaemonConnection,
    dialog_id: int | None,
    dialog_name: str | None,
) -> dict[str, object]:
    if dialog_id is not None and dialog_id != 0:
        return await conn.list_topics(dialog_id=dialog_id)
    return await conn.list_topics(dialog=dialog_name)


def _list_topics_error_result(args: ListTopics, response: dict[str, object]) -> ToolResult:
    error_code = response.get("error", "")
    error_msg = response.get("message", "Request failed.")
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
