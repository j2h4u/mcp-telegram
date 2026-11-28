"""Make a separate filtered copy for sharing with ordinary chat members.

Run: uv run python -m devtools.public_chat_export ORIGINAL.json PUBLIC.json [REMOVED.json]
Works offline; cannot detect messages deleted after the original export.
"""

import argparse
import fcntl
import json
import os
import tempfile
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import TextIO, cast

import ijson  # type: ignore[import-untyped]

from mcp_telegram.chat_export_checkpoint import Checkpoint, base_records, fingerprint, read_checkpoint_options
from mcp_telegram.chat_export_identity import IdentityIndex
from mcp_telegram.chat_export_projection import omit_empty_fields
from mcp_telegram.chat_export_schema import IDENTITY_FORMAT_VERSION, SPARSE_FORMAT_VERSION, read_export_version

# Each nested object has its own allowlist. Scalar slots reject containers;
# Telegram adding a field never expands the public export implicitly.
type Projection = tuple[str, ...] | dict[str, Projection] | list[Projection] | Callable[[object], object]


def _project(value: object, projection: Projection) -> object:
    if value is None:
        return None
    if callable(projection):
        return projection(value)
    if isinstance(projection, list):
        return [_project(item, projection[0]) for item in cast(list[object], value)] if isinstance(value, list) else []
    if not isinstance(value, dict):
        return {}
    data = cast(dict[str, object], value)
    if isinstance(projection, tuple):
        return {
            key: data[key]
            for key in projection
            if key in data and isinstance(data[key], str | int | float | bool | type(None))
        }
    result = {}
    for key, child in projection.items():
        if key in data:
            item = _project(data[key], child)
            if item is not None or data[key] is None:
                result[key] = item
    return result


def _scalar(value: object) -> object:
    return value if isinstance(value, str | int | float | bool | type(None)) else None


def _role(value: object) -> object:
    return value if isinstance(value, str) and value in {"owner", "admin", "member", "unknown"} else None


def _fields(*names: str) -> dict[str, Projection]:
    return dict.fromkeys(names, _scalar)


def _constructors(value: object, constructors: dict[str, Projection]) -> object:
    if not isinstance(value, dict):
        return {}
    name = cast(dict[str, object], value).get("_")
    return _project(value, constructors[name]) if isinstance(name, str) and name in constructors else {}


IDENTITY = {**_fields("id", "kind", "name", "username", "is_admin", "rank"), "role": _role, "metadata": ("label",)}
GROUP = ("dialog_id", "kind", "title")
ENTITY = ("_", "offset", "length", "url", "language", "user_id", "document_id", "collapsed")
TEXT = {**_fields("_", "text"), "entities": [ENTITY]}
OPTION = ("encoding", "data")


def _text(value: object) -> object:
    return _project(value, TEXT) if isinstance(value, dict) else _scalar(value)


def _option(value: object) -> object:
    return _project(value, OPTION) if isinstance(value, dict) else _scalar(value)


def _peer(value: object) -> object:
    return _constructors(
        value, {"PeerUser": ("_", "user_id"), "PeerChat": ("_", "chat_id"), "PeerChannel": ("_", "channel_id")}
    )


def _reaction(value: object) -> object:
    if isinstance(value, str):
        return value
    return _constructors(
        value, {"ReactionEmoji": ("_", "emoticon"), "ReactionCustomEmoji": ("_", "document_id"), "ReactionPaid": ("_",)}
    )


def _boolean(value: object) -> object:
    return value if isinstance(value, bool) else None


def _encoded(value: object) -> object:
    return _project(value, ("data", "encoding")) if isinstance(value, dict) else _scalar(value)


def _page(value: object) -> object:
    return _constructors(value, {"Page": PAGE})


def _web_attribute(value: object) -> object:
    return _constructors(
        value, {"WebPageAttributeStickerSet": {**_fields("_", "emojis", "text_color"), "stickers": [_document]}}
    )


def _size(value: object) -> object:
    return _constructors(
        value,
        {
            "PhotoSize": ("_", "type", "w", "h", "size"),
            "PhotoSizeProgressive": {**_fields("_", "type", "w", "h"), "sizes": [_scalar]},
            "PhotoStrippedSize": {**_fields("_", "type", "w", "h", "bytes_length", "bytes_omitted"), "bytes": _encoded},
            "PhotoPathSize": {**_fields("_", "type", "w", "h"), "bytes": _encoded},
            "VideoSize": ("_", "type", "w", "h", "size", "video_start_ts"),
        },
    )


def _stickerset(value: object) -> object:
    return _constructors(value, {"InputStickerSetID": ("_", "id")})


PHOTO = {**_fields("_", "id", "date", "has_stickers", "dc_id"), "sizes": [_size], "video_sizes": [_size]}
ATTRIBUTE_CONSTRUCTORS: dict[str, Projection] = {
    "DocumentAttributeFilename": ("_", "file_name"),
    "DocumentAttributeImageSize": ("_", "w", "h"),
    "DocumentAttributeVideo": (
        "_",
        "duration",
        "w",
        "h",
        "round_message",
        "supports_streaming",
        "nosound",
        "video_codec",
        "preload_prefix_size",
        "video_start_ts",
    ),
    "DocumentAttributeAudio": {**_fields("_", "duration", "voice", "title", "performer"), "waveform": _encoded},
    "DocumentAttributeSticker": {
        **_fields("_", "alt", "mask"),
        "stickerset": _stickerset,
        "mask_coords": ("_", "n", "x", "y", "zoom"),
    },
    "DocumentAttributeAnimated": ("_",),
}


def _attribute(value: object) -> object:
    return _constructors(value, ATTRIBUTE_CONSTRUCTORS)


DOCUMENT = {
    **_fields("_", "id", "date", "mime_type", "size", "dc_id"),
    "attributes": [_attribute],
    "thumbs": [_size],
    "video_thumbs": [_size],
}


def _media(value: object) -> object:
    if isinstance(value, dict) and isinstance(value.get("_"), str) and value["_"] in PUBLIC_MEDIA_CONSTRUCTORS:
        return _constructors(value, PUBLIC_MEDIA_CONSTRUCTORS)
    return _project(value, MEDIA)


def _geo(value: object) -> object:
    return _constructors(value, {"GeoPoint": ("_", "lat", "long", "accuracy_radius"), "GeoPointEmpty": ("_",)})


PUBLIC_MEDIA_CONSTRUCTORS: dict[str, Projection] = {
    "MessageMediaGeo": {"_": _scalar, "geo": _geo},
    "MessageMediaGeoLive": {**_fields("_", "period", "heading", "proximity_notification_radius"), "geo": _geo},
    "MessageMediaVenue": {**_fields("_", "title", "address", "provider", "venue_id", "venue_type"), "geo": _geo},
    "MessageMediaContact": ("_", "phone_number", "first_name", "last_name", "vcard", "user_id"),
    "MessageMediaDice": ("_", "value", "emoticon"),
}


def _photo(value: object) -> object:
    return _constructors(value, {"Photo": PHOTO, "PhotoEmpty": ("_", "id")})


def _document(value: object) -> object:
    return _constructors(value, {"Document": DOCUMENT, "DocumentEmpty": ("_", "id")})


POLL = {
    **_fields(
        "_",
        "id",
        "closed",
        "public_voters",
        "multiple_choice",
        "quiz",
        "close_period",
        "close_date",
        "hide_results_until_close",
        "revoting_disabled",
        "shuffle_answers",
        "subscribers_only",
        "open_answers",
        "hash",
    ),
    "countries_iso2": [_scalar],
    "question": _text,
    "answers": [{**_fields("_", "date"), "text": _text, "option": _option, "media": _media, "added_by": _peer}],
}
POLL_RESULTS = {
    **_fields("_", "total_voters", "min"),
    "results": [{**_fields("_", "voters", "correct"), "option": _option}],
    "solution": _scalar,
    "solution_entities": [ENTITY],
    "solution_media": _media,
}
MEDIA = {
    **_fields(
        "_",
        "spoiler",
        "ttl_seconds",
        "voice",
        "video",
        "round",
        "video_timestamp",
        "force_large_media",
        "force_small_media",
        "manual",
        "safe",
        "live_photo",
        "nopremium",
    ),
    "photo": PHOTO,
    "document": DOCUMENT,
    "alt_documents": [_document],
    "video_cover": _photo,
    "attached_media": _media,
    "poll": POLL,
    "results": POLL_RESULTS,
    "webpage": {
        **_fields(
            "_",
            "url",
            "display_url",
            "type",
            "site_name",
            "title",
            "description",
            "author",
            "duration",
            "has_large_media",
            "video_cover_photo",
            "embed_url",
            "embed_type",
            "embed_width",
            "embed_height",
            "id",
            "hash",
        ),
        "photo": _photo,
        "document": _document,
        "cached_page": _page,
        "attributes": [_web_attribute],
    },
}
FORWARD = {**_fields("_", "date", "from_name", "channel_post", "post_author", "imported", "psa_type"), "from_id": _peer}


def _rich_text(value: object) -> object:
    return value if isinstance(value, str) else _rich_node(value)


def _rich_node(value: object) -> object:
    return _constructors(value, RICH_CONSTRUCTORS)


RICH_CONSTRUCTORS: dict[str, Projection] = {
    "TextPlain": ("_", "text"),
    "TextConcat": {"_": _scalar, "texts": [_rich_node]},
    **{name: {"_": _scalar, "text": _rich_text} for name in ("TextBold", "TextFixed", "TextItalic", "TextAutoUrl")},
    "TextUrl": {**_fields("_", "url", "webpage_id"), "text": _rich_text},
    **{name: {"_": _scalar, "text": _rich_text} for name in ("PageBlockParagraph", "PageBlockHeading6")},
    "PageBlockDivider": ("_",),
    "PageBlockList": {"_": _scalar, "items": [_rich_node]},
    "PageListItemBlocks": {"_": _scalar, "checkbox": _boolean, "checked": _boolean, "blocks": [_rich_node]},
}


RICH_CONSTRUCTORS.update(
    {
        "TextEmpty": ("_",),
        **{
            name: {"_": _scalar, "text": _rich_text}
            for name in ("TextUnderline", "TextStrike", "TextSubscript", "TextSuperscript", "TextSpoiler")
        },
        "TextAnchor": {**_fields("_", "name"), "text": _rich_text},
        "TextEmail": {**_fields("_", "email"), "text": _rich_text},
        "TextPhone": {**_fields("_", "phone"), "text": _rich_text},
        "TextImage": ("_", "document_id", "w", "h"),
        "PageCaption": {"_": _scalar, "text": _rich_text, "credit": _rich_text},
        **{
            name: {"_": _scalar, "text": _rich_text}
            for name in (
                "PageBlockTitle",
                "PageBlockSubtitle",
                "PageBlockHeader",
                "PageBlockSubheader",
                "PageBlockFooter",
            )
        },
        "PageBlockAuthorDate": {**_fields("_", "published_date"), "author": _rich_text},
        "PageBlockAnchor": ("_", "name"),
        "PageBlockBlockquote": {**_fields("_", "collapsed"), "text": _rich_text, "caption": _rich_text},
        "PageBlockPullquote": {"_": _scalar, "text": _rich_text, "caption": _rich_text},
        "PageBlockPhoto": {**_fields("_", "photo_id", "spoiler", "url", "webpage_id"), "caption": _rich_node},
        "PageBlockVideo": {**_fields("_", "video_id", "autoplay", "loop", "spoiler"), "caption": _rich_node},
        "PageBlockCover": {"_": _scalar, "cover": _rich_node},
        **{
            name: {"_": _scalar, "items": [_rich_node], "caption": _rich_node}
            for name in ("PageBlockCollage", "PageBlockSlideshow")
        },
        "PageBlockDetails": {**_fields("_", "open"), "blocks": [_rich_node], "title": _rich_text},
        "PageListItemText": {**_fields("_", "checkbox", "checked"), "text": _rich_text},
        "PageListOrderedItemText": {**_fields("_", "num", "checkbox", "checked", "type", "value"), "text": _rich_text},
        "PageListOrderedItemBlocks": {**_fields("_", "num"), "blocks": [_rich_node]},
        "PageBlockOrderedList": {**_fields("_", "start", "reversed", "type"), "items": [_rich_node]},
        "PageTableRow": {"_": _scalar, "cells": [_rich_node]},
        "PageTableCell": {
            **_fields(
                "_", "align_center", "align_right", "colspan", "header", "rowspan", "valign_bottom", "valign_middle"
            ),
            "text": _rich_text,
        },
        "PageBlockTable": {**_fields("_", "bordered", "striped", "compact"), "title": _rich_text, "rows": [_rich_node]},
        "PageRelatedArticle": (
            "_",
            "url",
            "webpage_id",
            "title",
            "description",
            "photo_id",
            "author",
            "published_date",
        ),
        "PageBlockRelatedArticles": {"_": _scalar, "title": _rich_text, "articles": [_rich_node]},
    }
)
PAGE = {
    **_fields("_", "part", "rtl", "v2", "url", "views"),
    "blocks": [_rich_node],
    "photos": [_photo],
    "documents": [_document],
}


def _rich(value: object) -> object:
    return _constructors(
        value,
        {
            "RichMessage": {
                **_fields("_", "part", "rtl"),
                "blocks": [_rich_node],
                "documents": [_document],
                "photos": [_photo],
            }
        },
    )


def _button(value: object) -> object:
    return _constructors(value, {"KeyboardInlineButton": {**_fields("_", "text", "style"), "type": _button_type}})


def _button_type(value: object) -> object:
    return _constructors(value, {"InlineButtonTypeUrl": ("_", "url")})


def _button_row(value: object) -> object:
    return _constructors(value, {"KeyboardInlineButtonRow": {"_": _scalar, "buttons": [_button]}})


def _markup(value: object) -> object:
    return _constructors(value, {"ReplyInlineMarkup": {**_fields("_", "force_reply"), "rows": [_button_row]}})


REACTION_EVENT = {**_fields("_", "date"), "big": _boolean, "peer_id": _peer, "reaction": _reaction}


def _reaction_event(value: object) -> object:
    if (
        isinstance(value, dict)
        and set(value) == {"reactor"}
        and type(value["reactor"]) is int
        and value["reactor"] >= 0
    ):
        return dict(value)
    return _project(value, REACTION_EVENT)


def _top_reactors(value: object) -> object:
    if not isinstance(value, list):
        return []
    result = []
    for reactor in value:
        if not isinstance(reactor, dict) or reactor.get("_") != "MessageReactor" or reactor.get("top") is not True:
            continue
        public = cast(
            dict[str, object], _project(reactor, {**_fields("_", "count", "top", "anonymous"), "peer_id": _peer})
        )
        # Anonymous self-reactions carry our peer; non-top entries are also viewer-specific.
        if reactor.get("anonymous") is not False:
            public.pop("peer_id", None)
        result.append(public)
    return result


REACTIONS_AGGREGATE = {
    **_fields("_", "min"),
    "results": [{**_fields("_", "count"), "reaction": _reaction}],
    "recent_reactions": [_reaction_event],
    "top_reactors": _top_reactors,
}


MESSAGE_METADATA = {
    **_fields(
        "views",
        "forwards",
        "pinned",
        "post",
        "post_author",
        "via_bot_id",
        "from_rank",
        "summary_from_language",
        "edit_hide",
        "invert_media",
        "silent",
        "_",
        "legacy",
        "noforwards",
        "offline",
        "video_processing_pending",
        "paid_suggested_post_stars",
        "paid_suggested_post_ton",
        "from_boosts_applied",
        "effect",
        "paid_message_stars",
        "ttl_period",
        "via_business_bot_id",
        "reactions_are_possible",
        "report_delivery_until_date",
    ),
    "from_id": _peer,
    "peer_id": _peer,
    "factcheck": {**_fields("_", "hash", "need_check", "country"), "text": _text},
    "restriction_reason": [("_", "platform", "reason", "text")],
    "reactions": REACTIONS_AGGREGATE,
    "guestchat_via_from": _peer,
    "rich_message": _rich,
    "reply_markup": _markup,
    "replies": {
        **_fields("_", "replies", "comments", "channel_id", "max_id", "replies_pts"),
        "recent_repliers": [_peer],
    },
    "fwd_from": FORWARD,
    "reply_to": {
        **_fields(
            "_",
            "quote",
            "quote_text",
            "quote_offset",
            "forum_topic",
            "reply_to_top_id",
            "reply_to_msg_id",
            "reply_to_ephemeral",
            "reply_to_scheduled",
            "todo_item_id",
        ),
        "reply_to_peer_id": _peer,
        "poll_option": _option,
        "reply_media": _media,
        "quote_entities": [ENTITY],
        "reply_from": FORWARD,
    },
    "media": _media,
}
ACTION_CONSTRUCTORS: dict[str, Projection] = {
    "MessageActionPinMessage": ("_",),
    "MessageActionInviteToGroupCall": {"_": _scalar, "users": [_scalar], "call": ("_", "id")},
    "MessageActionSetMessagesTTL": ("_", "period"),
    "MessageActionGroupCall": {**_fields("_", "duration"), "call": ("_", "id")},
    "MessageActionGroupCallScheduled": {**_fields("_", "schedule_date"), "call": ("_", "id")},
    "MessageActionTopicCreate": ("_", "title", "icon_color", "icon_emoji_id", "title_missing"),
    "MessageActionTopicEdit": ("_", "title", "icon_emoji_id", "closed", "hidden", "title_missing"),
    "MessageActionChatAddUser": {"_": _scalar, "users": [_scalar]},
    "MessageActionChatCreate": {"_": _scalar, "title": _scalar, "users": [_scalar]},
    "MessageActionChatDeleteUser": ("_", "user_id"),
    "MessageActionChatJoinedByLink": ("_",),
    "MessageActionChatJoinedByRequest": ("_",),
    "MessageActionChatEditTitle": ("_", "title"),
    "MessageActionChatEditPhoto": {"_": _scalar, "photo": PHOTO},
    "MessageActionChatDeletePhoto": ("_",),
    "MessageActionChannelCreate": ("_", "title"),
    "MessageActionChannelMigrateFrom": ("_", "title", "chat_id"),
    "MessageActionChatMigrateTo": ("_", "channel_id"),
}


def _action(value: object) -> object:
    return _constructors(value, ACTION_CONSTRUCTORS)


def _identity_fields(prefix: str) -> dict[str, Projection]:
    return {f"{prefix}{key}": child for key, child in IDENTITY.items()}


def _reactor(value: object) -> object:
    return _project(
        value,
        {
            **_identity_fields("actor_"),
            "actor": _scalar,
            "date": _scalar,
            "reaction": _reaction,
            "raw": _reaction_event,
        },
    )


def _identity_or_reference(value: object) -> object:
    if type(value) is int and value >= 0:
        return value
    return _project(value, IDENTITY)


MESSAGE = {
    **_fields(
        "date",
        "dialog_id",
        "kind",
        "topic_id",
        "message_id",
        "message_key",
        "text",
        "edited_at",
        "grouped_id",
        "reply_to_dialog_id",
        "reply_to_message_id",
        "reply_key",
    ),
    **_identity_fields("author_"),
    "author": _scalar,
    "entities": [ENTITY],
    "service_action": _action,
    "topic": ("id", "topic_id", "title"),
    "related_users": [_identity_or_reference],
    "metadata": MESSAGE_METADATA,
    "reactions": {"aggregate": REACTIONS_AGGREGATE},
    "reactors": [_reactor],
}
HEADER_METADATA = {"order": _scalar, "peers": [GROUP], "exporter": ("name", "version", "repository_url")}


def public_facts(value: object, projection: Projection) -> object:
    """Publish only fields approved for this exact position in the export."""
    return _project(value, projection)


def _removed(original: object, public: object, path: str) -> Iterator[dict[str, object]]:
    """Diff source facts; emit missing subtrees once, with original array indices."""
    if isinstance(original, dict) and isinstance(public, dict):
        published = cast(dict[str, object], public)
        for key, value in cast(dict[str, object], original).items():
            pointer = f"{path}/{key.replace('~', '~0').replace('/', '~1')}"
            if key not in published:
                yield {"path": pointer, "value": value}
            else:
                yield from _removed(value, published[key], pointer)
    elif isinstance(original, list) and isinstance(public, list):
        if len(original) != len(public):
            yield {"path": path, "value": original}
            return
        published_items = cast(list[object], public)
        for index, value in enumerate(cast(list[object], original)):
            pointer = f"{path}/{index}"
            if index >= len(published_items):
                yield {"path": pointer, "value": value}
            else:
                yield from _removed(value, published_items[index], pointer)
    elif type(original) is not type(public) or original != public:
        yield {"path": path, "value": original}


def _remap_public_refs(record: dict[str, object], remap: Callable[[object], int]) -> None:
    if "author" in record:
        record["author"] = remap(record["author"])
    if "related_users" in record:
        record["related_users"] = [remap(ref) for ref in cast(list[object], record["related_users"])]
    for reactor in cast(list[dict[str, object]], record.get("reactors", [])):
        if "actor" in reactor:
            reactor["actor"] = remap(reactor["actor"])


def _dump_identities(stream: TextIO, index: IdentityIndex) -> None:
    for position, identity in enumerate(index.records()):
        if position:
            stream.write(",")
        json.dump(identity, stream, ensure_ascii=False, separators=(",", ":"))


def _write_public_export(  # noqa: PLR0912, PLR0915
    path: Path, stream: TextIO, removed_stream: TextIO, source_index: IdentityIndex, public_index: IdentityIndex
) -> dict[str, int]:
    counts = {"messages": 0, "admin_events": 0, "reactors": 0}
    declared = None
    first_removed = True
    format_version = read_export_version(path)

    def project(record: object, projection: Projection) -> object:
        result = public_facts(record, projection)
        return omit_empty_fields(result) if format_version >= SPARSE_FORMAT_VERSION else result

    next_public_identity = 0

    def remap(reference: object) -> int:
        nonlocal next_public_identity
        identity = cast(dict[str, object], project(source_index.get(reference), IDENTITY))
        # Keep v4 explicit empty facts; IdentityIndex.intern intentionally sparsifies them.
        snapshot = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        row = cast(
            tuple[int] | None,
            public_index.db.execute(
                "SELECT id FROM identities WHERE snapshot=? ORDER BY id LIMIT 1", (snapshot,)
            ).fetchone(),
        )
        if row is not None:
            return row[0] - 1
        position = next_public_identity
        public_index.put_at(position, identity)
        next_public_identity += 1
        return position

    if format_version >= IDENTITY_FORMAT_VERSION:
        next_source_identity = 0
        for kind, record in base_records(path, expand_identities=False):
            if kind == "identities.item":
                source_index.put_at(next_source_identity, record)
                next_source_identity += 1
            elif kind == "messages.item":
                _remap_public_refs(cast(dict[str, object], project(record, MESSAGE)), remap)
    stream.write(f'{{"format_version":{format_version}')
    removed_stream.write('{"format_version":1,"removed":[')
    messages_started = format_version < IDENTITY_FORMAT_VERSION

    def write_removed(items: Iterator[dict[str, object]]) -> None:
        nonlocal first_removed
        for item in items:
            if not first_removed:
                removed_stream.write(",")
            json.dump(item, removed_stream, ensure_ascii=False, separators=(",", ":"))
            first_removed = False

    def write_identity_removals() -> None:
        nonlocal first_removed
        changed = source_index.count() != public_index.count() or any(
            identity != public_index.get(position) for position, identity in enumerate(source_index.records())
        )
        if changed:
            if not first_removed:
                removed_stream.write(",")
            removed_stream.write('{"path":"/identities","value":[')
            _dump_identities(removed_stream, source_index)
            removed_stream.write("]}")
            first_removed = False

    def start_messages() -> None:
        nonlocal messages_started
        if not messages_started:
            stream.write(
                '],"messages":[' if format_version >= SPARSE_FORMAT_VERSION else '],"admin_events":[],"messages":['
            )
            messages_started = True

    if format_version >= SPARSE_FORMAT_VERSION:
        with path.open("rb") as source:
            has_admins = False
            events = cast(Iterator[tuple[str, str, object]], ijson.parse(source))  # pyright: ignore[reportAny]
            for prefix, event, value in events:
                if prefix == "" and event == "map_key":
                    if value == "admin_events":
                        has_admins = True
                        break
                    if value == "messages":
                        break
        if has_admins:
            removed_stream.write('{"path":"/admin_events","value":[')
            first_admin = True
            for kind, record in base_records(path, expand_identities=False):
                if kind == "admin_events.item":
                    if not first_admin:
                        removed_stream.write(",")
                    json.dump(record, removed_stream, ensure_ascii=False, separators=(",", ":"))
                    first_admin = False
                elif kind == "messages.item":
                    break
            removed_stream.write("]}")
            first_removed = False

    for kind, record in base_records(path, expand_identities=False):
        if kind in {"group", "metadata"}:
            source = record
            public = project(record, GROUP if kind == "group" else HEADER_METADATA)
            write_removed(_removed(source, public, f"/{kind}"))
            stream.write(f',"{kind}":')
            json.dump(public, stream, ensure_ascii=False, separators=(",", ":"))
            if kind == "metadata":
                stream.write(
                    ',"identities":['
                    if format_version >= IDENTITY_FORMAT_VERSION
                    else ',"admin_events":[],"messages":['
                )
                if format_version >= IDENTITY_FORMAT_VERSION:
                    _dump_identities(stream, public_index)
                    write_identity_removals()
        elif kind == "identities.item":
            continue
        elif kind == "admin_events.item":
            if format_version < SPARSE_FORMAT_VERSION:
                write_removed(iter([{"path": f"/admin_events/{counts['admin_events']}", "value": record}]))
            counts["admin_events"] += 1
        elif kind == "messages.item":
            start_messages()
            public = cast(dict[str, object], project(record, MESSAGE))
            if format_version >= IDENTITY_FORMAT_VERSION:
                _remap_public_refs(public, remap)
            write_removed(_removed(record, public, f"/messages/{counts['messages']}"))
            if counts["messages"]:
                stream.write(",")
            json.dump(public, stream, ensure_ascii=False, separators=(",", ":"))
            counts["messages"] += 1
            counts["reactors"] += len(cast(list[object], record.get("reactors", [])))
        elif kind == "export":
            declared = record
    if declared != counts:
        raise ValueError("Export counts do not match its records")
    start_messages()
    counts["admin_events"] = 0
    write_removed(_removed(declared, counts, "/export"))
    stream.write('],"export":')
    json.dump(counts, stream, separators=(",", ":"))
    stream.write("}\n")
    removed_stream.write("]}\n")
    return counts


def _sync_directory(path: Path) -> None:
    directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _create_pair_checkpoint(path: Path, options: dict[str, object]) -> int:
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for suffix in ("-journal", "-wal", "-shm"):
            auxiliary = path.with_name(path.name + suffix)
            if auxiliary.exists() or auxiliary.is_symlink():
                raise FileExistsError(f"Public export checkpoint auxiliary path already exists: {auxiliary}")
        checkpoint = Checkpoint(temporary)
        try:
            checkpoint.mark("options", options)
        finally:
            checkpoint.close()
        os.link(temporary, path)
        _sync_directory(path.parent)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise
    finally:
        temporary.unlink(missing_ok=True)


def _pair_checkpoint(path: Path, options: dict[str, object]) -> tuple[Checkpoint, int]:
    if not path.exists() and not path.is_symlink():
        descriptor = _create_pair_checkpoint(path, options)
    else:
        descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(descriptor)
            raise
    try:
        saved = read_checkpoint_options(path)
        if any(saved.get(key) != options[key] for key in ("source", "private", "public")):
            raise ValueError("Public export checkpoint belongs to another operation")
        return Checkpoint(path), descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _stage_pair(checkpoint: Checkpoint, source: Path, destinations: tuple[Path, Path]) -> None:
    staged: list[Path] = []
    provisional: list[list[object]] = []
    retained = False
    try:
        with ExitStack() as stack:
            streams = []
            for destination in destinations:
                descriptor, name = tempfile.mkstemp(
                    prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
                )
                staged.append(Path(name))
                streams.append(stack.enter_context(os.fdopen(descriptor, "w", encoding="utf-8")))
                identity = os.fstat(descriptor)
                provisional.append([name, [identity.st_dev, identity.st_ino]])
                _sync_directory(destination.parent)
                checkpoint.mark("staging", provisional)
            source_index = stack.enter_context(IdentityIndex())
            public_index = stack.enter_context(IdentityIndex())
            counts = _write_public_export(source, streams[1], streams[0], source_index, public_index)
            for stream in streams:
                stream.flush()
                os.fsync(stream.fileno())
        for parent in {item.parent for item in staged}:
            _sync_directory(parent)
        records = [[str(item), fingerprint(item)] for item in staged]
        if any(record[1] is None for record in records):
            raise FileNotFoundError("Public export staging file disappeared")
        retained = True
        checkpoint.mark_many({"stages": records, "counts": counts, "staging": None})
    finally:
        if not retained:
            _discard_provisional(provisional, destinations)


def _valid_staging_path(temporary: Path, destination: Path) -> bool:
    return (
        temporary.parent == destination.parent
        and temporary.name.startswith(f".{destination.name}.")
        and temporary.name.endswith(".tmp")
    )


def _discard_provisional(staging: list[list[object]], destinations: tuple[Path, Path]) -> None:
    for stage, destination in zip(staging, destinations, strict=False):
        temporary = Path(cast(str, stage[0]))
        if not _valid_staging_path(temporary, destination):
            continue
        try:
            identity = temporary.lstat()
        except FileNotFoundError:
            continue
        if [identity.st_dev, identity.st_ino] == stage[1] and not temporary.is_symlink():
            temporary.unlink()
            _sync_directory(temporary.parent)


def _publish_pair_file(stage: list[object], destination: Path) -> None:
    temporary = Path(cast(str, stage[0]))
    if not _valid_staging_path(temporary, destination):
        raise ValueError("Invalid public export staging path")
    staged = fingerprint(temporary)
    current = fingerprint(destination)
    if staged != stage[1] and not (staged is None and current == stage[1]):
        raise ValueError("Public export staging file changed")
    if current is None:
        os.link(temporary, destination)
    elif current != stage[1]:
        raise FileExistsError(f"Public export destination changed: {destination}")
    _sync_directory(destination.parent)


def _pair_stages(checkpoint: Checkpoint, destinations: tuple[Path, Path]) -> list[list[object]]:
    stages = cast(list[list[object]], checkpoint.state("stages"))
    if len(stages) != len(destinations) or any(stage[1] is None for stage in stages):
        raise ValueError("Invalid public export checkpoint")
    return stages


def _finish_pair(checkpoint: Checkpoint, destinations: tuple[Path, Path]) -> dict[str, int]:
    stages = _pair_stages(checkpoint, destinations)
    for stage, destination in zip(stages, destinations, strict=True):
        _publish_pair_file(stage, destination)
    return cast(dict[str, int], checkpoint.state("counts"))


def _discard_pair(checkpoint: Checkpoint, destinations: tuple[Path, Path]) -> None:
    stages = _pair_stages(checkpoint, destinations)
    for stage, destination in reversed(tuple(zip(stages, destinations, strict=True))):
        temporary = Path(cast(str, stage[0]))
        if not _valid_staging_path(temporary, destination):
            raise ValueError("Invalid public export staging path")
        if not destination.is_symlink() and fingerprint(destination) == stage[1]:
            destination.unlink()
        _sync_directory(destination.parent)
    for stage, destination in zip(stages, destinations, strict=True):
        temporary = Path(cast(str, stage[0]))
        if not temporary.is_symlink() and fingerprint(temporary) == stage[1]:
            temporary.unlink()
            _sync_directory(destination.parent)


def _ensure_pair_staged(checkpoint: Checkpoint, source: Path, destinations: tuple[Path, Path]) -> None:
    if checkpoint.state("stages") is not None:
        return
    provisional = checkpoint.state("staging")
    if provisional is not None:
        _discard_provisional(cast(list[list[object]], provisional), destinations)
        checkpoint.mark("staging", None)
    if any(item.exists() or item.is_symlink() for item in destinations):
        raise FileExistsError("Output already exists")
    _stage_pair(checkpoint, source, destinations)


def _rollback_pair(checkpoint: Checkpoint, checkpoint_path: Path, destinations: tuple[Path, Path]) -> None:
    checkpoint.mark("rollback", True)
    if checkpoint.state("stages") is not None:
        _discard_pair(checkpoint, destinations)
    staging = checkpoint.state("staging")
    if staging is not None:
        _discard_provisional(cast(list[list[object]], staging), destinations)
    checkpoint.close()
    checkpoint_path.unlink()
    _sync_directory(checkpoint_path.parent)


def _owned_pair_published(checkpoint: Checkpoint, destinations: tuple[Path, Path]) -> bool:
    stages = checkpoint.state("stages")
    if stages is None:
        return False
    return any(
        stage[1] is not None and not destination.is_symlink() and fingerprint(destination) == stage[1]
        for stage, destination in zip(cast(list[list[object]], stages), destinations, strict=True)
    )


def _check_pair_namespace(source: Path, destinations: tuple[Path, Path], checkpoint_path: Path) -> None:
    reserved = {checkpoint_path.with_name(checkpoint_path.name + suffix) for suffix in ("", "-journal", "-wal", "-shm")}
    if any(candidate in reserved or candidate.resolve() in reserved for candidate in (source, *destinations)):
        raise ValueError("Source or output collides with public export checkpoint")


def _prepare_pair(
    checkpoint: Checkpoint, checkpoint_path: Path, destinations: tuple[Path, Path], options: dict[str, object]
) -> bool:
    if checkpoint.state("rollback"):
        _rollback_pair(checkpoint, checkpoint_path, destinations)
        return False
    saved = cast(dict[str, object], checkpoint.state("options"))
    if saved["source_fingerprint"] != options["source_fingerprint"]:
        if not _owned_pair_published(checkpoint, destinations):
            _rollback_pair(checkpoint, checkpoint_path, destinations)
        raise ValueError("Input changed since public export started")
    return True


def _publish_owned_pair(
    checkpoint: Checkpoint,
    checkpoint_path: Path,
    source: Path,
    destinations: tuple[Path, Path],
    source_fingerprint: object,
) -> dict[str, int]:
    try:
        _ensure_pair_staged(checkpoint, source, destinations)
        if fingerprint(source) != source_fingerprint:
            raise ValueError("Input changed during filtering")
        counts = _finish_pair(checkpoint, destinations)
    except Exception:
        _rollback_pair(checkpoint, checkpoint_path, destinations)
        raise
    stages = cast(list[list[object]], checkpoint.state("stages"))
    checkpoint.close()
    for stage in stages:
        Path(cast(str, stage[0])).unlink(missing_ok=True)
    checkpoint_path.unlink()
    _sync_directory(checkpoint_path.parent)
    return counts


def sanitize_export(path: Path, output: Path, removed_output: Path | None = None) -> dict[str, int]:
    """Stage both new files; publish private removals first and public facts last."""
    removed_output = removed_output if removed_output is not None else output.with_name(f"{output.stem}.removed.json")
    destinations = (removed_output, output)
    paths = (path, *destinations)
    if len({item.resolve() for item in paths}) != len(paths):
        raise ValueError("Source and both outputs must differ")
    path = path.resolve()
    destinations = (
        destinations[0].parent.resolve() / destinations[0].name,
        destinations[1].parent.resolve() / destinations[1].name,
    )
    checkpoint_path = destinations[1].with_name(f".{destinations[1].name}.public-resume.sqlite3")
    _check_pair_namespace(path, destinations, checkpoint_path)
    if (
        not checkpoint_path.exists()
        and not checkpoint_path.is_symlink()
        and any(item.exists() or item.is_symlink() for item in destinations)
    ):
        raise FileExistsError("Output already exists")
    options: dict[str, object] = {
        "source": str(path),
        "source_fingerprint": fingerprint(path),
        "private": str(destinations[0]),
        "public": str(destinations[1]),
    }
    checkpoint, descriptor = _pair_checkpoint(checkpoint_path, options)
    try:
        if not _prepare_pair(checkpoint, checkpoint_path, destinations, options):
            return sanitize_export(path, destinations[1], destinations[0])
        return _publish_owned_pair(checkpoint, checkpoint_path, path, destinations, options["source_fingerprint"])
    finally:
        checkpoint.close()
        os.close(descriptor)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("removed_output", nargs="?", type=Path)
    args = parser.parse_args()
    print(
        json.dumps(
            sanitize_export(cast(Path, args.export), cast(Path, args.output), cast(Path | None, args.removed_output))
        )
    )
