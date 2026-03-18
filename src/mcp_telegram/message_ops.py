from __future__ import annotations

import logging
from typing import Callable

from telethon.errors import RPCError
from telethon.tl.functions.messages import GetMessageReactionsListRequest

from .cache import EntityCache, GROUP_TTL, USER_TTL, ReactionMetadataCache, TopicMetadataCache
from .dialog_target import get_sender_type
from .errors import (
    ambiguous_sender_text,
    invalid_navigation_text,
    rpc_error_detail,
    sender_not_found_text,
)
from .forum_topics import (
    build_topic_name_getter,
    fetch_topic_messages,
    is_topic_id_invalid_error,
    load_forum_topic_capability,
    message_matches_topic,
    messages_need_forum_topic_labels,
    refresh_topic_by_id,
)
from .models import (
    HISTORY_NAVIGATION_NEWEST,
    HISTORY_NAVIGATION_OLDEST,
    HistoryNavigationMode,
    ForumTopicFailure,
    MessageReadFailure,
    NavigationFailure,
    TopicCatalog,
    TopicMetadata,
    TopicLoader,
)
from .pagination import decode_history_navigation, decode_navigation_token
from .resolver import Candidates, NotFound, resolve

logger = logging.getLogger(__name__)


def parse_history_navigation_input(
    navigation: str | None,
    *,
    retry_tool: str,
) -> tuple[str | None, HistoryNavigationMode] | NavigationFailure:
    """Parse one public ListMessages navigation value into token and direction."""
    if navigation is None or navigation == HISTORY_NAVIGATION_NEWEST:
        return None, HISTORY_NAVIGATION_NEWEST
    if navigation == HISTORY_NAVIGATION_OLDEST:
        return None, HISTORY_NAVIGATION_OLDEST

    try:
        token = decode_navigation_token(navigation)
    except ValueError as exc:
        return NavigationFailure(
            kind="invalid_navigation",
            text=invalid_navigation_text(str(exc), retry_tool=retry_tool),
        )

    if token.kind != "history":
        return NavigationFailure(
            kind="invalid_navigation",
            text=invalid_navigation_text(
                f"Navigation token is for {token.kind}, not history",
                retry_tool=retry_tool,
            ),
        )

    return navigation, token.direction or HISTORY_NAVIGATION_NEWEST


def _sender_match_line(match: dict[str, object]) -> str:
    line = f'id={match["entity_id"]} name="{match["display_name"]}" score={match["score"]}'
    username = match.get("username")
    entity_type = match.get("entity_type")
    if username:
        line += f" @{username}"
    if entity_type:
        line += f" [{entity_type}]"
    return line


def _build_history_iter_kwargs(
    *,
    entity_id: int,
    limit: int,
    navigation: str | None,
    topic_id: int | None,
    retry_tool: str,
) -> dict[str, object] | MessageReadFailure | NavigationFailure:
    navigation_result = parse_history_navigation_input(
        navigation,
        retry_tool=retry_tool,
    )
    if isinstance(navigation_result, NavigationFailure):
        return navigation_result

    navigation_token, direction = navigation_result
    from_beginning = direction == HISTORY_NAVIGATION_OLDEST
    iter_kwargs: dict[str, object] = {
        "entity": entity_id,
        "limit": limit,
        "reverse": from_beginning,
    }

    shared_cursor_id: int | None = None
    if navigation_token is not None:
        try:
            shared_cursor_id = decode_history_navigation(
                navigation_token,
                expected_dialog_id=entity_id,
                expected_topic_id=topic_id,
                expected_direction=direction,
            )
        except ValueError as exc:
            return NavigationFailure(
                kind="invalid_navigation",
                text=invalid_navigation_text(str(exc), retry_tool=retry_tool),
            )

    if from_beginning:
        if shared_cursor_id is not None:
            iter_kwargs["min_id"] = shared_cursor_id
        else:
            iter_kwargs["min_id"] = 1
        return iter_kwargs

    if shared_cursor_id is not None:
        iter_kwargs["max_id"] = shared_cursor_id
        return iter_kwargs

    return iter_kwargs


def _resolve_sender_entity(
    *,
    cache: EntityCache,
    sender_query: str | None,
    retry_tool: str,
) -> int | MessageReadFailure | None:
    if sender_query is None:
        return None

    choices = cache.all_names_with_ttl(USER_TTL, GROUP_TTL)
    normalized = cache.all_names_normalized_with_ttl(USER_TTL, GROUP_TTL)
    sender_result = resolve(sender_query, choices, cache, normalized_choices=normalized)
    if isinstance(sender_result, NotFound):
        return MessageReadFailure(
            kind="sender_not_found",
            text=sender_not_found_text(sender_query, retry_tool=retry_tool),
        )
    if isinstance(sender_result, Candidates):
        match_lines = [_sender_match_line(match) for match in sender_result.matches]
        return MessageReadFailure(
            kind="sender_ambiguous",
            text=ambiguous_sender_text(sender_query, match_lines, retry_tool=retry_tool),
        )
    return sender_result.entity_id


def _cache_message_senders(
    *,
    cache: EntityCache,
    messages: list[object],
) -> None:
    for msg in messages:
        sender = getattr(msg, "sender", None)
        sender_id = getattr(msg, "sender_id", None)
        if sender is None or not isinstance(sender_id, int):
            continue
        sender_name = " ".join(
            filter(
                None,
                [
                    getattr(sender, "first_name", None),
                    getattr(sender, "last_name", None),
                ],
            )
        ) or getattr(sender, "title", "") or str(sender_id)
        cache.upsert(
            sender_id,
            get_sender_type(sender),
            sender_name,
            getattr(sender, "username", None),
        )


async def _build_reply_map(
    client: object,
    *,
    entity_id: int,
    messages: list[object],
) -> dict[int, object]:
    reply_ids = list(
        {
            reply_to_msg_id
            for msg in messages
            for reply_to_msg_id in [getattr(getattr(msg, "reply_to", None), "reply_to_msg_id", None)]
            if isinstance(reply_to_msg_id, int)
        }
    )
    if not reply_ids:
        return {}

    replied = await client.get_messages(entity_id, ids=reply_ids)
    replied_list = replied if isinstance(replied, list) else [replied]
    return {
        getattr(message, "id"): message
        for message in replied_list
        if message is not None and isinstance(getattr(message, "id", None), int)
    }


async def _build_reaction_names_map(
    client: object,
    *,
    cache: EntityCache,
    entity_id: int,
    messages: list[object],
    reaction_names_threshold: int,
) -> dict[int, dict[str, list[str]]]:
    reaction_names_map: dict[int, dict[str, list[str]]] = {}
    reaction_cache = ReactionMetadataCache(cache._conn)

    for msg in messages:
        message_id = getattr(msg, "id", None)
        if not isinstance(message_id, int):
            continue
        rxns = getattr(msg, "reactions", None)
        if not rxns:
            continue

        results = getattr(rxns, "results", None) or []
        total = sum(getattr(result, "count", 0) for result in results)
        if total == 0 or total > reaction_names_threshold:
            continue

        cached_reactions = reaction_cache.get(message_id, entity_id, ttl_seconds=600)
        if cached_reactions:
            reaction_names_map[message_id] = cached_reactions
            continue

        try:
            reaction_list = await client(
                GetMessageReactionsListRequest(
                    peer=entity_id,
                    id=message_id,
                    limit=100,
                )
            )
        except Exception:
            continue

        user_by_id = {
            getattr(user, "id"): user
            for user in (getattr(reaction_list, "users", None) or [])
            if isinstance(getattr(user, "id", None), int)
        }
        names_by_emoji: dict[str, list[str]] = {}
        for entry in (getattr(reaction_list, "reactions", None) or []):
            emoji = getattr(getattr(entry, "reaction", None), "emoticon", None) or "?"
            user_id = getattr(getattr(entry, "peer_id", None), "user_id", None)
            if not isinstance(user_id, int) or user_id not in user_by_id:
                continue
            user = user_by_id[user_id]
            name = " ".join(
                filter(
                    None,
                    [
                        getattr(user, "first_name", None),
                        getattr(user, "last_name", None),
                    ],
                )
            ) or str(user_id)
            cache.upsert(user_id, "user", name, getattr(user, "username", None))
            names_by_emoji.setdefault(str(emoji), []).append(name)

        if names_by_emoji:
            reaction_names_map[message_id] = names_by_emoji
            reaction_cache.upsert(message_id, entity_id, names_by_emoji)

    return reaction_names_map


async def _build_context_message_map(
    client: object,
    *,
    entity_id: int,
    hits: list[object],
    context_radius: int,
) -> dict[int, object]:
    context_ids_needed: set[int] = set()
    hit_ids = {
        message_id
        for hit in hits
        for message_id in [getattr(hit, "id", None)]
        if isinstance(message_id, int)
    }
    for hit_id in hit_ids:
        for offset in range(-context_radius, context_radius + 1):
            if offset != 0:
                context_ids_needed.add(hit_id + offset)
    context_ids_needed -= hit_ids
    if not context_ids_needed:
        return {}

    fetched = await client.get_messages(entity_id, ids=list(context_ids_needed))
    fetched_list = fetched if isinstance(fetched, list) else [fetched]
    return {
        message_id: message
        for message in fetched_list
        for message_id in [getattr(message, "id", None)]
        if message is not None and isinstance(message_id, int)
    }


async def _build_cross_topic_name_getter(
    client: object,
    *,
    cache: EntityCache,
    entity_id: int,
    dialog_name: str,
    fetched_messages: list[object],
    topic_catalog: TopicCatalog | None,
    topic_cache: TopicMetadataCache | None,
    load_topics: TopicLoader | None,
) -> Callable[[object], str | None] | None:
    dialog_cache_entry = cache.get(entity_id, GROUP_TTL)
    if (
        not fetched_messages
        or dialog_cache_entry is None
        or dialog_cache_entry.get("type") != "group"
    ):
        return None

    active_topic_cache = topic_cache if topic_cache is not None else TopicMetadataCache(cache._conn)
    active_topic_catalog = topic_catalog
    if active_topic_catalog is None:
        topic_capability = await load_forum_topic_capability(
            client,
            entity=entity_id,
            dialog_id=entity_id,
            dialog_name=dialog_name,
            topic_cache=active_topic_cache,
            requested_topic=None,
            retry_tool="ListMessages",
            load_topics=load_topics,
        )
        if isinstance(topic_capability, ForumTopicFailure):
            return None
        active_topic_catalog = topic_capability

    if active_topic_catalog is None:
        return None
    if not (
        messages_need_forum_topic_labels(fetched_messages)
        or len(active_topic_catalog["choices"]) > 1
    ):
        return None
    return build_topic_name_getter(active_topic_catalog)


async def fetch_messages_for_topic(
    client: object,
    *,
    entity_id: int,
    iter_kwargs: dict[str, object],
    topic_metadata: TopicMetadata,
    topic_cache: TopicMetadataCache,
    allow_headerless_messages: bool,
    fetch_topic_messages_fn: Callable | None = None,
    refresh_topic_by_id_fn: Callable | None = None,
) -> tuple[list[object] | None, TopicMetadata, dict[str, object]]:
    """Fetch one topic page with one bounded by-ID refresh and retry on stale anchors."""
    active_iter_kwargs = dict(iter_kwargs)
    active_topic_metadata = topic_metadata
    original_exc: RPCError | None = None
    retry_invalid_exc: RPCError | None = None
    active_fetch_topic_messages = fetch_topic_messages_fn if fetch_topic_messages_fn is not None else fetch_topic_messages
    active_refresh_topic_by_id = refresh_topic_by_id_fn if refresh_topic_by_id_fn is not None else refresh_topic_by_id

    async def scan_dialog_history_for_topic() -> tuple[list[object], dict[str, object]]:
        """Fallback to dialog-wide history scanning when thread fetch rejects a valid topic anchor."""
        history_iter_kwargs = dict(active_iter_kwargs)
        history_iter_kwargs.pop("reply_to", None)
        messages = await active_fetch_topic_messages(
            client,
            iter_kwargs=history_iter_kwargs,
            topic_metadata=active_topic_metadata,
            allow_headerless_messages=False,
        )
        return messages, history_iter_kwargs

    try:
        messages = await active_fetch_topic_messages(
            client,
            iter_kwargs=active_iter_kwargs,
            topic_metadata=active_topic_metadata,
            allow_headerless_messages=allow_headerless_messages,
        )
        return messages, active_topic_metadata, active_iter_kwargs
    except RPCError as exc:
        if not is_topic_id_invalid_error(exc):
            raise
        original_exc = exc

    refreshed_topic = await active_refresh_topic_by_id(
        client,
        entity=entity_id,
        dialog_id=entity_id,
        topic_id=int(active_topic_metadata["topic_id"]),
        topic_cache=topic_cache,
    )
    if refreshed_topic is None:
        if original_exc is not None:
            raise original_exc
        raise RuntimeError("Missing original TOPIC_ID_INVALID exception during refresh path")

    active_topic_metadata = refreshed_topic
    if bool(active_topic_metadata["is_deleted"]):
        return None, active_topic_metadata, active_iter_kwargs

    refreshed_top_message_id = active_topic_metadata["top_message_id"]
    if refreshed_top_message_id is None:
        if original_exc is not None:
            raise original_exc
        raise RuntimeError("Missing original TOPIC_ID_INVALID exception during refresh path")

    refreshed_reply_to = int(refreshed_top_message_id)
    if active_iter_kwargs.get("reply_to") == refreshed_reply_to:
        dialog_messages, dialog_iter_kwargs = await scan_dialog_history_for_topic()
        if dialog_messages:
            return dialog_messages, active_topic_metadata, dialog_iter_kwargs
        if original_exc is not None:
            raise original_exc
        raise RuntimeError("Missing original TOPIC_ID_INVALID exception during refresh path")

    active_iter_kwargs["reply_to"] = refreshed_reply_to
    try:
        messages = await active_fetch_topic_messages(
            client,
            iter_kwargs=active_iter_kwargs,
            topic_metadata=active_topic_metadata,
            allow_headerless_messages=allow_headerless_messages,
        )
        return messages, active_topic_metadata, active_iter_kwargs
    except RPCError as retry_exc:
        if not is_topic_id_invalid_error(retry_exc):
            raise
        retry_invalid_exc = retry_exc

    dialog_messages, dialog_iter_kwargs = await scan_dialog_history_for_topic()
    if dialog_messages:
        return dialog_messages, active_topic_metadata, dialog_iter_kwargs
    if retry_invalid_exc is not None:
        raise retry_invalid_exc
    raise RuntimeError("Missing retry TOPIC_ID_INVALID exception during dialog-scan fallback")
