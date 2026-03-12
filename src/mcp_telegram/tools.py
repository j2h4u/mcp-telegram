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
from pydantic import BaseModel, ConfigDict
from telethon import TelegramClient, custom, functions, types  # type: ignore[import-untyped]
from telethon.tl.functions.messages import (
    GetCommonChatsRequest,
    GetMessageReactionsListRequest,
    GetPeerDialogsRequest,
)
from telethon.tl.types import Channel, Chat
from telethon.utils import get_peer_id
from xdg_base_dirs import xdg_state_home

from .cache import (
    EntityCache,
    GROUP_TTL,
    USER_TTL,
    ReactionMetadataCache,
    TopicMetadataCache,
)
from .formatter import format_messages
from .pagination import decode_cursor, encode_cursor
from .resolver import Candidates, NotFound, resolve
from .telegram import create_client

# Fetch reactor names only when total reactions per message are at or below this limit.
# Covers personal chats (always ≤ a few) while skipping expensive lookups on busy groups.
REACTION_NAMES_THRESHOLD = 15
FORUM_TOPICS_PAGE_SIZE = 100
TOPIC_METADATA_TTL_SECONDS = 600
GENERAL_TOPIC_ID = 1
GENERAL_TOPIC_TITLE = "General"

logger = logging.getLogger(__name__)


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
    return Tool(
        name=args.__name__,
        description=args.__doc__,
        inputSchema=args.model_json_schema(),
    )


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
    return {
        "topic_id": topic_id,
        "title": GENERAL_TOPIC_TITLE if is_general else title,
        "top_message_id": top_message_id,
        "is_general": is_general,
        "is_deleted": is_deleted,
    }


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
    response = await client(functions.channels.GetForumTopicsRequest(
        channel=entity,
        offset_date=offset_date,
        offset_id=offset_id,
        offset_topic=offset_topic,
        limit=limit,
    ))
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
    response = await client(functions.channels.GetForumTopicsByIDRequest(
        channel=entity,
        topics=[topic_id],
    ))
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
                entity = dialog.entity
                username: str | None = getattr(entity, "username", None)
                cache.upsert(dialog.id, dtype, dialog.name, username)
                lines.append(
                    f"name='{dialog.name}' id={dialog.id} type={dtype} "
                    f"last_message_at={last_at} unread={dialog.unread_count}"
                )
        result_count = len(lines)
        result = [TextContent(type="text", text="\n".join(lines))]
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


class ListMessages(ToolArgs):
    """
    List messages in a dialog by name. Returns messages newest-first in human-readable format
    (HH:mm FirstName: text) with date headers and session breaks.

    Use cursor= with the next_cursor token from a previous response to page back in time.
    Use sender= to filter messages from a specific person (name string, resolved via fuzzy match).
    Use topic= to filter messages to one forum topic after the dialog has been resolved.
    Use unread=True to show only messages you haven't read yet.
    Use from_beginning=True to fetch messages oldest-first (starts from message ID 1). When true,
    pagination reads forward through time rather than backward.

    If response is ambiguous (multiple matches), use the numeric id= parameter with the ID from the matches list.
    For @username lookups, prepend @ to the name: dialog="@username".
    """

    dialog: str
    limit: int = 100
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
    has_filter = False
    page_depth = 1

    try:
        # Step 1 — Resolve dialog name
        cache = get_entity_cache()
        result = resolve(args.dialog, cache.all_names_with_ttl(USER_TTL, GROUP_TTL), cache)
        if isinstance(result, NotFound):
            return [TextContent(type="text", text=f'Dialog not found: "{args.dialog}"')]
        if isinstance(result, Candidates):
            match_lines = []
            for match in result.matches:
                line = f'id={match["entity_id"]} name="{match["display_name"]}" score={match["score"]}'
                if match.get("username"):
                    line += f' @{match["username"]}'
                if match.get("entity_type"):
                    line += f' [{match["entity_type"]}]'
                match_lines.append(line)
            return [TextContent(type="text", text=f'Ambiguous "{args.dialog}". Matches:\n' + "\n".join(match_lines))]
        entity_id: int = result.entity_id
        resolve_prefix = (
            f'[resolved: "{args.dialog}" → {result.display_name}]\n'
            if args.dialog.strip().lower() != result.display_name.strip().lower()
            else ""
        )
        topic_metadata: dict[str, int | str | bool | None] | None = None

        # Step 2 — Build iter_messages kwargs
        iter_kwargs: dict[str, t.Any] = {
            "entity": entity_id,
            "limit": args.limit,
            "reverse": args.from_beginning,  # Toggle iteration direction based on parameter
        }

        # Handle cursor based on iteration direction
        if args.from_beginning:
            # Reverse iteration: use min_id (page forward from oldest)
            if args.cursor:
                try:
                    iter_kwargs["min_id"] = decode_cursor(args.cursor, entity_id)
                except Exception as exc:
                    return [TextContent(type="text", text=f"Invalid cursor: {exc}")]
            else:
                iter_kwargs["min_id"] = 1  # Start from oldest message
        else:
            # Forward iteration: use max_id (page backward from newest)
            if args.cursor:
                try:
                    iter_kwargs["max_id"] = decode_cursor(args.cursor, entity_id)
                except Exception as exc:
                    return [TextContent(type="text", text=f"Invalid cursor: {exc}")]

        # Step 3 — Sender filter (resolve before opening client)
        if args.sender:
            has_filter = True
            sender_result = resolve(args.sender, cache.all_names_with_ttl(USER_TTL, GROUP_TTL), cache)
            if isinstance(sender_result, NotFound):
                return [TextContent(type="text", text=f'Sender not found: "{args.sender}"')]
            if isinstance(sender_result, Candidates):
                match_lines = []
                for match in sender_result.matches:
                    line = f'id={match["entity_id"]} name="{match["display_name"]}" score={match["score"]}'
                    if match.get("username"):
                        line += f' @{match["username"]}'
                    if match.get("entity_type"):
                        line += f' [{match["entity_type"]}]'
                    match_lines.append(line)
                return [TextContent(type="text", text=f'Ambiguous sender "{args.sender}". Matches:\n' + "\n".join(match_lines))]
            iter_kwargs["from_user"] = sender_result.entity_id

        # Track unread as a filter
        if args.unread:
            has_filter = True

        # Step 4 — Topic resolution + unread filter + message fetch + format + cursor
        async with connected_client() as client:
            if args.topic:
                has_filter = True
                topic_cache = TopicMetadataCache(cache._conn)
                topic_catalog = await _load_dialog_topics(
                    client,
                    entity=entity_id,
                    dialog_id=entity_id,
                    topic_cache=topic_cache,
                )
                topic_result = resolve(args.topic, topic_catalog["choices"])
                if isinstance(topic_result, NotFound):
                    return [TextContent(type="text", text=f'Topic not found: "{args.topic}"')]
                if isinstance(topic_result, Candidates):
                    match_lines = []
                    for match in topic_result.matches:
                        match_lines.append(
                            f'id={match["entity_id"]} name="{match["display_name"]}" score={match["score"]}'
                        )
                    return [
                        TextContent(
                            type="text",
                            text=f'Ambiguous topic "{args.topic}". Matches:\n' + "\n".join(match_lines),
                        )
                    ]
                topic_metadata = topic_catalog["metadata_by_id"].get(topic_result.entity_id)

            if args.unread:
                input_peer = await client.get_input_entity(entity_id)
                peer_result = await client(GetPeerDialogsRequest(peers=[input_peer]))
                tl_dialog = peer_result.dialogs[0]
                iter_kwargs["min_id"] = tl_dialog.read_inbox_max_id

            messages = [msg async for msg in client.iter_messages(**iter_kwargs)]

            # Lazy cache population: upsert sender entities
            for msg in messages:
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

            # Build reply_map for reply annotations
            reply_ids = list({
                msg.reply_to.reply_to_msg_id
                for msg in messages
                if getattr(msg, "reply_to", None) and getattr(msg.reply_to, "reply_to_msg_id", None)
            })
            reply_map: dict[int, object] = {}
            if reply_ids:
                replied = await client.get_messages(entity_id, ids=reply_ids)
                replied_list = replied if isinstance(replied, list) else [replied]
                reply_map = {m.id: m for m in replied_list if m}

            # Build reaction_names_map: fetch reactor names for messages with few reactions
            reaction_names_map: dict[int, dict[str, list[str]]] = {}
            reaction_cache = ReactionMetadataCache(cache._conn)
            for msg in messages:
                rxns = getattr(msg, "reactions", None)
                if not rxns:
                    continue
                results = getattr(rxns, "results", None) or []
                total = sum(getattr(r, "count", 0) for r in results)
                if total == 0 or total > REACTION_NAMES_THRESHOLD:
                    continue

                # Check reaction cache first (10-minute TTL)
                cached_reactions = reaction_cache.get(msg.id, entity_id, ttl_seconds=600)
                if cached_reactions:
                    reaction_names_map[msg.id] = cached_reactions
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
                        # Cache the reaction names for future requests
                        reaction_cache.upsert(msg.id, entity_id, by_emoji)
                except Exception:
                    pass  # fallback to count-only

        text = format_messages(messages, reply_map=reply_map, reaction_names_map=reaction_names_map)
        next_cursor: str | None = None
        if len(messages) == args.limit and messages:
            next_cursor = encode_cursor(messages[-1].id, entity_id)

        result_count = len(messages)
        result_text = resolve_prefix + text
        if next_cursor:
            result_text += f"\n\nnext_cursor: {next_cursor}"
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
        result = resolve(args.dialog, cache.all_names_with_ttl(USER_TTL, GROUP_TTL), cache)
        if isinstance(result, NotFound):
            return [TextContent(type="text", text=f'Dialog not found: "{args.dialog}"')]
        if isinstance(result, Candidates):
            match_lines = []
            for match in result.matches:
                line = f'id={match["entity_id"]} name="{match["display_name"]}" score={match["score"]}'
                if match.get("username"):
                    line += f' @{match["username"]}'
                if match.get("entity_type"):
                    line += f' [{match["entity_type"]}]'
                match_lines.append(line)
            return [TextContent(type="text", text=f'Ambiguous "{args.dialog}". Matches:\n' + "\n".join(match_lines))]
        entity_id: int = result.entity_id
        resolve_prefix = (
            f'[resolved: "{args.dialog}" → {result.display_name}]\n'
            if args.dialog.strip().lower() != result.display_name.strip().lower()
            else ""
        )

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
        result_text = resolve_prefix + "\n\n".join(parts) if parts else resolve_prefix
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
            return [TextContent(type="text", text="Not authenticated")]
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
            return [TextContent(type="text", text=f'User not found: "{args.user}"')]
        if isinstance(result, Candidates):
            match_lines = []
            for match in result.matches:
                line = f'id={match["entity_id"]} name="{match["display_name"]}" score={match["score"]}'
                if match.get("username"):
                    line += f' @{match["username"]}'
                if match.get("entity_type"):
                    line += f' [{match["entity_type"]}]'
                match_lines.append(line)
            return [TextContent(type="text", text=f'Ambiguous user "{args.user}". Matches:\n' + "\n".join(match_lines))]
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
                return [TextContent(type="text", text=f"Error fetching user info: {exc}")]

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

        return [TextContent(type="text", text=summary if summary else "No usage data in past 30 days.")]

    except FileNotFoundError:
        return [TextContent(type="text", text="Analytics database not yet created. Use other tools first to generate telemetry.")]
    except sqlite3.OperationalError as exc:
        # Table doesn't exist or DB not initialized yet
        if "no such table" in str(exc):
            return [TextContent(type="text", text="Analytics database not yet created. Use other tools first to generate telemetry.")]
        logger.error("GetUsageStats query failed: %s", exc)
        return [TextContent(type="text", text=f"Error querying usage stats: {type(exc).__name__}")]
    except Exception as exc:
        logger.error("GetUsageStats query failed: %s", exc)
        return [TextContent(type="text", text=f"Error querying usage stats: {type(exc).__name__}")]
