"""Normalized Telegram rich-message facts and their searchable text."""

from __future__ import annotations

import json
from typing import Literal, cast

from .message_composition import normalize_telegram_fact

RichStatus = Literal["unknown", "absent", "complete", "partial"]
RichTextStatus = Literal["complete", "unsupported"]

# Only presentation fields belong in the text projection. Other fields remain
# in the normalized tree, including URLs, media references and layout flags.
_TEXT_NODES = {
    "TextPlain",
    "TextBold",
    "TextItalic",
    "TextUnderline",
    "TextStrike",
    "TextFixed",
    "TextEmail",
    "TextUrl",
    "TextPhone",
    "TextSubscript",
    "TextSuperscript",
    "TextMarked",
    "TextSpoiler",
    "TextAnchor",
    "TextAutoEmail",
    "TextAutoPhone",
    "TextAutoUrl",
    "TextBankCard",
    "TextBotCommand",
    "TextCashtag",
    "TextHashtag",
    "TextDate",
    "TextMention",
    "TextMentionName",
    "TextButton",
    "PageButton",
    "PageListItemText",
    "PageListOrderedItemText",
    "PageTableCell",
    "PageBlockTitle",
    "PageBlockSubtitle",
    "PageBlockHeader",
    "PageBlockSubheader",
    "PageBlockParagraph",
    "PageBlockPreformatted",
    "PageBlockFooter",
    "PageBlockKicker",
    "PageBlockThinking",
    *(f"PageBlockHeading{i}" for i in range(1, 7)),
}
_FIELDS: dict[str, tuple[str, ...]] = {
    "RichMessage": ("blocks",),
    "TextConcat": ("texts",),
    "TextDiff": ("text", "old_text"),
    "PageBlockChannel": ("channel",),
    **dict.fromkeys(
        ("Channel", "ChannelForbidden", "Chat", "ChatForbidden", "Community", "CommunityForbidden"), ("title",)
    ),
    "ChatEmpty": (),
    "TextCustomEmoji": ("alt",),
    "TextMath": ("source",),
    "PageBlockMath": ("source",),
    "PageBlockAuthorDate": ("author",),
    "PageBlockBlockquote": ("text", "caption"),
    "PageBlockPullquote": ("text", "caption"),
    "PageBlockBlockquoteBlocks": ("blocks", "caption"),
    "PageBlockButtonRow": ("buttons",),
    "PageBlockList": ("items",),
    "PageBlockOrderedList": ("items",),
    "PageListItemBlocks": ("blocks",),
    "PageListOrderedItemBlocks": ("blocks",),
    "PageBlockTable": ("title", "rows"),
    "PageTableRow": ("cells",),
    "PageBlockDetails": ("title", "blocks"),
    "PageBlockCover": ("cover",),
    "PageBlockCollage": ("items", "caption"),
    "PageBlockSlideshow": ("items", "caption"),
    "PageBlockEmbedPost": ("author", "blocks", "caption"),
    "PageBlockRelatedArticles": ("title", "articles"),
    "PageRelatedArticle": ("title", "description", "author"),
    "PageCaption": ("text", "credit"),
    **dict.fromkeys(
        ("PageBlockPhoto", "PageBlockVideo", "PageBlockAudio", "PageBlockDocument", "PageBlockEmbed", "PageBlockMap"),
        ("caption",),
    ),
    **dict.fromkeys(_TEXT_NODES, ("text",)),
    **dict.fromkeys(("TextEmpty", "TextImage", "PageBlockDivider", "PageBlockAnchor"), ()),
}


def capture_rich_message(value: object) -> str:
    """Encode an observed rich message, including observed absence."""
    normalized = normalize_telegram_fact(value)
    if normalized is not None and not isinstance(normalized, dict):
        raise ValueError("rich message must be an object or null")
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _join_text(parts: list[tuple[str, bool]], separator: str) -> tuple[str, bool]:
    return separator.join(text for text, _ in parts if text), all(ok for _, ok in parts)


def _node_text(node: dict[str, object]) -> tuple[str, bool]:
    name = node.get("_")
    if not isinstance(name, str) or name not in _FIELDS:
        # Unknown constructors keep their raw facts; only an explicit text field
        # is safe to display until their semantics are supported.
        text, _ = _text(node.get("text"), "")
        return text, False
    child_separator = "" if name == "TextConcat" else "\t" if name == "PageTableRow" else "\n"
    parts = [_text(node.get(field), child_separator) for field in _FIELDS[name]]
    separator = "" if name.startswith("Text") and name != "TextDiff" else "\n"
    return _join_text(parts, separator)


def _text(value: object, separator: str = "\n") -> tuple[str, bool]:
    if value is None:
        return "", True
    if isinstance(value, str):
        return value, True
    if isinstance(value, list):
        return _join_text([_text(item, separator) for item in value], separator)
    if isinstance(value, dict):
        return _node_text(value)
    return "", False


def _source_partial(value: object) -> bool:
    if isinstance(value, list):
        return any(_source_partial(item) for item in value)
    if isinstance(value, dict):
        return value.get("_") == "PageBlockUnsupported" or any(_source_partial(item) for item in value.values())
    return False


def rich_message_text(tree: dict[str, object]) -> str:
    """Project user-visible text without interpreting metadata as text."""
    return _text(tree)[0]


def decode_rich_message(payload: str | None) -> tuple[dict[str, object] | None, RichStatus, RichTextStatus | None]:
    """Decode captured facts and report source and text coverage separately."""
    if payload is None:
        return None, "unknown", None
    value = cast(object, json.loads(payload))
    if value is None:
        return None, "absent", None
    if not isinstance(value, dict):
        raise ValueError("rich message must contain an object or null")
    tree = cast(dict[str, object], value)
    supported = _text(tree)[1]
    return (
        tree,
        "partial" if tree.get("part") or _source_partial(tree.get("blocks")) else "complete",
        "complete" if supported else "unsupported",
    )


def rich_media_reference_gaps(tree: dict[str, object]) -> dict[str, list[int]]:
    """List referenced photo/document IDs absent from this rich message's declarations."""
    declared: dict[str, set[int]] = {"photos": set(), "documents": set()}
    for kind, ids in declared.items():
        values = tree.get(kind)
        if isinstance(values, list):
            ids.update(
                identifier
                for item in values
                if isinstance(item, dict) and type(identifier := item.get("id")) is int and identifier != 0
            )
    fields = {
        "PageBlockPhoto": ("photos", "photo_id"),
        "PageBlockVideo": ("documents", "video_id"),
        "PageBlockAudio": ("documents", "audio_id"),
        "PageBlockDocument": ("documents", "document_id"),
        "PageBlockEmbed": ("photos", "poster_photo_id"),
        "PageBlockEmbedPost": ("photos", "author_photo_id"),
        "PageRelatedArticle": ("photos", "photo_id"),
    }
    missing: dict[str, set[int]] = {"photos": set(), "documents": set()}

    def visit(value: object) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            name = value.get("_")
            field = fields.get(name) if isinstance(name, str) else None
            if field is not None:
                kind, name = field
                identifier = value.get(name)
                if type(identifier) is int and identifier != 0 and identifier not in declared[kind]:
                    missing[kind].add(identifier)
            for item in value.values():
                visit(item)

    visit(tree.get("blocks"))
    return {
        "missing_photo_ids": sorted(missing["photos"]),
        "missing_document_ids": sorted(missing["documents"]),
    }
