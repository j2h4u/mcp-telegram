from typing import Literal, NotRequired, TypedDict

from ..message_content import ContentKind

TelegramContentKind = Literal[
    "message_text",
    "snippet",
    "media_description",
    "reply_snippet",
    "forward_snippet",
    "reaction",
    "about",
    "bio",
    "bot_description",
    "bot_command_description",
    "business_intro",
    "business_location",
    "private_forward_name",
    "restriction_reason",
    "note",
]

WarningSeverity = Literal["info", "warning", "action_required"]
NavigationDirection = Literal["older", "newer", "around", "forward", "backward"]

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

FOLDER_SNAPSHOT_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "generation": {"type": ["integer", "null"]},
        "status": {"type": "string", "enum": ["fresh", "stale", "unavailable"]},
        "completed_at": {"type": ["integer", "null"]},
        "age_seconds": {"type": ["integer", "null"]},
        "complete": {"type": "boolean"},
    },
    "required": ["generation", "status", "completed_at", "age_seconds", "complete"],
    "additionalProperties": False,
}


class TelegramContent(TypedDict):
    text: str
    is_telegram_content: Literal[True]
    content_kind: TelegramContentKind


class StructuredWarning(TypedDict):
    kind: str
    severity: WarningSeverity
    message: str
    action: NotRequired[str]


class NavigationMetadata(TypedDict):
    next_navigation: str | None
    has_more: bool
    direction: NotRequired[NavigationDirection]
    anchor_message_id: NotRequired[int]
    source_cursor: NotRequired[str]


class ResultCountSemantics(TypedDict):
    count: int
    result_count_semantics: str


def telegram_content(text: str, content_kind: TelegramContentKind) -> TelegramContent:
    return {
        "text": text,
        "is_telegram_content": True,
        "content_kind": content_kind,
    }


def unavailable_folder_snapshot() -> dict[str, object]:
    return {
        "generation": None,
        "status": "unavailable",
        "completed_at": None,
        "age_seconds": None,
        "complete": False,
    }


def serialize_message_content(
    text: str | None,
    media_description: str | None,
    kind: ContentKind,
) -> dict[str, TelegramContent | None]:
    """Serialize already-projected message facts for delivery surfaces.

    Projection (including hidden-link rendering) belongs to ``MessageContent``;
    this helper only applies the shared wire wrapper and primary-content rule.
    """
    primary = text if kind == "message_text" else media_description
    return {
        "content": telegram_content(primary, kind) if primary is not None and kind != "none" else None,
        "media": telegram_content(media_description, "media_description") if media_description is not None else None,
    }


def structured_warning(
    kind: str,
    message: str,
    *,
    severity: WarningSeverity = "warning",
    action: str | None = None,
) -> StructuredWarning:
    warning: StructuredWarning = {
        "kind": kind,
        "severity": severity,
        "message": message,
    }
    if action:
        warning["action"] = action
    return warning


def navigation_metadata(
    next_navigation: str | None,
    *,
    has_more: bool | None = None,
    direction: NavigationDirection | None = None,
    anchor_message_id: int | None = None,
    source_cursor: str | None = None,
) -> NavigationMetadata:
    metadata: NavigationMetadata = {
        "next_navigation": next_navigation,
        "has_more": next_navigation is not None if has_more is None else has_more,
    }
    if direction is not None:
        metadata["direction"] = direction
    if anchor_message_id is not None:
        metadata["anchor_message_id"] = anchor_message_id
    if source_cursor is not None:
        metadata["source_cursor"] = source_cursor
    return metadata


def result_count_semantics(count: int, semantics: str) -> ResultCountSemantics:
    return {
        "count": count,
        "result_count_semantics": semantics,
    }
