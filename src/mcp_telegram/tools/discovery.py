import logging
from typing import Literal

from pydantic import Field

logger = logging.getLogger(__name__)

from ..resolver import parse_exact_dialog_id
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
            },
            "required": ["exclude_archived", "ignore_pinned", "filter", "message_state", "scope", "folder_id"],
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


@mcp_tool(
    name="list_dialogs",
    title="List Dialogs",
    posture="secondary/helper",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
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
                "access_lost_at": d.get("access_lost_at"),
                "members": d.get("members"),
                "created": d.get("created"),
                "unread_in": d.get("unread_in"),
                "unread_out": d.get("unread_out"),
                "unread_mentions_count": int(d.get("unread_mentions_count", 0) or 0),
                "unread_reactions_count": int(d.get("unread_reactions_count", 0) or 0),
                **_structured_dialog_lifecycle_fields(d),
                "folder_ids": list(d.get("folder_ids", [])),
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
    List forum topics for one dialog.

    Use this before topic= when working with forum supergroups so you can choose an exact topic
    name or numeric topic_id instead of guessing via fuzzy match.
    """

    dialog: str = Field(max_length=500)


@mcp_tool(
    name="list_topics",
    title="List Topics",
    posture="secondary/helper",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
    output_schema=LIST_TOPICS_OUTPUT_SCHEMA,
)
async def list_topics(args: ListTopics) -> ToolResult:
    # Try to resolve dialog_id from parsing as numeric/username first
    dialog_id: int | None = parse_exact_dialog_id(args.dialog)
    dialog_name: str | None = None if dialog_id is not None else args.dialog

    try:
        async with daemon_connection() as conn:
            if dialog_id is not None and dialog_id != 0:
                response = await conn.list_topics(dialog_id=dialog_id)
            else:
                response = await conn.list_topics(dialog=dialog_name)
    except DaemonNotRunningError:
        return error_result(_daemon_not_running_text(), has_filter=True)

    if not response.get("ok"):
        error_code = response.get("error", "")
        error_msg = response.get("message", "Request failed.")
        if error_code == "dialog_not_found":
            from ..errors import dialog_not_found_text

            return error_result(dialog_not_found_text(args.dialog, retry_tool="ListTopics"), has_filter=True)
        error_prefix = f"{error_code}: " if error_code else ""
        return error_result(
            f"Error: {error_prefix}{error_msg}\n"
            "Action: Retry ListTopics with a corrected dialog id/name, or call ListDialogs first.",
            has_filter=True,
        )

    data = response.get("data", {})
    topics = data.get("topics", [])
    structured_topics: list[dict[str, object]] = []
    for topic in topics:
        title = topic.get("title") or ""
        structured_topic: dict[str, object] = {
            "topic_id": topic.get("topic_id", topic.get("id")),
            "title": title,
            "title_content": telegram_content(str(title), "message_text"),
        }
        if "pinned" in topic:
            structured_topic["pinned"] = topic.get("pinned")
        if "hidden" in topic:
            structured_topic["hidden"] = topic.get("hidden")
        if "snapshot_at" in topic:
            structured_topic["snapshot_at"] = topic.get("snapshot_at")
        structured_topics.append(structured_topic)
    structured_content = {
        "dialog": args.dialog,
        "dialog_id": data.get("dialog_id"),
        "topics": structured_topics,
        "count": len(structured_topics),
        "empty_reason": None if structured_topics else "no_active_topics",
    }

    if not topics:
        return structured_result(structured_content, has_filter=True)

    return structured_result(
        structured_content,
        result_count=len(structured_topics),
        has_filter=True,
    )
