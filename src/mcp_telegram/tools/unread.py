import math
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import cast

from pydantic import ConfigDict, Field, StrictInt, model_validator

from ..entity_identity import ENTITY_IDENTITY_SCHEMA, EntityIdentity, project_entity_identity
from ..formatter import (
    _compute_inline_markers,
    _render_read_state_header,
)
from ..models import DialogType, ReadMessage, ReadState
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
from .structured import (
    StructuredWarning,
    serialize_message_content,
    structured_warning,
    telegram_content,
)

GET_INBOX_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "limit": {"type": "integer"},
        "group_size_threshold": {"type": "integer"},
        "applied_since_utc": {"type": ["string", "null"]},
        "bootstrap_pending": {"type": "integer"},
        "coverage": {
            "type": "object",
            "properties": {
                "complete": {"type": "boolean"},
                "state": {"type": "string"},
                "bootstrap_pending_count": {"type": "integer"},
            },
            "required": ["complete", "state", "bootstrap_pending_count"],
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
                            "hidden_count": {"type": "integer"},
                        },
                        "required": ["entity", "hidden_count"],
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
        "dialogs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "entity": ENTITY_IDENTITY_SCHEMA,
                    "category": {"type": ["string", "null"]},
                    "dialog_type": {"type": ["string", "null"]},
                    "unread_count": {"type": "integer"},
                    "unread_mentions_count": {"type": "integer"},
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
                                "msg_id": {"type": "integer"},
                                "sender": {
                                    "oneOf": [
                                        ENTITY_IDENTITY_SCHEMA,
                                        {
                                            "type": "object",
                                            "properties": {"kind": {"type": "string", "enum": ["system", "unknown"]}},
                                            "required": ["kind"],
                                            "additionalProperties": False,
                                        },
                                    ]
                                },
                                "out": {"type": "boolean"},
                                # Keep Unix seconds at the internal tool boundary so
                                # the shared temporal presentation layer can render
                                # the requested timezone in the MCP response.
                                "date": {"type": ["integer", "null"]},
                                "content": {"type": ["object", "null"]},
                                "media": {"type": ["object", "null"]},
                                "reply_to_msg_id": {"type": ["integer", "null"]},
                                "edit_date": {"type": ["integer", "null"]},
                                "reactions": {"type": ["object", "null"]},
                                "reaction_events": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "reactor_id": {"type": ["integer", "null"]},
                                            "emoji": {"type": "string"},
                                            "reacted_at": {"type": ["integer", "null"]},
                                        },
                                        "required": ["reactor_id", "emoji", "reacted_at"],
                                        "additionalProperties": False,
                                    },
                                },
                                "reaction_events_status": {"type": "string"},
                                "read_at": {"type": ["integer", "null"]},
                                "read_markers": {"type": "array", "items": {"type": "object"}},
                                "inline_markers": {"type": "array", "items": {"type": "object"}},
                            },
                            "required": [
                                "msg_id",
                                "sender",
                                "out",
                                "date",
                                "content",
                                "media",
                                "reply_to_msg_id",
                                "edit_date",
                                "reactions",
                                "reaction_events",
                                "reaction_events_status",
                                "read_at",
                                "read_markers",
                                "inline_markers",
                            ],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": [
                    "entity",
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
        "bootstrap_pending",
        "coverage",
        "warnings",
        "budget",
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
    """Fetch unread messages from personal chats and small groups, prioritized by tier.

    Reads local sync.db only. Prioritizes mentions, DMs, bots, and groups;
    channel dialogs are excluded. Messages inside each chat are chronological.
    Check bootstrap_pending to detect incomplete read-position coverage instead
    of treating an empty result as final.
    """

    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=100, ge=50, le=500, description="Total message budget across all chats (50-500)")
    group_size_threshold: int = Field(
        default=100,
        ge=10,
        description=(
            "Group member count above which to hide messages. Dialogs with unknown member counts remain visible."
        ),
    )
    since_utc: str | None = Field(
        default=None,
        description="Inclusive RFC3339 UTC lower bound (Z or +00:00); mutually exclusive with last_hours.",
    )
    last_hours: StrictInt | None = Field(
        default=None,
        ge=1,
        le=_MAX_INBOX_LAST_HOURS,
        description="Return unread messages from the last 1-720 hours, evaluated at request time; mutually exclusive with since_utc.",
    )

    @model_validator(mode="before")
    @classmethod
    def validate_last_hours_range(cls, value: object) -> object:
        if isinstance(value, dict):
            hours = value.get("last_hours")
            if isinstance(hours, int) and not isinstance(hours, bool) and not 1 <= hours <= _MAX_INBOX_LAST_HOURS:
                raise ValueError(f"last_hours must be between 1 and {_MAX_INBOX_LAST_HOURS} hours.")
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
        raise ValueError("last_hours must be an integer between 1 and 720 hours.")
    if not 1 <= last_hours <= _MAX_INBOX_LAST_HOURS:
        raise ValueError(f"last_hours must be between 1 and {_MAX_INBOX_LAST_HOURS} hours.")


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
        return None
    return _resolve_inbox_relative(last_hours, now)


def _message_date(sent_at: object) -> int | None:
    if sent_at is None or isinstance(sent_at, bool):
        return None
    if not isinstance(sent_at, (int, float, str, bytes, bytearray)):
        return None
    try:
        return int(sent_at)
    except TypeError, ValueError, OverflowError:
        return None


def _project_message_sender(message: ReadMessage) -> EntityIdentity | dict[str, object]:
    """Project one inbox message author without inventing an identity.

    ``effective_sender_id`` carries the concrete actor for ordinary senders,
    including the peer/self sides of direct messages.  Service messages and
    group rows with no sender id intentionally remain explicit non-entities.
    """
    if message.is_service:
        return {"kind": "system"}

    actor_id = message.effective_sender_id
    if actor_id is None:
        actor_id = message.sender_id
    if isinstance(actor_id, bool) or not isinstance(actor_id, int) or actor_id == 0:
        return {"kind": "unknown"}

    return project_entity_identity(
        display_name=message.sender_first_name,
        username=message.sender_username,
        telegram_id=actor_id,
    )


_READ_MARKER_METADATA = {
    "[I read up to here]": {
        "kind": "i_read_up_to_here",
        "side": "inbox",
        "role": "boundary",
    },
    "[unread by me]": {
        "kind": "unread_by_me",
        "side": "inbox",
        "role": "tail_start",
    },
    "[peer read up to here]": {
        "kind": "peer_read_up_to_here",
        "side": "outbox",
        "role": "boundary",
    },
    "[unread by peer]": {
        "kind": "unread_by_peer",
        "side": "outbox",
        "role": "tail_start",
    },
}


def _structured_reactions(display: str | None) -> dict[str, object] | None:
    if not display:
        return None
    return {
        "display": display,
        "content": telegram_content(display, "reaction"),
    }


def _structured_reaction_events(message: ReadMessage) -> list[dict[str, object]]:
    return [
        {
            "reactor_id": event.get("reactor_id") if isinstance(event, Mapping) else event.reactor_id,
            "emoji": event.get("emoji") if isinstance(event, Mapping) else event.emoji,
            "reacted_at": event.get("reacted_at") if isinstance(event, Mapping) else event.reacted_at,
        }
        for event in message.reaction_events
    ]


def _structured_read_marker(message_id: int, label: str) -> dict[str, object]:
    metadata = _READ_MARKER_METADATA[label]
    return {
        "kind": metadata["kind"],
        "label": label,
        "side": metadata["side"],
        "role": metadata["role"],
        "anchor_message_id": message_id,
    }


def _read_state_payload(read_state: ReadState | dict | None, dialog_type: str | None) -> dict[str, object] | None:
    if read_state is None and dialog_type is None:
        return None
    return {
        "dialog_type": dialog_type,
        "state": read_state,
        "header_lines": _render_read_state_header(read_state, dialog_type, int(time.time())),
    }


def _bootstrap_pending_warnings(bootstrap_pending: int) -> list[StructuredWarning]:
    if bootstrap_pending <= 0:
        return []

    warning_message = (
        f"bootstrap_pending={bootstrap_pending} dialog(s) are still being seeded by the sync daemon. "
        "Results may be incomplete until bootstrap completes."
    )
    return [
        structured_warning(
            "bootstrap_pending",
            warning_message,
            severity="warning",
            action="Retry shortly once the sync daemon finishes read-state bootstrap.",
        )
    ]


def _structured_inbox_group(group: dict) -> tuple[dict[str, object], dict[str, object] | None, int]:
    message_rows = group.get("messages", [])
    total_in_chat = int(group.get("total_in_chat", group.get("unread_count", 0)) or 0)
    hidden_count = max(0, total_in_chat - len(message_rows))
    read_state = group.get("read_state")
    read_state_payload = read_state if isinstance(read_state, dict) else None
    telegram_id = int(group.get("dialog_id", 0) or 0)
    entity = project_entity_identity(
        display_name=group.get("display_name"),
        username=group.get("username"),
        telegram_id=telegram_id,
    )
    dialog = {
        "entity": entity,
        "category": group.get("category"),
        "dialog_type": group.get("dialog_type"),
        "unread_count": group.get("unread_count", 0),
        "unread_mentions_count": group.get("unread_mentions_count", 0),
        "total_in_chat": total_in_chat,
        "is_channel": DialogType.parse(group.get("category")) == DialogType.CHANNEL,
        "is_bot": DialogType.parse(group.get("category")) == DialogType.BOT,
        "read_state": _read_state_payload(read_state_payload, group.get("dialog_type")),
        "budget": {
            "shown_count": len(message_rows),
            "total_in_chat": total_in_chat,
            "hidden_count": hidden_count,
        },
        "messages": _structured_messages(
            message_rows,
            read_state=read_state_payload,
            dialog_type=group.get("dialog_type"),
        ),
    }
    hidden_entry: dict[str, object] | None = None
    if hidden_count:
        hidden_entry = {"entity": entity, "hidden_count": hidden_count}
    return dialog, hidden_entry, len(message_rows)


def _structured_inbox_groups(
    groups: list[dict],
) -> tuple[list[dict[str, object]], list[dict[str, object]], int]:
    structured_dialogs: list[dict[str, object]] = []
    hidden_count_by_dialog: list[dict[str, object]] = []
    result_message_count = 0
    for group in groups:
        dialog, hidden_entry, message_count = _structured_inbox_group(group)
        structured_dialogs.append(dialog)
        result_message_count += message_count
        if hidden_entry is not None:
            hidden_count_by_dialog.append(hidden_entry)
    return structured_dialogs, hidden_count_by_dialog, result_message_count


def _structured_messages(
    rows: list[dict], *, read_state: dict | None, dialog_type: str | None
) -> list[dict[str, object]]:
    if not rows:
        return []
    ordered_rows = sorted(
        rows,
        key=lambda row: (
            int(row.get("sent_at") or 0),
            int(row.get("message_id") or 0),
        ),
    )
    messages = [ReadMessage(**row) for row in ordered_rows]
    marker_by_message = (
        _compute_inline_markers(messages, read_state) if DialogType.parse(dialog_type) == DialogType.USER else {}
    )
    structured: list[dict[str, object]] = []
    for _row, message in zip(ordered_rows, messages, strict=False):
        marker_label = marker_by_message.get(message.id)
        read_markers = [_structured_read_marker(message.id, marker_label)] if marker_label else []
        projected = serialize_message_content(message.text, message.media_description, message.content_kind)
        structured.append(
            {
                "msg_id": message.id,
                "sender": _project_message_sender(message),
                "out": bool(message.out),
                "date": _message_date(message.sent_at),
                "content": projected["content"],
                "media": projected["media"],
                "reply_to_msg_id": message.reply_to_msg_id,
                "edit_date": message.edit_date,
                "reactions": _structured_reactions(message.reactions_display),
                "reaction_events": _structured_reaction_events(message),
                "reaction_events_status": message.reaction_events_status,
                "read_at": message.read_at,
                "read_markers": read_markers,
                "inline_markers": read_markers,
            }
        )
    return structured


def _identity_text_fact(value: object) -> str | None:
    return value if isinstance(value, str) else None


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

    try:
        async with daemon_connection() as conn:
            response = await conn.get_inbox(
                limit=args.limit,
                group_size_threshold=args.group_size_threshold,
                since_utc=applied_since_utc,
            )
    except DaemonNotRunningError as exc:
        return error_result(_daemon_not_running_text(exc), has_filter=applied_since_utc is not None)

    if err := _check_daemon_response(response):
        err.has_filter = applied_since_utc is not None
        return err

    data = response.get("data", {})
    groups = data.get("groups", [])
    # Defensive: older daemon responses or test mocks may omit bootstrap_pending.
    # Treat missing as 0 (full coverage assumed). Also guard against explicit None.
    bootstrap_pending = int(data.get("bootstrap_pending", 0) or 0)
    warnings = _bootstrap_pending_warnings(bootstrap_pending)
    structured_dialogs, hidden_count_by_dialog, result_message_count = _structured_inbox_groups(groups)
    structured_content = {
        "limit": args.limit,
        "group_size_threshold": args.group_size_threshold,
        "applied_since_utc": applied_since_utc,
        "bootstrap_pending": bootstrap_pending,
        "coverage": {
            "complete": bootstrap_pending == 0,
            "state": "complete" if bootstrap_pending == 0 else "partial",
            "bootstrap_pending_count": bootstrap_pending,
        },
        "warnings": warnings,
        "budget": {
            "requested_limit": args.limit,
            "result_message_count": result_message_count,
            "dialog_count": len(structured_dialogs),
            "hidden_count": sum(cast(int, item["hidden_count"]) for item in hidden_count_by_dialog),
            "hidden_count_by_dialog": hidden_count_by_dialog,
            "allocation_policy": "daemon allocates the requested unread message budget across dialogs",
        },
        "dialogs": structured_dialogs,
        "count": len(structured_dialogs),
        "result_count_semantics": "count is the number of unread dialogs returned; budget.result_message_count is the number of message rows shown",
    }

    if not groups:
        return structured_result(structured_content, result_count=0, has_filter=applied_since_utc is not None)

    return structured_result(
        structured_content,
        result_count=result_message_count,
        has_filter=applied_since_utc is not None,
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
