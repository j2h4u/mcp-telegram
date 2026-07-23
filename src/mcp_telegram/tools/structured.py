from typing import Literal, NotRequired, TypedDict

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
