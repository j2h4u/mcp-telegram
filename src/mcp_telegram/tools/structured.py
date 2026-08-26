from typing import Literal, NotRequired, TypedDict, cast

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


AttachmentType = Literal["contact", "other"]
DeliveryContentKind = ContentKind | Literal["snippet"]


class Attachment(TypedDict):
    """Minimal canonical representation for any Telegram media attachment."""

    type: AttachmentType
    description: NotRequired[str]


class SerializedMessageContent(TypedDict):
    content: TelegramContent | None
    media: Attachment | None


MEDIA_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": ["contact", "other"]},
        "description": {"type": "string"},
    },
    "required": ["type"],
    "additionalProperties": False,
}


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
    kind: DeliveryContentKind,
    media_kind: str | None = None,
) -> SerializedMessageContent:
    """Serialize already-projected message facts for delivery surfaces.

    Projection (including hidden-link rendering) belongs to ``MessageContent``;
    this helper only applies the shared wire wrapper and primary-content rule.
    """
    media = project_media_description(media_description, media_kind)
    # Media is represented once by its attachment object.  Keep content for
    # captions/text only so the description cannot be duplicated.
    normalized_text = text or None
    content = None
    if normalized_text is not None and normalized_text != media_description:
        content_kind = "message_text" if kind in {"none", "media_description"} else cast(TelegramContentKind, kind)
        content = telegram_content(normalized_text, content_kind)
    elif text == "" and media_description is None and kind == "message_text":
        # Activity keeps its explicit empty-body envelope when there is no
        # attachment at all.
        content = telegram_content("", "message_text")
    return {
        "content": content,
        "media": media,
    }


def project_media_description(media_description: str | None, media_kind: str | None = None) -> Attachment | None:
    """Project persisted media facts into one stable agent-facing attachment."""
    if media_kind is None:
        return None
    attachment: Attachment = {"type": "contact" if media_kind == "contact" else "other"}
    if media_description:
        attachment["description"] = media_description
    return attachment


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
