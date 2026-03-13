from __future__ import annotations

import logging
import sqlite3
import sys
import time
import typing as t
from contextlib import asynccontextmanager
from functools import cache as functools_cache
from functools import singledispatch

from mcp.types import (
    EmbeddedResource,
    ImageContent,
    TextContent,
    Tool,
)
from pydantic import BaseModel, ConfigDict, Field
from telethon.errors import RPCError
from telethon import TelegramClient, custom, functions, types  # type: ignore[import-untyped]
from telethon.tl.functions.messages import (
    GetCommonChatsRequest,
    GetMessageReactionsListRequest,
    GetPeerDialogsRequest,
)
from telethon.tl.types import Channel, Chat
from telethon.utils import get_peer_id
from xdg_base_dirs import xdg_state_home

from . import capabilities
from .cache import (
    EntityCache,
    GROUP_TTL,
    USER_TTL,
    ReactionMetadataCache,
    TopicMetadataCache,
)
from .formatter import format_messages
from .pagination import decode_cursor, encode_cursor
from .resolver import Candidates, NotFound, Resolved, resolve
from .telegram import create_client

# Fetch reactor names only when total reactions per message are at or below this limit.
# Covers personal chats (always ≤ a few) while skipping expensive lookups on busy groups.
REACTION_NAMES_THRESHOLD = 15
FORUM_TOPICS_PAGE_SIZE = 100
TOPIC_METADATA_TTL_SECONDS = 600
GENERAL_TOPIC_ID = 1
GENERAL_TOPIC_TITLE = "General"

logger = logging.getLogger(__name__)


def _build_get_forum_topics_request(
    *,
    entity: t.Any,
    offset_date: object | None,
    offset_id: int,
    offset_topic: int,
    limit: int,
) -> object:
    """Build a forum-topics page request across Telethon API variants."""
    request_cls = getattr(functions.messages, "GetForumTopicsRequest", None)
    if request_cls is not None:
        return request_cls(
            peer=entity,
            offset_date=offset_date,
            offset_id=offset_id,
            offset_topic=offset_topic,
            limit=limit,
        )

    return functions.channels.GetForumTopicsRequest(
        channel=entity,
        offset_date=offset_date,
        offset_id=offset_id,
        offset_topic=offset_topic,
        limit=limit,
    )


def _build_get_forum_topics_by_id_request(*, entity: t.Any, topic_ids: list[int]) -> object:
    """Build a by-ID forum-topics request across Telethon API variants."""
    request_cls = getattr(functions.messages, "GetForumTopicsByIDRequest", None)
    if request_cls is not None:
        return request_cls(peer=entity, topics=topic_ids)

    return functions.channels.GetForumTopicsByIDRequest(
        channel=entity,
        topics=topic_ids,
    )


@asynccontextmanager
async def connected_client():
    """Wraps create_client() with connect/disconnect and timing logs.

    Defined here (not in telegram.py) so tests can patch create_client in this module.
    """
    client = create_client()
    already_connected = client.is_connected()
    t0 = time.monotonic()
    await client.connect()
    connect_ms = (time.monotonic() - t0) * 1000
    logger.info("tg_connect: %.1fms (reused=%s)", connect_ms, already_connected)
    try:
        yield client
    finally:
        t0 = time.monotonic()
        await client.disconnect()
        logger.info("tg_disconnect: %.1fms", (time.monotonic() - t0) * 1000)


# How to add a new tool:
#
# 1. Create a new class that inherits from ToolArgs
#    ```python
#    class NewTool(ToolArgs):
#        """Description of the new tool."""
#        pass
#    ```
#    Attributes of the class will be used as arguments for the tool.
#    The class docstring will be used as the tool description.
#
# 2. Implement the tool_runner function for the new class
#    ```python
#    @tool_runner.register
#    async def new_tool(args: NewTool) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
#        pass
#    ```
#    The function should return a sequence of TextContent, ImageContent or EmbeddedResource.
#    The function should be async and accept a single argument of the new class.
#
# 3. Done! Restart the client and the new tool should be available.


class ToolArgs(BaseModel):
    model_config = ConfigDict()


@singledispatch
async def tool_runner(
    args,  # noqa: ANN001
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    raise NotImplementedError(f"Unsupported type: {type(args)}")


def tool_description(args: type[ToolArgs]) -> Tool:
    schema = _sanitize_tool_schema(args.model_json_schema())
    return Tool(
        name=args.__name__,
        description=args.__doc__,
        inputSchema=schema,
    )


def _sanitize_tool_schema(value: t.Any) -> t.Any:
    """Return MCP-friendly JSON schema without explicit null unions."""
    if isinstance(value, dict):
        sanitized = {key: _sanitize_tool_schema(item) for key, item in value.items()}

        any_of = sanitized.get("anyOf")
        if isinstance(any_of, list):
            non_null_variants = [
                item for item in any_of if not (isinstance(item, dict) and item.get("type") == "null")
            ]
            has_null_variant = len(non_null_variants) != len(any_of)
            if has_null_variant and len(non_null_variants) == 1:
                replacement = non_null_variants[0]
                if not isinstance(replacement, dict):
                    return replacement

                merged = {
                    key: item
                    for key, item in sanitized.items()
                    if key not in {"anyOf", "default"}
                }
                return {**replacement, **merged}

        schema_type = sanitized.get("type")
        if sanitized.get("default") is None and schema_type != "null":
            sanitized.pop("default", None)

        return sanitized

    if isinstance(value, list):
        return [_sanitize_tool_schema(item) for item in value]

    return value


def tool_args(tool: Tool, *args, **kwargs) -> ToolArgs:  # noqa: ANN002, ANN003
    return sys.modules[__name__].__dict__[tool.name](*args, **kwargs)


@functools_cache
def get_entity_cache() -> EntityCache:
    """Return the shared EntityCache instance (opened once per process)."""
    db_dir = xdg_state_home() / "mcp-telegram"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "entity_cache.db"
    return EntityCache(db_path)


def _get_analytics_collector():
    """Lazy-load analytics collector (same pattern as get_entity_cache).

    Returns:
        TelemetryCollector singleton for telemetry recording
    """
    from .analytics import TelemetryCollector
    db_dir = xdg_state_home() / "mcp-telegram"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "analytics.db"
    return TelemetryCollector.get_instance(db_path)


def _get_sender_type(sender: t.Any) -> str:
    """Determine sender type from Telethon entity instance."""
    if isinstance(sender, Channel):
        return "channel"
    elif isinstance(sender, Chat):
        return "group"
    else:
        return "user"


def _cache_dialog_entry(cache: EntityCache, dialog: object) -> None:
    """Persist one Telethon dialog in the local entity cache."""
    dialog_id = getattr(dialog, "id", None)
    dialog_name = getattr(dialog, "name", None)
    if not isinstance(dialog_id, int) or not isinstance(dialog_name, str):
        return

    if getattr(dialog, "is_user", False):
        dialog_type = "user"
    elif getattr(dialog, "is_group", False):
        dialog_type = "group"
    elif getattr(dialog, "is_channel", False):
        dialog_type = "channel"
    else:
        dialog_type = "unknown"

    entity = getattr(dialog, "entity", None)
    username = getattr(entity, "username", None) if entity is not None else None
    cache.upsert(dialog_id, dialog_type, dialog_name, username)


async def _resolve_dialog(cache: EntityCache, query: str) -> Resolved | Candidates | NotFound:
    """Resolve one dialog, retrying once after a live cache warmup."""
    result = resolve(query, cache.all_names_with_ttl(USER_TTL, GROUP_TTL), cache)
    if not isinstance(result, NotFound):
        return result

    try:
        async with connected_client() as client:
            live_choices: dict[int, str] = {}
            async for dialog in client.iter_dialogs(archived=None, ignore_pinned=False):
                dialog_id = getattr(dialog, "id", None)
                dialog_name = getattr(dialog, "name", None)
                if isinstance(dialog_id, int) and isinstance(dialog_name, str):
                    live_choices[dialog_id] = dialog_name
                try:
                    _cache_dialog_entry(cache, dialog)
                except sqlite3.OperationalError as cache_exc:
                    logger.warning(
                        "dialog_cache_refresh_failed query=%r dialog_id=%r error=%s",
                        query,
                        dialog_id,
                        cache_exc,
                    )
    except Exception as exc:
        logger.warning("dialog_resolve_warmup_failed query=%r error=%s", query, exc)
        return result

    refreshed_result = resolve(query, cache.all_names_with_ttl(USER_TTL, GROUP_TTL), cache)
    if not isinstance(refreshed_result, NotFound):
        return refreshed_result

    return resolve(query, live_choices, cache)


def _normalize_topic_metadata(
    topic: object,
    *,
    existing_topic: dict[str, int | str | bool | None] | None = None,
) -> dict[str, int | str | bool | None]:
    """Normalize raw Telethon forum-topic objects into cache metadata rows."""
    topic_id = int(getattr(topic, "id"))
    raw_title = getattr(topic, "title", None)
    if raw_title is None and existing_topic is not None:
        title = str(existing_topic["title"])
    elif raw_title is None:
        title = f"Topic {topic_id}"
    else:
        title = str(raw_title)

    raw_top_message_id = getattr(topic, "top_message", None)
    if raw_top_message_id is None and existing_topic is not None:
        top_message_id = existing_topic["top_message_id"]
    else:
        top_message_id = raw_top_message_id

    is_general = topic_id == GENERAL_TOPIC_ID or title.casefold() == GENERAL_TOPIC_TITLE.casefold()
    is_deleted = raw_title is None
    normalized_topic = {
        "topic_id": topic_id,
        "title": GENERAL_TOPIC_TITLE if is_general else title,
        "top_message_id": top_message_id,
        "is_general": is_general,
        "is_deleted": is_deleted,
    }
    if existing_topic is not None and existing_topic.get("inaccessible_error") is not None:
        normalized_topic["inaccessible_error"] = existing_topic["inaccessible_error"]
    if existing_topic is not None and existing_topic.get("inaccessible_at") is not None:
        normalized_topic["inaccessible_at"] = existing_topic["inaccessible_at"]
    return normalized_topic


def _with_general_topic(
    topics: list[dict[str, int | str | bool | None]],
) -> list[dict[str, int | str | bool | None]]:
    """Ensure the General topic is represented explicitly in topic metadata."""
    if any(bool(topic["is_general"]) for topic in topics):
        return sorted(topics, key=lambda topic: int(topic["topic_id"]))

    return [
        {
            "topic_id": GENERAL_TOPIC_ID,
            "title": GENERAL_TOPIC_TITLE,
            "top_message_id": None,
            "is_general": True,
            "is_deleted": False,
        },
        *sorted(topics, key=lambda topic: int(topic["topic_id"])),
    ]


async def _fetch_forum_topics_page(
    client: t.Any,
    *,
    entity: t.Any,
    offset_date: object | None = None,
    offset_id: int = 0,
    offset_topic: int = 0,
    limit: int = FORUM_TOPICS_PAGE_SIZE,
) -> tuple[list[object], int]:
    """Fetch one raw page of forum topics using Telegram's channels.getForumTopics RPC."""
    response = await client(
        _build_get_forum_topics_request(
            entity=entity,
            offset_date=offset_date,
            offset_id=offset_id,
            offset_topic=offset_topic,
            limit=limit,
        )
    )
    topics = list(getattr(response, "topics", []))
    total_count = int(getattr(response, "count", len(topics)))
    return topics, total_count


async def _fetch_all_forum_topics(
    client: t.Any,
    *,
    entity: t.Any,
    page_size: int = FORUM_TOPICS_PAGE_SIZE,
) -> list[dict[str, int | str | bool | None]]:
    """Fetch and normalize all forum topics for one dialog via raw paginated RPC calls."""
    offset_date: object | None = None
    offset_id = 0
    offset_topic = 0
    topics_by_id: dict[int, dict[str, int | str | bool | None]] = {}
    seen_server_topic_ids: set[int] = set()
    total_count: int | None = None

    while True:
        page_topics, page_count = await _fetch_forum_topics_page(
            client,
            entity=entity,
            offset_date=offset_date,
            offset_id=offset_id,
            offset_topic=offset_topic,
            limit=page_size,
        )
        if total_count is None:
            total_count = page_count
        if not page_topics:
            break

        for topic in page_topics:
            normalized_topic = _normalize_topic_metadata(topic)
            topic_id = int(normalized_topic["topic_id"])
            topics_by_id[topic_id] = normalized_topic
            seen_server_topic_ids.add(topic_id)

        last_topic = page_topics[-1]
        next_offset_topic = int(getattr(last_topic, "id"))
        next_offset_id = getattr(last_topic, "top_message", 0) or 0
        next_offset_date = getattr(last_topic, "date", None)
        if (
            next_offset_topic == offset_topic
            and next_offset_id == offset_id
            and next_offset_date == offset_date
        ):
            break

        offset_topic = next_offset_topic
        offset_id = next_offset_id
        offset_date = next_offset_date

        if total_count and len(seen_server_topic_ids) >= total_count:
            break
        if len(page_topics) < page_size:
            break

    return _with_general_topic(list(topics_by_id.values()))


async def _refresh_topic_by_id(
    client: t.Any,
    *,
    entity: t.Any,
    dialog_id: int,
    topic_id: int,
    topic_cache: TopicMetadataCache,
    ttl_seconds: int = TOPIC_METADATA_TTL_SECONDS,
) -> dict[str, int | str | bool | None] | None:
    """Refresh one topic by ID and persist tombstones when Telegram reports deletion."""
    existing_topic = topic_cache.get_topic(dialog_id, topic_id, ttl_seconds)
    response = await client(
        _build_get_forum_topics_by_id_request(entity=entity, topic_ids=[topic_id])
    )
    response_topics = list(getattr(response, "topics", []))
    if not response_topics:
        return None

    topic = _normalize_topic_metadata(response_topics[0], existing_topic=existing_topic)
    topic_cache.upsert_topics(dialog_id, [topic])
    return topic


async def _load_dialog_topics(
    client: t.Any,
    *,
    entity: t.Any,
    dialog_id: int,
    topic_cache: TopicMetadataCache,
    ttl_seconds: int = TOPIC_METADATA_TTL_SECONDS,
) -> dict[str, t.Any]:
    """Load one dialog's topic catalog from cache or Telegram and keep tombstones available."""
    cached_topics = topic_cache.get_dialog_topics(
        dialog_id,
        ttl_seconds,
        include_deleted=True,
    )
    if cached_topics is None:
        cached_topics = await _fetch_all_forum_topics(client, entity=entity)
        if cached_topics:
            topic_cache.upsert_topics(dialog_id, cached_topics)
    else:
        normalized_topics = _with_general_topic(cached_topics)
        if len(normalized_topics) != len(cached_topics):
            topic_cache.upsert_topics(dialog_id, normalized_topics)
        cached_topics = normalized_topics

    metadata_by_id = {int(topic["topic_id"]): topic for topic in cached_topics}
    choices = {
        topic_id: str(topic["title"])
        for topic_id, topic in metadata_by_id.items()
        if not bool(topic["is_deleted"])
    }
    deleted_topics = {
        topic_id: topic
        for topic_id, topic in metadata_by_id.items()
        if bool(topic["is_deleted"])
    }
    return {
        "choices": choices,
        "metadata_by_id": metadata_by_id,
        "deleted_topics": deleted_topics,
    }


def _resolve_deleted_topic(
    requested_topic: str,
    deleted_topics: dict[int, dict[str, int | str | bool | None]],
) -> t.Any | None:
    """Resolve one deleted topic by its preserved tombstone title, if any."""
    deleted_choices = {
        topic_id: str(topic["title"])
        for topic_id, topic in deleted_topics.items()
    }
    if not deleted_choices:
        return None

    deleted_result = resolve(requested_topic, deleted_choices)
    if isinstance(deleted_result, NotFound):
        return None
    return deleted_result


def _action_text(summary: str, action: str) -> str:
    """Return a short action-oriented response body."""
    return f"{summary}\nAction: {action}"


def _dialog_not_found_text(dialog_name: str, *, retry_tool: str) -> str:
    """Return an action-oriented response for missing dialogs."""
    return _action_text(
        f'Dialog "{dialog_name}" was not found.',
        f"Call ListDialogs, then retry {retry_tool} with dialog set to an exact dialog id, @username, or full dialog name.",
    )


def _ambiguous_dialog_text(dialog_name: str, match_lines: list[str], *, retry_tool: str) -> str:
    """Return an action-oriented response for ambiguous dialogs."""
    matches = "\n".join(match_lines)
    return (
        f'Dialog "{dialog_name}" matched multiple dialogs.\n'
        f"Action: Retry {retry_tool} with dialog set to one of the numeric ids from the matches below.\n"
        f"{matches}"
    )


def _sender_not_found_text(sender_name: str, *, retry_tool: str) -> str:
    """Return an action-oriented response for missing senders."""
    return _action_text(
        f'Sender "{sender_name}" was not found.',
        f"Retry {retry_tool} without sender, or use an exact sender name or @username that appears in this dialog.",
    )


def _ambiguous_sender_text(sender_name: str, match_lines: list[str], *, retry_tool: str) -> str:
    """Return an action-oriented response for ambiguous senders."""
    matches = "\n".join(match_lines)
    return (
        f'Sender "{sender_name}" matched multiple users.\n'
        f"Action: Retry {retry_tool} with sender set to one exact match from the list below.\n"
        f"{matches}"
    )


def _deleted_topic_text(topic_name: str, *, retry_tool: str) -> str:
    """Return the explicit user-facing message for deleted topics."""
    return _action_text(
        f'Topic "{topic_name}" was deleted and can no longer be fetched.',
        f"Call ListTopics for this dialog, then retry {retry_tool} with an active topic title, or omit topic to read across all topics.",
    )


def _rpc_error_detail(exc: RPCError) -> str:
    """Return the stable Telegram RPC detail for one exception."""
    detail = getattr(exc, "message", None) or str(exc)
    return str(detail)


def _inaccessible_topic_text(topic_name: str, exc: RPCError, *, resolved: bool, retry_tool: str) -> str:
    """Return a readable user-facing message for inaccessible topics."""
    detail = _rpc_error_detail(exc)
    if resolved:
        return _action_text(
            f'Topic "{topic_name}" resolved, but Telegram rejected thread fetch ({detail}).',
            f"Retry {retry_tool} without topic to read dialog-wide messages, or call ListTopics and choose another active topic.",
        )

    return _action_text(
        f'Topic "{topic_name}" could not be loaded because Telegram rejected topic access ({detail}).',
        f"Call ListTopics for this dialog, then retry {retry_tool} with an exact active topic title, or omit topic.",
    )


def _topic_not_found_text(topic_name: str, *, retry_tool: str) -> str:
    """Return an action-oriented response for missing topics."""
    return _action_text(
        f'Topic "{topic_name}" was not found.',
        f"Call ListTopics for this dialog, then retry {retry_tool} with an exact topic title.",
    )


def _ambiguous_topic_text(topic_name: str, match_lines: list[str], *, retry_tool: str) -> str:
    """Return an action-oriented response for ambiguous topics."""
    matches = "\n".join(match_lines)
    return (
        f'Topic "{topic_name}" matched multiple topics.\n'
        f"Action: Retry {retry_tool} with topic set to one exact topic title from the matches below.\n"
        f"{matches}"
    )


def _ambiguous_deleted_topic_text(topic_name: str, match_lines: list[str], *, retry_tool: str) -> str:
    """Return an action-oriented response for ambiguous deleted topics."""
    matches = "\n".join(match_lines)
    return (
        f'Deleted topic query "{topic_name}" matched multiple deleted topics.\n'
        f"Action: Call ListTopics for this dialog, then retry {retry_tool} with an active topic title instead of a deleted one.\n"
        f"{matches}"
    )


def _invalid_cursor_text(detail: str, *, retry_tool: str) -> str:
    """Return an action-oriented response for malformed cursors."""
    return _action_text(
        f"Cursor is invalid: {detail}",
        f"Retry {retry_tool} without cursor to start from the first page, or reuse the exact next_cursor value from the previous {retry_tool} response.",
    )


def _dialog_topics_unavailable_text(dialog_name: str, exc: RPCError) -> str:
    """Return a readable message when one dialog cannot expose a topic catalog."""
    detail = _rpc_error_detail(exc)
    return _action_text(
        f'Dialog "{dialog_name}" does not expose a readable forum-topic catalog ({detail}).',
        "Do not use ListTopics for this dialog. Retry ListMessages without topic if you want dialog messages, or choose another forum-enabled dialog.",
    )


def _no_active_topics_text(dialog_name: str) -> str:
    """Return an action-oriented response for dialogs without active topics."""
    return _action_text(
        f'No active forum topics found for "{dialog_name}".',
        "Retry ListMessages without topic to read dialog-wide messages, or choose another forum-enabled dialog.",
    )


def _user_not_found_text(user_name: str, *, retry_tool: str) -> str:
    """Return an action-oriented response for missing users."""
    return _action_text(
        f'User "{user_name}" was not found.',
        f"Call ListDialogs, then retry {retry_tool} with an exact user name or @username.",
    )


def _ambiguous_user_text(user_name: str, match_lines: list[str], *, retry_tool: str) -> str:
    """Return an action-oriented response for ambiguous users."""
    matches = "\n".join(match_lines)
    return (
        f'User "{user_name}" matched multiple users.\n'
        f"Action: Retry {retry_tool} with one exact user match from the list below.\n"
        f"{matches}"
    )


def _fetch_user_info_error_text(user_name: str, detail: str) -> str:
    """Return an action-oriented response for user-info fetch failures."""
    return _action_text(
        f'Could not fetch info for user "{user_name}" ({detail}).',
        "Retry GetUserInfo later. If this persists, verify that the Telegram session still has access to this user and shared chats.",
    )


def _not_authenticated_text(retry_tool: str) -> str:
    """Return an action-oriented response for missing Telegram auth."""
    return _action_text(
        "Telegram session is not authenticated.",
        f"Authenticate the Telegram session, then retry {retry_tool}.",
    )


def _no_usage_data_text() -> str:
    """Return an action-oriented response when telemetry exists but has no recent rows."""
    return _action_text(
        "No usage data in the past 30 days.",
        "Use any Telegram tools to generate telemetry, then retry GetUsageStats.",
    )


def _usage_stats_db_missing_text() -> str:
    """Return an action-oriented response when telemetry DB is missing."""
    return _action_text(
        "Analytics database not yet created.",
        "Use other tools first to generate telemetry, then retry GetUsageStats.",
    )


def _usage_stats_query_error_text(error_type: str) -> str:
    """Return an action-oriented response for usage-stats query failures."""
    return _action_text(
        f"Could not query usage stats ({error_type}).",
        "Retry GetUsageStats later. If the error persists, inspect analytics.db initialization and schema.",
    )


def _no_dialogs_text() -> str:
    """Return an action-oriented response when no dialogs are visible."""
    return _action_text(
        "No dialogs were returned.",
        "Retry ListDialogs with exclude_archived=False and ignore_pinned=False, or verify that the Telegram session is authenticated and has visible dialogs.",
    )


def _search_no_hits_text(dialog_name: str, query: str) -> str:
    """Return an action-oriented response when search finds no hits."""
    return _action_text(
        f'No messages matched query "{query}" in dialog "{dialog_name}".',
        "Retry SearchMessages with a broader query, a smaller offset, or a different dialog.",
    )


def _topic_status(topic: dict[str, int | str | bool | None]) -> str:
    """Return one short topic status label for listings."""
    if bool(topic["is_deleted"]):
        return "deleted"
    if bool(topic["is_general"]):
        return "general"
    if topic.get("inaccessible_error"):
        return "previously_inaccessible"
    return "active"


def _topic_row_text(topic: dict[str, int | str | bool | None]) -> str:
    """Return one stable topic row for ListTopics output."""
    line = (
        f'topic_id={topic["topic_id"]} '
        f'title="{topic["title"]}" '
        f'top_message_id={topic["top_message_id"]} '
        f'status={_topic_status(topic)}'
    )
    if topic.get("inaccessible_error"):
        line += f' last_error={topic["inaccessible_error"]}'
    return line


def _is_topic_id_invalid_error(exc: RPCError) -> bool:
    """Return True when Telegram reports the cached thread anchor is invalid."""
    return "TOPIC_ID_INVALID" in _rpc_error_detail(exc).upper()


def _forum_topic_anchor_id(msg: object) -> int | None:
    """Return the topic anchor message id for one forum message, if present."""
    reply_to = getattr(msg, "reply_to", None)
    if reply_to is None:
        return None

    top_id = getattr(reply_to, "reply_to_top_id", None)
    if isinstance(top_id, int):
        return top_id

    if bool(getattr(reply_to, "forum_topic", False)):
        reply_id = getattr(reply_to, "reply_to_msg_id", None)
        if isinstance(reply_id, int):
            return reply_id

    return None


def _messages_need_forum_topic_labels(messages: list[object]) -> bool:
    """Return True when a mixed message page appears to come from a forum dialog."""
    return any(_forum_topic_anchor_id(msg) is not None for msg in messages)


def _build_topic_name_getter(
    topic_catalog: dict[str, t.Any],
) -> t.Callable[[object], str | None]:
    """Build a formatter callback that labels cross-topic forum messages."""
    topic_name_by_anchor: dict[int, str] = {}
    for topic in topic_catalog["metadata_by_id"].values():
        if bool(topic["is_deleted"]):
            continue

        topic_name_by_anchor[int(topic["topic_id"])] = str(topic["title"])
        if topic["top_message_id"] is not None:
            topic_name_by_anchor[int(topic["top_message_id"])] = str(topic["title"])

    def _topic_name_for_message(msg: object) -> str | None:
        anchor_id = _forum_topic_anchor_id(msg)
        if anchor_id is None:
            return GENERAL_TOPIC_TITLE
        return topic_name_by_anchor.get(anchor_id)

    return _topic_name_for_message


def _topic_empty_state_text(*, unread: bool) -> str:
    """Return the empty-state body for one ListMessages response."""
    if unread:
        return "no unread messages"
    return ""


def _append_topic_match_metadata(
    match: dict[str, t.Any],
    metadata_by_id: dict[int, dict[str, int | str | bool | None]],
) -> str:
    """Return one topic match line enriched with cached metadata."""
    line = f'id={match["entity_id"]} name="{match["display_name"]}" score={match["score"]}'
    topic = metadata_by_id.get(int(match["entity_id"]))
    if topic is not None:
        line += f' status={_topic_status(topic)}'
        if topic["top_message_id"] is not None:
            line += f' top_message_id={topic["top_message_id"]}'
        if topic.get("inaccessible_error"):
            line += f' last_error={topic["inaccessible_error"]}'
    return line


def _message_matches_topic(
    message: object,
    *,
    topic_id: int,
    top_message_id: int | None,
    is_general: bool,
    allow_headerless_messages: bool,
) -> bool:
    """Return True when one message belongs to the requested forum topic."""
    anchor_ids = {topic_id}
    if top_message_id is not None:
        anchor_ids.add(top_message_id)

    message_id = getattr(message, "id", None)
    if isinstance(message_id, int) and message_id in anchor_ids:
        return True

    reply_to = getattr(message, "reply_to", None)
    if reply_to is None:
        if is_general:
            return True
        return allow_headerless_messages

    reply_to_top_id = getattr(reply_to, "reply_to_top_id", None)
    if isinstance(reply_to_top_id, int) and reply_to_top_id in anchor_ids:
        return True

    reply_to_msg_id = getattr(reply_to, "reply_to_msg_id", None)
    return isinstance(reply_to_msg_id, int) and reply_to_msg_id in anchor_ids


async def _fetch_topic_messages(
    client: t.Any,
    *,
    iter_kwargs: dict[str, t.Any],
    topic_metadata: dict[str, int | str | bool | None],
    allow_headerless_messages: bool,
) -> list[object]:
    """Fetch a topic page and strip any leaked adjacent-topic messages."""
    requested_limit = int(iter_kwargs.get("limit", 0) or 0)
    if requested_limit <= 0:
        return []

    topic_id = int(topic_metadata["topic_id"])
    raw_top_message_id = topic_metadata["top_message_id"]
    top_message_id = int(raw_top_message_id) if raw_top_message_id is not None else None
    is_general = bool(topic_metadata["is_general"])

    batch_kwargs = dict(iter_kwargs)
    batch_limit = requested_limit
    boundary_key = "min_id" if bool(batch_kwargs.get("reverse")) else "max_id"
    topic_messages: list[object] = []

    while len(topic_messages) < requested_limit:
        raw_messages = [msg async for msg in client.iter_messages(**batch_kwargs)]
        if not raw_messages:
            break

        for msg in raw_messages:
            if _message_matches_topic(
                msg,
                topic_id=topic_id,
                top_message_id=top_message_id,
                is_general=is_general,
                allow_headerless_messages=allow_headerless_messages,
            ):
                topic_messages.append(msg)
                if len(topic_messages) == requested_limit:
                    break

        if len(topic_messages) == requested_limit or len(raw_messages) < batch_limit:
            break

        last_message_id = getattr(raw_messages[-1], "id", None)
        previous_boundary = batch_kwargs.get(boundary_key)
        if not isinstance(last_message_id, int) or previous_boundary == last_message_id:
            break
        batch_kwargs[boundary_key] = last_message_id

    return topic_messages


async def _fetch_messages_for_topic(
    client: t.Any,
    *,
    entity_id: int,
    iter_kwargs: dict[str, t.Any],
    topic_metadata: dict[str, int | str | bool | None],
    topic_cache: TopicMetadataCache,
    allow_headerless_messages: bool,
) -> tuple[list[object] | None, dict[str, int | str | bool | None], dict[str, t.Any]]:
    """Fetch one topic page with one bounded by-ID refresh and retry on stale anchors."""
    active_iter_kwargs = dict(iter_kwargs)
    active_topic_metadata = topic_metadata

    async def _scan_dialog_history_for_topic() -> tuple[list[object], dict[str, t.Any]]:
        """Fallback to dialog-wide history scanning when thread fetch rejects a valid topic anchor."""
        history_iter_kwargs = dict(active_iter_kwargs)
        history_iter_kwargs.pop("reply_to", None)
        messages = await _fetch_topic_messages(
            client,
            iter_kwargs=history_iter_kwargs,
            topic_metadata=active_topic_metadata,
            allow_headerless_messages=False,
        )
        return messages, history_iter_kwargs

    try:
        messages = await _fetch_topic_messages(
            client,
            iter_kwargs=active_iter_kwargs,
            topic_metadata=active_topic_metadata,
            allow_headerless_messages=allow_headerless_messages,
        )
        return messages, active_topic_metadata, active_iter_kwargs
    except RPCError as exc:
        if not _is_topic_id_invalid_error(exc):
            raise

        refreshed_topic = await _refresh_topic_by_id(
            client,
            entity=entity_id,
            dialog_id=entity_id,
            topic_id=int(active_topic_metadata["topic_id"]),
            topic_cache=topic_cache,
        )
        if refreshed_topic is None:
            raise exc

        active_topic_metadata = refreshed_topic
        if bool(active_topic_metadata["is_deleted"]):
            return None, active_topic_metadata, active_iter_kwargs

        refreshed_top_message_id = active_topic_metadata["top_message_id"]
        if refreshed_top_message_id is None:
            raise exc

        refreshed_reply_to = int(refreshed_top_message_id)
        if active_iter_kwargs.get("reply_to") == refreshed_reply_to:
            dialog_messages, dialog_iter_kwargs = await _scan_dialog_history_for_topic()
            if dialog_messages:
                return dialog_messages, active_topic_metadata, dialog_iter_kwargs
            raise exc

        active_iter_kwargs["reply_to"] = refreshed_reply_to
        try:
            messages = await _fetch_topic_messages(
                client,
                iter_kwargs=active_iter_kwargs,
                topic_metadata=active_topic_metadata,
                allow_headerless_messages=allow_headerless_messages,
            )
            return messages, active_topic_metadata, active_iter_kwargs
        except RPCError as retry_exc:
            if not _is_topic_id_invalid_error(retry_exc):
                raise

            dialog_messages, dialog_iter_kwargs = await _scan_dialog_history_for_topic()
            if dialog_messages:
                return dialog_messages, active_topic_metadata, dialog_iter_kwargs
            raise retry_exc


_resolve_dialog_target = capabilities.resolve_dialog_target
_load_forum_topic_capability = capabilities.load_forum_topic_capability
_build_get_forum_topics_request = capabilities.build_get_forum_topics_request
_build_get_forum_topics_by_id_request = capabilities.build_get_forum_topics_by_id_request
_normalize_topic_metadata = capabilities.normalize_topic_metadata
_with_general_topic = capabilities.with_general_topic
_fetch_forum_topics_page = capabilities.fetch_forum_topics_page
_fetch_all_forum_topics = capabilities.fetch_all_forum_topics
_refresh_topic_by_id = capabilities.refresh_topic_by_id
_load_dialog_topics = capabilities.load_dialog_topics
_resolve_deleted_topic = capabilities.resolve_deleted_topic
_dialog_not_found_text = capabilities.dialog_not_found_text
_ambiguous_dialog_text = capabilities.ambiguous_dialog_text
_deleted_topic_text = capabilities.deleted_topic_text
_rpc_error_detail = capabilities.rpc_error_detail
_inaccessible_topic_text = capabilities.inaccessible_topic_text
_topic_not_found_text = capabilities.topic_not_found_text
_ambiguous_topic_text = capabilities.ambiguous_topic_text
_ambiguous_deleted_topic_text = capabilities.ambiguous_deleted_topic_text
_dialog_topics_unavailable_text = capabilities.dialog_topics_unavailable_text
_no_active_topics_text = capabilities.no_active_topics_text
_topic_status = capabilities.topic_status
_topic_row_text = capabilities.topic_row_text
_is_topic_id_invalid_error = capabilities.is_topic_id_invalid_error
_forum_topic_anchor_id = capabilities.forum_topic_anchor_id
_messages_need_forum_topic_labels = capabilities.messages_need_forum_topic_labels
_build_topic_name_getter = capabilities.build_topic_name_getter
_topic_empty_state_text = capabilities.topic_empty_state_text
_append_topic_match_metadata = capabilities.append_topic_match_metadata
_message_matches_topic = capabilities.message_matches_topic
_fetch_topic_messages = capabilities.fetch_topic_messages
_fetch_messages_for_topic = capabilities.fetch_messages_for_topic
_execute_list_topics_capability = capabilities.execute_list_topics_capability
_execute_history_read_capability = capabilities.execute_history_read_capability


### ListDialogs ###


class ListDialogs(ToolArgs):
    """List available dialogs, chats and channels with type and last message timestamp.

    Returns both archived and non-archived dialogs by default (Telegram uses archiving as a UI
    organization tool, not data archival). Set exclude_archived=True to show only non-archived
    dialogs (equivalent to old archived=False behavior).
    """

    exclude_archived: bool = False  # Changed: renamed from archived, inverted default
    ignore_pinned: bool = False


@tool_runner.register
async def list_dialogs(
    args: ListDialogs,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    """List dialogs with telemetry recording."""
    logger.info("method[ListDialogs] args[%s]", args)
    t0 = time.monotonic()
    error_type = None
    result_count = 0

    try:
        cache = get_entity_cache()
        lines: list[str] = []
        async with connected_client() as client:
            # Map parameter to Telethon's archived parameter
            # None = mixed (current folder + archives), False = main folder, True = archive folder
            # Since we want default behavior to show all (both archived and non-archived),
            # we use None when exclude_archived=False, and False when exclude_archived=True
            telethon_archived_param = None if not args.exclude_archived else False

            async for dialog in client.iter_dialogs(
                archived=telethon_archived_param, ignore_pinned=args.ignore_pinned
            ):
                if dialog.is_user:
                    dtype = "user"
                elif dialog.is_group:
                    dtype = "group"
                elif dialog.is_channel:
                    dtype = "channel"
                else:
                    dtype = "unknown"
                last_at = dialog.date.strftime("%Y-%m-%d %H:%M") if dialog.date else "unknown"
                # Lazy cache warm-up: upsert entity metadata on every ListDialogs call
                _cache_dialog_entry(cache, dialog)
                lines.append(
                    f"name='{dialog.name}' id={dialog.id} type={dtype} "
                    f"last_message_at={last_at} unread={dialog.unread_count}"
                )
        result_count = len(lines)
        result_text = "\n".join(lines) if lines else _no_dialogs_text()
        result = [TextContent(type="text", text=result_text)]
    except Exception as exc:
        error_type = type(exc).__name__
        raise
    finally:
        duration_ms = (time.monotonic() - t0) * 1000
        try:
            from .analytics import TelemetryEvent
            collector = _get_analytics_collector()
            collector.record_event(TelemetryEvent(
                tool_name="ListDialogs",
                timestamp=time.time(),
                duration_ms=duration_ms,
                result_count=result_count,
                has_cursor=False,
                page_depth=1,
                has_filter=False,
                error_type=error_type,
            ))
        except Exception as e:
            logger.error("Failed to record telemetry for ListDialogs: %s", e)

    return result


### ListMessages ###


class ListTopics(ToolArgs):
    """
    List forum topics for one dialog.

    Use this before topic= when working with forum supergroups so you can choose an exact topic
    name or numeric topic_id instead of guessing via fuzzy match.
    """

    dialog: str


@tool_runner.register
async def list_topics(
    args: ListTopics,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    """List dialog topics with telemetry recording."""
    logger.info("method[ListTopics] args[%s]", args)
    t0 = time.monotonic()
    error_type = None
    result_count = 0

    try:
        cache = get_entity_cache()
        async with connected_client() as client:
            topic_execution = await _execute_list_topics_capability(
                client,
                cache=cache,
                dialog_query=args.dialog,
                retry_tool="ListTopics",
                resolve_dialog=_resolve_dialog,
                load_topics=_load_dialog_topics,
            )
        if isinstance(
            topic_execution,
            (capabilities.DialogTargetFailure, capabilities.ForumTopicFailure),
        ):
            return [TextContent(type="text", text=topic_execution.text)]

        result_count = len(topic_execution.active_topics)
        if not topic_execution.active_topics:
            text = topic_execution.resolve_prefix + _no_active_topics_text(
                topic_execution.dialog_name
            )
            return [TextContent(type="text", text=text)]

        lines = [_topic_row_text(topic) for topic in topic_execution.active_topics]
        result_text = topic_execution.resolve_prefix + "\n".join(lines)
        result = [TextContent(type="text", text=result_text)]
    except Exception as exc:
        error_type = type(exc).__name__
        raise
    finally:
        duration_ms = (time.monotonic() - t0) * 1000
        try:
            from .analytics import TelemetryEvent

            collector = _get_analytics_collector()
            collector.record_event(TelemetryEvent(
                tool_name="ListTopics",
                timestamp=time.time(),
                duration_ms=duration_ms,
                result_count=result_count,
                has_cursor=False,
                page_depth=1,
                has_filter=True,
                error_type=error_type,
            ))
        except Exception as e:
            logger.error("Failed to record telemetry for ListTopics: %s", e)

    return result


class ListMessages(ToolArgs):
    """
    List messages in one dialog.

    REQUIRED: dialog must be provided. This tool does not support a global
    "latest messages across all dialogs" mode.

    Returns messages newest-first in human-readable format
    (HH:mm FirstName: text) with date headers and session breaks.

    Use cursor= with the next_cursor token from a previous response to page back in time.
    Use sender= to filter messages from a specific person (name string, resolved via fuzzy match).
    Use topic= to filter messages to one forum topic after the dialog has been resolved.
    In forum dialogs, omitting topic= returns a cross-topic page and each message is labeled inline.
    Use unread=True to show only messages you haven't read yet.
    Use from_beginning=True to fetch messages oldest-first (starts from message ID 1). When true,
    pagination reads forward through time rather than backward.
    Default limit=50; set limit explicitly if you want a smaller MCP response.

    If response is ambiguous (multiple matches), use the numeric id= parameter with the ID from the matches list.
    For @username lookups, prepend @ to the name: dialog="@username".
    """

    dialog: str = Field(
        description=(
            "Required. Dialog identifier to read from: numeric id, @username, or fuzzy dialog name. "
            "No default is applied, and this tool cannot list messages across all dialogs."
        )
    )
    limit: int = 50
    cursor: str | None = None
    sender: str | None = None
    topic: str | None = None
    unread: bool = False
    from_beginning: bool = False


@tool_runner.register
async def list_messages(
    args: ListMessages,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    """List messages with telemetry recording."""
    logger.info("method[ListMessages] args[%s]", args)
    t0 = time.monotonic()
    error_type = None
    result_count = 0
    has_filter = bool(args.sender or args.topic or args.unread)
    page_depth = 1

    try:
        cache = get_entity_cache()
        async with connected_client() as client:
            history_execution = await _execute_history_read_capability(
                client,
                cache=cache,
                dialog_query=args.dialog,
                limit=args.limit,
                cursor=args.cursor,
                sender_query=args.sender,
                topic_query=args.topic,
                unread=args.unread,
                from_beginning=args.from_beginning,
                retry_tool="ListMessages",
                resolve_dialog=_resolve_dialog,
                get_sender_type=_get_sender_type,
                reaction_names_threshold=REACTION_NAMES_THRESHOLD,
                load_topics=_load_dialog_topics,
                fetch_topic_messages_fn=_fetch_topic_messages,
                refresh_topic_by_id_fn=_refresh_topic_by_id,
            )
        if isinstance(
            history_execution,
            (
                capabilities.DialogTargetFailure,
                capabilities.ForumTopicFailure,
                capabilities.MessageReadFailure,
            ),
        ):
            return [TextContent(type="text", text=history_execution.text)]

        messages = list(history_execution.messages)
        text = format_messages(
            messages,
            reply_map=history_execution.reply_map,
            reaction_names_map=history_execution.reaction_names_map,
            topic_name_getter=history_execution.topic_name_getter,
        )
        if not text:
            text = _topic_empty_state_text(unread=args.unread)

        result_count = len(messages)
        topic_prefix = (
            f"[topic: {history_execution.topic_name}]\n"
            if history_execution.topic_name
            else ""
        )
        result_text = history_execution.resolve_prefix + topic_prefix + text
        if history_execution.next_cursor:
            result_text += f"\n\nnext_cursor: {history_execution.next_cursor}"
        result = [TextContent(type="text", text=result_text)]
    except Exception as exc:
        error_type = type(exc).__name__
        raise
    finally:
        duration_ms = (time.monotonic() - t0) * 1000
        try:
            from .analytics import TelemetryEvent
            collector = _get_analytics_collector()
            collector.record_event(TelemetryEvent(
                tool_name="ListMessages",
                timestamp=time.time(),
                duration_ms=duration_ms,
                result_count=result_count,
                has_cursor=args.cursor is not None,
                page_depth=page_depth,
                has_filter=has_filter,
                error_type=error_type,
            ))
        except Exception as e:
            logger.error("Failed to record telemetry for ListMessages: %s", e)

    return result


### SearchMessages ###


class SearchMessages(ToolArgs):
    """
    Search messages in a dialog by text query. Returns matching messages newest to oldest.

    Use offset= with the next_offset value from a previous response to get the next page.

    If response is ambiguous, use the numeric ID from the matches list to disambiguate.
    For @username lookups, prepend @ to the dialog name: dialog="@channel_name".
    """

    dialog: str
    query: str
    limit: int = 20
    offset: int | None = None


@tool_runner.register
async def search_messages(
    args: SearchMessages,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    """Search messages with telemetry recording."""
    logger.info("method[SearchMessages] args[%s]", args)
    t0 = time.monotonic()
    error_type = None
    result_count = 0
    page_depth = 1

    try:
        # Step 1: Resolve dialog name
        cache = get_entity_cache()
        dialog_target = await _resolve_dialog_target(
            cache=cache,
            query=args.dialog,
            retry_tool="SearchMessages",
            resolve_dialog=_resolve_dialog,
        )
        if isinstance(dialog_target, capabilities.DialogTargetFailure):
            return [TextContent(type="text", text=dialog_target.text)]
        entity_id: int = dialog_target.entity_id
        resolve_prefix = dialog_target.resolve_prefix

        page_offset = args.offset or 0

        async with connected_client() as client:
            # Step 1: Fetch hits
            hits = [
                msg async for msg in client.iter_messages(
                    entity_id,
                    search=args.query,
                    limit=args.limit,
                    add_offset=page_offset,
                )
            ]

            # Lazy cache population: upsert sender entities from hit messages
            for msg in hits:
                sender = getattr(msg, "sender", None)
                if sender is not None:
                    sender_name = " ".join(
                        filter(None, [
                            getattr(sender, "first_name", None),
                            getattr(sender, "last_name", None),
                        ])
                    ) or getattr(sender, "title", "") or str(msg.sender_id)
                    sender_type = _get_sender_type(sender)
                    cache.upsert(
                        msg.sender_id, sender_type, sender_name,
                        getattr(sender, "username", None)
                    )

            # Step 2: Fetch context messages (±3 around each hit, excluding hit IDs)
            hit_ids = {h.id for h in hits}
            context_ids_needed: set[int] = set()
            for hit in hits:
                for offset in range(-3, 4):
                    if offset != 0:
                        context_ids_needed.add(hit.id + offset)
            context_ids_needed -= hit_ids

            context_msgs: dict[int, object] = {}
            if context_ids_needed:
                fetched = await client.get_messages(entity_id, ids=list(context_ids_needed))
                fetched_list = fetched if isinstance(fetched, list) else [fetched]
                context_msgs = {m.id: m for m in fetched_list if m is not None and isinstance(m.id, int)}

            # Step 3: Build reaction_names_map for hits
            reaction_names_map: dict[int, dict[str, list[str]]] = {}
            for msg in hits:
                rxns = getattr(msg, "reactions", None)
                if not rxns:
                    continue
                results = getattr(rxns, "results", None) or []
                total = sum(getattr(r, "count", 0) for r in results)
                if total == 0 or total > REACTION_NAMES_THRESHOLD:
                    continue
                try:
                    rl = await client(GetMessageReactionsListRequest(
                        peer=entity_id,
                        id=msg.id,
                        limit=100,
                    ))
                    user_by_id = {u.id: u for u in (getattr(rl, "users", None) or [])}
                    by_emoji: dict[str, list[str]] = {}
                    for entry in (getattr(rl, "reactions", None) or []):
                        emoji = getattr(getattr(entry, "reaction", None), "emoticon", None) or "?"
                        uid = getattr(getattr(entry, "peer_id", None), "user_id", None)
                        if uid and uid in user_by_id:
                            u = user_by_id[uid]
                            name = " ".join(filter(None, [
                                getattr(u, "first_name", None),
                                getattr(u, "last_name", None),
                            ])) or str(uid)
                            cache.upsert(u.id, "user", name, getattr(u, "username", None))
                            by_emoji.setdefault(emoji, []).append(name)
                    if by_emoji:
                        reaction_names_map[msg.id] = by_emoji
                except Exception:
                    pass  # fallback to count-only

            # Step 4 & 5: Build per-hit groups and format each
            parts: list[str] = []
            for i, hit in enumerate(hits):
                before = [
                    context_msgs[hit.id - j]
                    for j in range(3, 0, -1)
                    if (hit.id - j) in context_msgs
                ]
                after = [
                    context_msgs[hit.id + j]
                    for j in range(1, 4)
                    if (hit.id + j) in context_msgs
                ]
                group_msgs = sorted([*before, hit, *after], key=lambda m: m.id, reverse=True)
                group_text = format_messages(
                    group_msgs, reply_map={}, reaction_names_map=reaction_names_map
                )

                # Mark the hit line with [HIT] prefix using time prefix as locator
                from zoneinfo import ZoneInfo
                hit_dt = hit.date.astimezone(ZoneInfo("UTC"))
                hit_time_prefix = hit_dt.strftime("%H:%M")
                hit_lines = group_text.splitlines()
                for idx, line in enumerate(hit_lines):
                    if line.startswith(hit_time_prefix) and "[HIT]" not in line:
                        hit_lines[idx] = f"[HIT] {line}"
                        break
                group_text = "\n".join(hit_lines)

                parts.append(f"--- hit {i + 1}/{len(hits)} ---\n{group_text}")

        result_count = len(hits)
        if parts:
            result_text = resolve_prefix + "\n\n".join(parts)
        else:
            result_text = resolve_prefix + _search_no_hits_text(dialog_target.display_name, args.query)
        if len(hits) == args.limit:
            result_text += f"\n\nnext_offset: {page_offset + args.limit}"
        result = [TextContent(type="text", text=result_text)]
    except Exception as exc:
        error_type = type(exc).__name__
        raise
    finally:
        duration_ms = (time.monotonic() - t0) * 1000
        try:
            from .analytics import TelemetryEvent
            collector = _get_analytics_collector()
            collector.record_event(TelemetryEvent(
                tool_name="SearchMessages",
                timestamp=time.time(),
                duration_ms=duration_ms,
                result_count=result_count,
                has_cursor=False,
                page_depth=page_depth,
                has_filter=True,  # search is inherently filtered
                error_type=error_type,
            ))
        except Exception as e:
            logger.error("Failed to record telemetry for SearchMessages: %s", e)

    return result


### GetMe ###


class GetMyAccount(ToolArgs):
    """Return own account info: numeric id, display name, and username. No arguments required."""

    pass


@tool_runner.register
async def get_my_account(args: GetMyAccount) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    """Return own account info with telemetry recording."""
    logger.info("method[GetMyAccount] args[%s]", args)
    t0 = time.monotonic()
    error_type = None
    result_count = 0

    try:
        async with connected_client() as client:
            me = await client.get_me()
        if me is None:
            return [TextContent(type="text", text=_not_authenticated_text("GetMyAccount"))]
        name = " ".join(filter(None, [
            getattr(me, "first_name", None),
            getattr(me, "last_name", None),
        ]))
        username = getattr(me, "username", None) or "none"
        text = f"id={me.id} name='{name}' username=@{username}"
        result_count = 1
        result = [TextContent(type="text", text=text)]
    except Exception as exc:
        error_type = type(exc).__name__
        raise
    finally:
        duration_ms = (time.monotonic() - t0) * 1000
        try:
            from .analytics import TelemetryEvent
            collector = _get_analytics_collector()
            collector.record_event(TelemetryEvent(
                tool_name="GetMyAccount",
                timestamp=time.time(),
                duration_ms=duration_ms,
                result_count=result_count,
                has_cursor=False,
                page_depth=1,
                has_filter=False,
                error_type=error_type,
            ))
        except Exception as e:
            logger.error("Failed to record telemetry for GetMyAccount: %s", e)

    return result


### GetUserInfo ###


class GetUserInfo(ToolArgs):
    """
    Look up a Telegram user by name. Returns their profile (id, name, username) and
    the list of chats shared with this account. Resolves the name via fuzzy match.
    """

    user: str


@tool_runner.register
async def get_user_info(args: GetUserInfo) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    """Look up user info with telemetry recording."""
    logger.info("method[GetUserInfo] args[%s]", args)
    t0 = time.monotonic()
    error_type = None
    result_count = 0

    try:
        cache = get_entity_cache()
        result = resolve(args.user, cache.all_names_with_ttl(USER_TTL, GROUP_TTL), cache)
        if isinstance(result, NotFound):
            return [TextContent(type="text", text=_user_not_found_text(args.user, retry_tool="GetUserInfo"))]
        if isinstance(result, Candidates):
            match_lines = []
            for match in result.matches:
                line = f'id={match["entity_id"]} name="{match["display_name"]}" score={match["score"]}'
                if match.get("username"):
                    line += f' @{match["username"]}'
                if match.get("entity_type"):
                    line += f' [{match["entity_type"]}]'
                match_lines.append(line)
            return [
                TextContent(
                    type="text",
                    text=_ambiguous_user_text(args.user, match_lines, retry_tool="GetUserInfo"),
                )
            ]
        entity_id: int = result.entity_id
        display_name: str = result.display_name

        async with connected_client() as client:
            try:
                user = await client.get_entity(entity_id)
                common_result = await client(GetCommonChatsRequest(
                    user_id=entity_id,
                    max_id=0,
                    limit=100,
                ))
            except Exception as exc:
                return [TextContent(type="text", text=_fetch_user_info_error_text(args.user, str(exc)))]

        name = " ".join(filter(None, [
            getattr(user, "first_name", None),
            getattr(user, "last_name", None),
        ]))
        username = getattr(user, "username", None) or "none"
        chat_lines = []
        for chat in common_result.chats:
            chat_name = getattr(chat, "title", None) or getattr(chat, "first_name", str(chat.id))
            full_id = get_peer_id(chat)
            if isinstance(chat, Channel):
                ctype = "supergroup" if getattr(chat, "megagroup", False) else "channel"
            elif isinstance(chat, Chat):
                ctype = "group"
            else:
                ctype = "user"
            chat_lines.append(f"  id={full_id} type={ctype} name='{chat_name}'")
        chats_text = "\n".join(chat_lines) if chat_lines else "  (none)"
        text = (
            f'[resolved: "{display_name}"]\n'
            f"id={entity_id} name='{name}' username=@{username}\n"
            f"Common chats ({len(common_result.chats)}):\n{chats_text}"
        )
        result_count = 1
        result = [TextContent(type="text", text=text)]
    except Exception as exc:
        error_type = type(exc).__name__
        raise
    finally:
        duration_ms = (time.monotonic() - t0) * 1000
        try:
            from .analytics import TelemetryEvent
            collector = _get_analytics_collector()
            collector.record_event(TelemetryEvent(
                tool_name="GetUserInfo",
                timestamp=time.time(),
                duration_ms=duration_ms,
                result_count=result_count,
                has_cursor=False,
                page_depth=1,
                has_filter=False,
                error_type=error_type,
            ))
        except Exception as e:
            logger.error("Failed to record telemetry for GetUserInfo: %s", e)

    return result


### GetUsageStats ###


def format_usage_summary(stats: dict) -> str:
    """Generate <100 token natural-language summary of usage patterns.

    Input dict keys:
    - tool_distribution: dict[str, int] — {tool_name: count}
    - error_distribution: dict[str, int] — {error_type: count}
    - max_page_depth: int
    - dialogs_with_deep_scroll: int (estimated)
    - total_calls: int
    - filter_count: int
    - latency_median_ms: float
    - latency_p95_ms: float

    Output: natural-language string, target 60-80 tokens, < 100 hard limit.
    """
    parts = []

    # Tool frequency (top 2)
    if stats.get("tool_distribution"):
        sorted_tools = sorted(stats["tool_distribution"].items(), key=lambda x: x[1], reverse=True)
        top_tools = sorted_tools[:2]
        if top_tools:
            top_tool, top_count = top_tools[0]
            top_pct = int(top_count * 100 / stats["total_calls"]) if stats["total_calls"] > 0 else 0
            parts.append(f"Most active: {top_tool} ({top_pct}% of calls)")

    # Deep scroll detection
    if stats.get("max_page_depth", 0) >= 5:
        parts.append(f"Deep scrolling detected: max page depth {stats['max_page_depth']}")

    # Error patterns
    if stats.get("error_distribution"):
        errors_str = ", ".join(
            [f"{err} ({cnt})" for err, cnt in sorted(stats["error_distribution"].items(), key=lambda x: x[1], reverse=True)[:3]]
        )
        parts.append(f"Errors: {errors_str}")

    # Filter usage
    if stats.get("total_calls", 0) > 0 and stats.get("filter_count", 0) > 0:
        filter_pct = int(stats["filter_count"] * 100 / stats["total_calls"])
        parts.append(f"Filtered queries: {filter_pct}%")

    # Latency
    median = stats.get("latency_median_ms", 0)
    p95 = stats.get("latency_p95_ms", 0)
    if median or p95:
        parts.append(f"Response time: {median:.0f}ms median, {p95:.0f}ms p95")

    summary = " ".join(parts)

    # Safety: if summary exceeds 100 tokens, truncate gracefully
    tokens = summary.split()
    if len(tokens) > 100:
        summary = " ".join(tokens[:100]) + "..."

    return summary


class GetUsageStats(ToolArgs):
    """Get actionable usage statistics from telemetry (last 30 days)."""

    pass


@tool_runner.register
async def get_usage_stats(args: GetUsageStats) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    """Query analytics.db and return natural-language summary of usage patterns.

    NOTE: This tool does NOT record telemetry (to avoid noise in analytics).
    """
    logger.info("method[GetUsageStats] args[%s]", args)

    # Get analytics DB path
    db_dir = xdg_state_home() / "mcp-telegram"
    db_path = db_dir / "analytics.db"

    # Query analytics DB (30-day window)
    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        since = int(time.time()) - 30 * 86400

        # Tool distribution
        tool_dist = dict(
            cursor.execute(
                "SELECT tool_name, COUNT(*) FROM telemetry_events WHERE timestamp >= ? GROUP BY tool_name ORDER BY COUNT(*) DESC",
                (since,),
            ).fetchall()
        )

        # Error distribution
        error_dist = dict(
            cursor.execute(
                "SELECT error_type, COUNT(*) FROM telemetry_events WHERE timestamp >= ? AND error_type IS NOT NULL GROUP BY error_type ORDER BY COUNT(*) DESC",
                (since,),
            ).fetchall()
        )

        # Page depth stats
        max_depth_result = cursor.execute(
            "SELECT MAX(page_depth) FROM telemetry_events WHERE timestamp >= ?",
            (since,),
        ).fetchone()
        max_depth = max_depth_result[0] if max_depth_result and max_depth_result[0] is not None else 0

        # Filter usage
        filter_count_result = cursor.execute(
            "SELECT COUNT(*) FROM telemetry_events WHERE timestamp >= ? AND has_filter = 1",
            (since,),
        ).fetchone()
        filter_count = filter_count_result[0] if filter_count_result else 0

        # Total calls
        total_calls_result = cursor.execute(
            "SELECT COUNT(*) FROM telemetry_events WHERE timestamp >= ?",
            (since,),
        ).fetchone()
        total_calls = total_calls_result[0] if total_calls_result else 0

        # Latency percentiles
        latencies = cursor.execute(
            "SELECT duration_ms FROM telemetry_events WHERE timestamp >= ? ORDER BY duration_ms",
            (since,),
        ).fetchall()

        conn.close()

        # Compute percentiles
        latency_median_ms = 0
        latency_p95_ms = 0
        if latencies:
            sorted_latencies = [lat[0] for lat in latencies]
            latency_median_ms = sorted_latencies[len(sorted_latencies) // 2]
            p95_idx = int(len(sorted_latencies) * 0.95)
            latency_p95_ms = sorted_latencies[p95_idx] if p95_idx < len(sorted_latencies) else sorted_latencies[-1]

        # Format summary
        summary = format_usage_summary(
            {
                "tool_distribution": tool_dist,
                "error_distribution": error_dist,
                "max_page_depth": max_depth,
                "dialogs_with_deep_scroll": 0,  # Estimated (not tracked in this DB schema)
                "total_calls": total_calls,
                "filter_count": filter_count,
                "latency_median_ms": latency_median_ms,
                "latency_p95_ms": latency_p95_ms,
            }
        )

        return [TextContent(type="text", text=summary if summary else _no_usage_data_text())]

    except FileNotFoundError:
        return [TextContent(type="text", text=_usage_stats_db_missing_text())]
    except sqlite3.OperationalError as exc:
        # Table doesn't exist or DB not initialized yet
        if "no such table" in str(exc):
            return [TextContent(type="text", text=_usage_stats_db_missing_text())]
        logger.error("GetUsageStats query failed: %s", exc)
        return [TextContent(type="text", text=_usage_stats_query_error_text(type(exc).__name__))]
    except Exception as exc:
        logger.error("GetUsageStats query failed: %s", exc)
        return [TextContent(type="text", text=_usage_stats_query_error_text(type(exc).__name__))]
