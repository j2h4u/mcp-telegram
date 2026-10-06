"""Make a separate filtered copy for sharing with ordinary chat members.

Run: uv run python -m devtools.public_chat_export ORIGINAL.json PUBLIC.json
Works offline; cannot detect messages deleted after the original export.
"""

import argparse
import json
import os
import tempfile
from collections.abc import Callable
from importlib.metadata import version
from pathlib import Path
from typing import TextIO, cast

from mcp_telegram.chat_export_checkpoint import base_records, fingerprint
from mcp_telegram.chat_export_projection import project_exporter

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
    return value if isinstance(value, str) and value in {"owner", "admin", "member"} else None


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


PHOTO = ("_", "id", "date", "has_stickers")
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
    ),
    "DocumentAttributeAudio": ("_", "duration", "voice", "title", "performer"),
    "DocumentAttributeSticker": ("_", "alt"),
    "DocumentAttributeAnimated": ("_",),
}


def _attribute(value: object) -> object:
    return _constructors(value, ATTRIBUTE_CONSTRUCTORS)


DOCUMENT = {**_fields("_", "id", "date", "mime_type", "size"), "attributes": [_attribute]}
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
    ),
    "question": _text,
    "answers": [{**_fields("_", "date"), "text": _text, "option": _option}],
}
POLL_RESULTS = {**_fields("_", "total_voters"), "results": [{**_fields("_", "voters"), "option": _option}]}
MEDIA = {
    **_fields("_", "spoiler", "ttl_seconds", "voice", "video", "round", "video_timestamp"),
    "photo": PHOTO,
    "document": DOCUMENT,
    "poll": POLL,
    "results": POLL_RESULTS,
    "webpage": ("_", "url", "display_url", "type", "site_name", "title", "description", "author", "duration"),
}
FORWARD = {**_fields("_", "date", "from_name", "channel_post", "post_author", "imported", "psa_type"), "from_id": _peer}
MESSAGE_METADATA = {
    **_fields("views", "forwards", "pinned", "post", "post_author", "via_bot_id"),
    "replies": ("_", "replies", "comments", "channel_id", "max_id"),
    "fwd_from": FORWARD,
    "reply_to": {
        **_fields("_", "quote", "quote_text", "quote_offset", "forum_topic", "reply_to_top_id"),
        "quote_entities": [ENTITY],
        "reply_from": FORWARD,
    },
    "media": MEDIA,
}
ACTION_CONSTRUCTORS: dict[str, Projection] = {
    "MessageActionPinMessage": ("_",),
    "MessageActionInviteToGroupCall": {"_": _scalar, "users": [_scalar], "call": ("_", "id")},
    "MessageActionTopicCreate": ("_", "title", "icon_color", "icon_emoji_id"),
    "MessageActionTopicEdit": ("_", "title", "icon_emoji_id", "closed", "hidden"),
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
    "entities": [ENTITY],
    "service_action": _action,
    "topic": ("id", "topic_id", "title"),
    "related_users": [IDENTITY],
    "metadata": MESSAGE_METADATA,
    "reactions": {"aggregate": {"results": [{**_fields("_", "count"), "reaction": _reaction}]}},
    "reactors": [{**_identity_fields("actor_"), "date": _scalar, "reaction": _reaction}],
}
HEADER_METADATA = {"order": _scalar, "peers": [GROUP], "exporter": ("name", "version", "repository_url")}


def public_facts(value: object, projection: Projection) -> object:
    """Publish only fields approved for this exact position in the export."""
    return _project(value, projection)


def _write_public_export(path: Path, stream: TextIO) -> dict[str, int]:
    counts = {"messages": 0, "admin_events": 0, "reactors": 0}
    declared = None
    stream.write('{"format_version":1')
    for kind, record in base_records(path):
        if kind in {"group", "metadata"}:
            if kind == "metadata":
                record = {"exporter": project_exporter(version("mcp-telegram")), **record}
            stream.write(f',"{kind}":')
            json.dump(
                public_facts(record, GROUP if kind == "group" else HEADER_METADATA),
                stream,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            if kind == "metadata":
                stream.write(',"admin_events":[],"messages":[')
        elif kind == "admin_events.item":
            counts["admin_events"] += 1
        elif kind == "messages.item":
            if counts["messages"]:
                stream.write(",")
            json.dump(public_facts(record, MESSAGE), stream, ensure_ascii=False, separators=(",", ":"))
            counts["messages"] += 1
            counts["reactors"] += len(cast(list[object], record["reactors"]))
        elif kind == "export":
            declared = record
    if declared != counts:
        raise ValueError("Export counts do not match its records")
    counts["admin_events"] = 0
    stream.write('],"export":')
    json.dump(counts, stream, separators=(",", ":"))
    stream.write("}\n")
    return counts


def sanitize_export(path: Path, output: Path) -> dict[str, int]:
    """Stream to a new destination; never modify input or overwrite existing files."""
    if path.resolve() == output.resolve():
        raise ValueError("Output must differ from the original export")
    if fingerprint(output) is not None:
        raise FileExistsError("Output already exists")
    before = fingerprint(path)
    descriptor, name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            counts = _write_public_export(path, stream)
            stream.flush()
            os.fsync(stream.fileno())
        if fingerprint(path) != before:
            raise ValueError("Input changed during filtering")
        os.link(temporary, output)
        directory = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return counts
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(json.dumps(sanitize_export(args.export, args.output)))
