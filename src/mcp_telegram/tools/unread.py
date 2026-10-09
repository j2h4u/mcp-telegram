import math
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import cast

from pydantic import ConfigDict, Field, StrictInt, model_validator

from ..entity_identity import ENTITY_IDENTITY_SCHEMA, project_entity_identity
from ..inbox_projection import (  # noqa: F401 -- retain existing pure-helper imports for callers
    INBOX_MESSAGE_FIELDS,
    _display_name_source,
    _drop_largest_inbox_preview,
    _finalize_inbox_payload,
    _identity_text_fact,
    _inbox_int,
    _inbox_size,
    _project_inbox_message,
    _project_read_position_pending_entities,
    _structured_inbox_group,
    _structured_messages,
    project_inbox_payload,
)
from ..models import DialogType
from ..temporal import parse_utc_boundary
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
from .message_view import MESSAGE_VIEW_SCHEMA

GET_INBOX_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "limit": {"type": "integer"},
        "group_size_threshold": {"type": "integer"},
        "applied_since_utc": {"type": ["string", "null"]},
        "applied_dialog_types": {
            "type": "array",
            "items": {"type": "string", "enum": [item.value for item in DialogType]},
        },
        "read_position_pending_count": {"type": "integer"},
        "read_position_pending_entities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "entity": ENTITY_IDENTITY_SCHEMA,
                    "display_name_source": {"type": "string", "enum": ["name", "username", "numeric"]},
                },
                "required": ["entity", "display_name_source"],
                "additionalProperties": False,
            },
        },
        "coverage": {
            "type": "object",
            "properties": {
                "complete": {"type": "boolean"},
                "state": {"type": "string"},
                "scope": {"type": "string"},
                "read_position_pending_count": {"type": "integer"},
                "read_position_pending_entities": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "entity": ENTITY_IDENTITY_SCHEMA,
                            "display_name_source": {"type": "string", "enum": ["name", "username", "numeric"]},
                        },
                        "required": ["entity", "display_name_source"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["complete", "state", "scope", "read_position_pending_count", "read_position_pending_entities"],
            "additionalProperties": False,
        },
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
        "budget": {
            "type": "object",
            "properties": {
                "requested_limit": {"type": "integer"},
                "result_message_count": {"type": "integer"},
                "dialog_count": {"type": "integer"},
                "hidden_count": {"type": "integer"},
                "hidden_count_by_dialog": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "entity": ENTITY_IDENTITY_SCHEMA,
                            "display_name_source": {"type": "string", "enum": ["name", "username", "numeric"]},
                            "hidden_count": {"type": "integer"},
                        },
                        "required": ["entity", "display_name_source", "hidden_count"],
                        "additionalProperties": False,
                    },
                },
                "allocation_policy": {"type": "string"},
            },
            "required": [
                "requested_limit",
                "result_message_count",
                "dialog_count",
                "hidden_count",
                "hidden_count_by_dialog",
                "allocation_policy",
            ],
            "additionalProperties": False,
        },
        "selection_complete": {"type": "boolean"},
        "content_truncated_count": {"type": "integer"},
        "page": {"type": "integer"},
        "total_dialog_count": {"type": "integer"},
        "shown_dialog_count": {"type": "integer"},
        "remaining_dialog_count": {"type": "integer"},
        "next_page": {"type": ["integer", "null"]},
        "total_message_count": {"type": "integer"},
        "page_message_count": {"type": "integer"},
        "shown_message_count": {"type": "integer"},
        "dialogs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "entity": ENTITY_IDENTITY_SCHEMA,
                    "display_name_source": {"type": "string", "enum": ["name", "username", "numeric"]},
                    "category": {"type": ["string", "null"]},
                    "dialog_type": {"type": ["string", "null"]},
                    "unread_count": {"type": "integer"},
                    "unread_mentions_count": {
                        "type": "integer",
                        "description": (
                            "Observed persisted dialog-level Telegram unread mention count; it is not limited "
                            "by since_utc/last_hours and is used for ranking."
                        ),
                    },
                    "total_in_chat": {"type": "integer"},
                    "is_channel": {"type": "boolean"},
                    "is_bot": {"type": "boolean"},
                    "read_state": {
                        "type": ["object", "null"],
                        "properties": {
                            "dialog_type": {"type": ["string", "null"]},
                            "state": {"type": ["object", "null"]},
                            "header_lines": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["dialog_type", "state", "header_lines"],
                        "additionalProperties": False,
                    },
                    "budget": {
                        "type": "object",
                        "properties": {
                            "shown_count": {"type": "integer"},
                            "total_in_chat": {"type": "integer"},
                            "hidden_count": {"type": "integer"},
                        },
                        "required": ["shown_count", "total_in_chat", "hidden_count"],
                        "additionalProperties": False,
                    },
                    "messages": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                **{
                                    key: value
                                    for key, value in cast(dict[str, object], MESSAGE_VIEW_SCHEMA["properties"]).items()
                                    if key in INBOX_MESSAGE_FIELDS
                                },
                                "content_truncated": {
                                    "type": "boolean",
                                    "description": "True when the body or media description was shortened by preview_chars or the response budget.",
                                },
                                "content_source_length": {
                                    "type": "integer",
                                    "description": "Original rendered body plus media-description character count after canonical media deduplication.",
                                },
                            },
                            "required": [
                                "dialog_id",
                                "msg_id",
                                "sent_at",
                                "out",
                                "content_truncated",
                                "content_source_length",
                            ],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": [
                    "entity",
                    "display_name_source",
                    "category",
                    "dialog_type",
                    "unread_count",
                    "unread_mentions_count",
                    "total_in_chat",
                    "is_channel",
                    "is_bot",
                    "read_state",
                    "budget",
                    "messages",
                ],
                "additionalProperties": False,
            },
        },
        "count": {"type": "integer"},
        "result_count_semantics": {"type": "string"},
    },
    "required": [
        "limit",
        "group_size_threshold",
        "applied_since_utc",
        "read_position_pending_count",
        "read_position_pending_entities",
        "coverage",
        "warnings",
        "budget",
        "selection_complete",
        "content_truncated_count",
        "page",
        "total_dialog_count",
        "shown_dialog_count",
        "remaining_dialog_count",
        "next_page",
        "total_message_count",
        "page_message_count",
        "shown_message_count",
        "dialogs",
        "count",
        "result_count_semantics",
    ],
    "additionalProperties": False,
}

_MAX_INBOX_LAST_HOURS = 720

GET_UNREAD_SUMMARY_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "dialogs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "entity": ENTITY_IDENTITY_SCHEMA,
                    "display_name_source": {"type": "string", "enum": ["name", "username", "numeric"]},
                    "dialog_type": {"type": ["string", "null"]},
                    "unread_count": {"type": ["integer", "null"]},
                    "unread_mark": {"type": ["boolean", "null"]},
                    "unread_mentions_count": {"type": "integer"},
                    "unread_reactions_count": {"type": "integer"},
                    "archived": {"type": "boolean"},
                    "last_message_at": {"type": ["integer", "string", "null"]},
                },
                "required": [
                    "entity",
                    "display_name_source",
                    "dialog_type",
                    "unread_count",
                    "unread_mark",
                    "unread_mentions_count",
                    "unread_reactions_count",
                    "archived",
                    "last_message_at",
                ],
                "additionalProperties": False,
            },
        },
        "count": {"type": "integer"},
        "total_matching": {"type": "integer"},
        "truncated": {"type": "boolean"},
        "source_observation": {
            "type": "object",
            "properties": {
                "status": {"type": ["string", "null"]},
                "completed_at": {"type": ["integer", "string", "null"]},
                "observed_count": {"type": ["integer", "null"]},
                "visible_count": {"type": ["integer", "null"]},
            },
            "required": ["status", "completed_at", "observed_count", "visible_count"],
            "additionalProperties": False,
        },
    },
    "required": ["dialogs", "count", "total_matching", "truncated", "source_observation"],
    "additionalProperties": False,
}


class GetUnreadSummary(ToolArgs):
    """Return a compact unread overview from persisted Telegram dialog facts."""

    model_config = ConfigDict(extra="forbid")

    limit: StrictInt = Field(default=50, ge=1, le=200, description="Maximum number of unread dialogs to return.")


class GetInbox(ToolArgs):
    """Return a compact unread orientation from personal chats and small groups.

    Uses the synchronized Telegram state. Prioritizes human personal chats, mentioned groups,
    bots, services, and other groups in that order;
    channel dialogs are excluded unless explicitly included with include_dialog_types.
    Incoming human-DM messages deleted before reading remain visible for the configured
    recent-deletion period and include their last known content and deletion time.
    Messages inside each chat are chronological. ``@replies`` is classified as
    the ``service`` dialog type.
    Check read_position_pending_count and its bounded identities to detect
    incomplete read-position coverage instead of treating an empty result as
    final.
    """

    model_config = ConfigDict(extra="forbid")

    limit: int = Field(
        default=40,
        ge=1,
        le=100,
        description="Message budget for this dialog page (1-100), capped by messages_per_dialog.",
    )
    page: int = Field(
        default=1, ge=1, description="Dialog page number; keep dialogs_per_page unchanged when following next_page."
    )
    messages_per_dialog: int = Field(default=5, ge=1, le=20, description="Maximum previews per dialog (1-20).")
    dialogs_per_page: int = Field(
        default=20, ge=1, le=20, description="Dialogs per page (1-20); use the same size for page navigation."
    )
    preview_chars: int = Field(
        default=400,
        ge=32,
        le=4000,
        description="Character cap for each body and media preview; longer text ends with an ellipsis.",
    )
    response_chars: int = Field(
        default=24000,
        ge=4000,
        le=64000,
        description="Compact ASCII JSON character budget for the inbox payload; lower-priority previews are removed first.",
    )
    group_size_threshold: int = Field(
        default=100,
        ge=10,
        description=(
            "Group member count above which to hide messages. Dialogs with unknown member counts remain visible."
        ),
    )
    since_utc: str | None = Field(
        default=None,
        description="Inclusive RFC3339 UTC lower bound (Z or +00:00); mutually exclusive with last_hours and overrides its implicit 24-hour default.",
    )
    last_hours: StrictInt | None = Field(
        default=24,
        ge=0,
        le=_MAX_INBOX_LAST_HOURS,
        description="Return unread messages from the last 24 hours by default (0 disables the time filter; 1-720 returns that many hours). Evaluated at request time; mutually exclusive with since_utc.",
    )
    include_dialog_types: list[DialogType] | None = Field(
        default=None,
        min_length=1,
        description="Optional allowlist of canonical dialog types to include in the inbox.",
    )

    @model_validator(mode="before")
    @classmethod
    def validate_last_hours_range(cls, value: object) -> object:
        if isinstance(value, dict):
            hours = value.get("last_hours")
            if "since_utc" in value and value["since_utc"] is not None and "last_hours" not in value:
                value = {**value, "last_hours": None}
            elif isinstance(hours, int) and not isinstance(hours, bool) and not 0 <= hours <= _MAX_INBOX_LAST_HOURS:
                raise ValueError(f"last_hours must be between 0 and {_MAX_INBOX_LAST_HOURS} hours.")
        return value

    @model_validator(mode="after")
    def validate_time_filter(self) -> GetInbox:
        if self.since_utc is not None and self.last_hours is not None:
            raise ValueError("since_utc and last_hours are mutually exclusive; provide only one.")
        parse_utc_boundary(self.since_utc, field="since_utc")
        return self


def _canonical_utc_seconds(epoch_seconds: int) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_inbox_since(since_utc: str) -> int:
    """Parse a UTC lower bound, ceiled for integer-second message storage."""
    parsed = parse_utc_boundary(since_utc, field="since_utc")
    assert parsed is not None
    normalized = since_utc[:-1] + "+00:00" if since_utc.endswith("Z") else since_utc
    return math.ceil(datetime.fromisoformat(normalized).timestamp())


def _validate_inbox_last_hours(last_hours: int) -> None:
    if isinstance(last_hours, bool) or not isinstance(last_hours, int):
        raise ValueError("last_hours must be an integer between 0 and 720 hours.")
    if not 0 <= last_hours <= _MAX_INBOX_LAST_HOURS:
        raise ValueError(f"last_hours must be between 0 and {_MAX_INBOX_LAST_HOURS} hours.")


def _resolve_inbox_relative(last_hours: int, now: datetime | None) -> str:
    _validate_inbox_last_hours(last_hours)
    reference = now if now is not None else datetime.now(tz=UTC)
    if reference.tzinfo is None or reference.utcoffset() != UTC.utcoffset(reference):
        raise ValueError("now must be an aware UTC datetime.")
    cutoff = int(reference.timestamp()) - (last_hours * 60 * 60)
    return _canonical_utc_seconds(cutoff)


def _resolve_inbox_since(
    since_utc: str | None,
    last_hours: int | None,
    *,
    now: datetime | None = None,
) -> str | None:
    """Resolve the inbox's optional time selector to one canonical UTC bound."""
    if since_utc is not None and last_hours is not None:
        raise ValueError("since_utc and last_hours are mutually exclusive; provide only one.")
    if since_utc is not None:
        return _canonical_utc_seconds(_parse_inbox_since(since_utc))
    if last_hours is None:
        last_hours = 24
    _validate_inbox_last_hours(last_hours)
    if last_hours == 0:
        return None
    return _resolve_inbox_relative(last_hours, now)


def _project_unread_summary_dialog(raw_row: Mapping[str, object]) -> dict[str, object] | None:
    dialog_id = raw_row.get("dialog_id")
    if isinstance(dialog_id, bool) or not isinstance(dialog_id, int):
        return None
    entity = project_entity_identity(
        display_name=_identity_text_fact(raw_row.get("name")),
        username=_identity_text_fact(raw_row.get("username")),
        telegram_id=dialog_id,
    )
    return {
        "entity": entity,
        "display_name_source": _display_name_source(raw_row.get("display_name_source")),
        "dialog_type": raw_row.get("dialog_type"),
        "unread_count": raw_row.get("unread_count"),
        "unread_mark": raw_row.get("unread_mark"),
        "unread_mentions_count": raw_row.get("unread_mentions_count", 0),
        "unread_reactions_count": raw_row.get("unread_reactions_count", 0),
        "archived": raw_row.get("archived", False),
        "last_message_at": raw_row.get("last_message_at"),
    }


def _project_unread_summary_dialogs(raw_dialogs: object) -> list[dict[str, object]]:
    if not isinstance(raw_dialogs, list):
        return []
    dialogs: list[dict[str, object]] = []
    for raw_row in raw_dialogs:
        if not isinstance(raw_row, Mapping):
            continue
        dialog = _project_unread_summary_dialog(raw_row)
        if dialog is not None:
            dialogs.append(dialog)
    return dialogs


def _project_inbox_response(
    args: GetInbox,
    response: dict,
    *,
    applied_since_utc: str | None,
    has_inbox_filter: bool,
) -> ToolResult:
    if err := _check_daemon_response(response):
        err.has_filter = has_inbox_filter
        return err
    data = response.get("data", {})
    payload = (
        data
        if response.get("inbox_projected")
        else project_inbox_payload(args, data, applied_since_utc=applied_since_utc)
    )
    if payload is None:
        return error_result(
            "Error: inbox metadata exceeds response_chars. Action: narrow include_dialog_types or reduce dialogs_per_page.",
            has_filter=has_inbox_filter,
        )
    return structured_result(
        payload,
        result_count=_inbox_int(cast(dict[str, object], payload["budget"]), "result_message_count", 0),
        has_filter=has_inbox_filter,
    )


@mcp_tool(
    name="get_inbox",
    title="Inbox",
    annotations=ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
    output_schema=GET_INBOX_OUTPUT_SCHEMA,
)
async def get_inbox(args: GetInbox) -> ToolResult:
    try:
        applied_since_utc = _resolve_inbox_since(args.since_utc, args.last_hours)
    except ValueError as exc:
        return error_result(f"Error: invalid time filter: {exc}", has_filter=True)
    has_inbox_filter = applied_since_utc is not None or args.include_dialog_types is not None

    try:
        async with daemon_connection() as conn:
            response = await conn.get_inbox(
                limit=args.limit,
                preview_chars=args.preview_chars,
                timezone=args.timezone,
                response_chars=args.response_chars,
                page=args.page,
                messages_per_dialog=args.messages_per_dialog,
                dialogs_per_page=args.dialogs_per_page,
                group_size_threshold=args.group_size_threshold,
                since_utc=applied_since_utc,
                include_dialog_types=(
                    [dialog_type.value for dialog_type in args.include_dialog_types]
                    if args.include_dialog_types is not None
                    else None
                ),
            )
    except DaemonNotRunningError as exc:
        return error_result(_daemon_not_running_text(exc), has_filter=has_inbox_filter)

    return _project_inbox_response(
        args,
        response,
        applied_since_utc=applied_since_utc,
        has_inbox_filter=has_inbox_filter,
    )


@mcp_tool(
    name="get_unread_summary",
    title="Unread Summary",
    annotations=ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
    output_schema=GET_UNREAD_SUMMARY_OUTPUT_SCHEMA,
)
async def get_unread_summary(args: GetUnreadSummary) -> ToolResult:
    """Return unread dialog facts without reading message history or cursors."""
    try:
        async with daemon_connection() as conn:
            response = await conn.get_unread_summary(limit=args.limit)
    except DaemonNotRunningError as exc:
        return error_result(_daemon_not_running_text(exc))

    if err := _check_daemon_response(response):
        return err

    raw_data = response.get("data")
    data = raw_data if isinstance(raw_data, Mapping) else {}
    raw_dialogs = data.get("dialogs", [])
    dialogs = _project_unread_summary_dialogs(raw_dialogs)
    raw_observation = data.get("source_observation")
    observation = dict(raw_observation) if isinstance(raw_observation, Mapping) else {}
    source_observation = {
        "status": observation.get("status"),
        "completed_at": observation.get("completed_at"),
        "observed_count": observation.get("observed_count"),
        "visible_count": observation.get("visible_count"),
    }
    structured_content = {
        "dialogs": dialogs,
        "count": len(dialogs),
        "total_matching": int(data.get("total_matching", len(dialogs)) or 0),
        "truncated": bool(data.get("truncated", False)),
        "source_observation": source_observation,
    }
    return structured_result(structured_content, result_count=len(dialogs))
