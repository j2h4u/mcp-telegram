"""Content-free diagnostics for native rich-hydration message lookups."""

from __future__ import annotations

from typing import Literal, TypedDict, cast

from telethon.tl import functions, types
from telethon.utils import get_peer_id

_MAX_ITEMS = 100


RichLookupCounts = TypedDict(  # noqa: UP013 - classification keys are consumed dynamically
    "RichLookupCounts",
    {
        "requested": int,
        "matched": int,
        "explicit_empty": int,
        "wrong_peer": int,
        "absent": int,
        "unexpected": int,
    },
)


def _message_ids(request: object) -> tuple[set[int], int]:
    ids = cast(list[object], getattr(request, "id", []))
    result: set[int] = set()
    unexpected = 0
    for value in ids[:_MAX_ITEMS]:
        message_id = value.id if isinstance(value, types.InputMessageID) else value
        if type(message_id) is not int or message_id in result:
            unexpected += 1
        else:
            result.add(message_id)
    return result, unexpected + int(len(ids) > _MAX_ITEMS)


def _peer_id(peer: object) -> int | None:
    try:
        return cast(int, get_peer_id(peer))
    except TypeError, ValueError, AttributeError:
        return None


def _target_peer(request: object, target: int | None) -> int | None:
    if isinstance(request, functions.channels.GetMessagesRequest):
        return _peer_id(request.channel)
    return target


def _classify_item(
    item: object, target: int | None
) -> Literal["matched", "explicit_empty", "wrong_peer", "unexpected"]:
    if isinstance(item, types.MessageEmpty):
        peer = item.peer_id
        return "wrong_peer" if peer is not None and _peer_id(peer) != target else "explicit_empty"
    if isinstance(item, types.Message | types.MessageService):
        peer = _peer_id(item.peer_id)
        if peer is None or target is None:
            return "unexpected"
        return "matched" if peer == target else "wrong_peer"
    return "unexpected"


def rich_response_kind(response: object) -> Literal["native_messages", "native_not_modified", "native_invalid"]:
    if isinstance(response, types.messages.MessagesNotModified):
        return "native_not_modified"
    if isinstance(response, (types.messages.Messages, types.messages.MessagesSlice, types.messages.ChannelMessages)):
        return "native_messages" if isinstance(response.messages, list) else "native_invalid"
    return "native_invalid"


def classify_rich_lookup(request: object, response: object, target: int | None) -> RichLookupCounts:
    """Summarize one native result without retaining identifiers or content."""
    requested, unexpected = _message_ids(request)
    counts: RichLookupCounts = {
        "requested": len(requested),
        "matched": 0,
        "explicit_empty": 0,
        "wrong_peer": 0,
        "absent": 0,
        "unexpected": unexpected,
    }
    kind = rich_response_kind(response)
    if kind != "native_messages":
        counts["unexpected"] += int(kind == "native_invalid")
        return counts
    native = cast(types.messages.Messages | types.messages.MessagesSlice | types.messages.ChannelMessages, response)
    messages = cast(list[object], native.messages)
    target = _target_peer(request, target)
    seen: set[int] = set()
    for item in messages[:_MAX_ITEMS]:
        message_id = getattr(item, "id", None)
        if type(message_id) is not int or message_id not in requested or message_id in seen:
            counts["unexpected"] += 1
            continue
        seen.add(message_id)
        category = _classify_item(item, target)
        counts[category] += 1
    counts["absent"] = len(requested - seen)
    counts["unexpected"] += int(len(messages) > _MAX_ITEMS)
    return counts
