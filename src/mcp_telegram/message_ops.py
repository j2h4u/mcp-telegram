from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable, NoReturn, cast

from telethon.errors import RPCError  # type: ignore[import-untyped]

if TYPE_CHECKING:
    from telethon import TelegramClient  # type: ignore[import-untyped]

from telethon.tl.functions.messages import GetMessageReactionsListRequest  # type: ignore[import-untyped]

from .cache import CachedMessage, EntityCache, GROUP_TTL, MessageCache, USER_TTL, ReactionMetadataCache, TopicMetadataCache
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
    MessageLike,
    ForumTopicFailure,
    MessageReadFailure,
    NavigationFailure,
    TopicCatalog,
    TopicMetadata,
    TopicLoader,
)
from .pagination import HistoryDirection, decode_history_navigation, decode_navigation_token
from .resolver import Candidates, NotFound, resolve

logger = logging.getLogger(__name__)


def parse_history_navigation_input(
    navigation: str | None,
    *,
    retry_tool: str,
) -> tuple[str | None, HistoryDirection] | NavigationFailure:
    """Parse one public ListMessages navigation value into token and direction."""
    if navigation is None or navigation == HistoryDirection.NEWEST:
        return None, HistoryDirection.NEWEST
    if navigation == HistoryDirection.OLDEST:
        return None, HistoryDirection.OLDEST

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

    return navigation, token.direction or HistoryDirection.NEWEST


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
    """Build kwargs dict for ``client.iter_messages()`` from navigation input.

    On success returns a dict suitable for ``**kwargs`` expansion into
    ``iter_messages``.  Keys always include ``entity``, ``limit``, ``reverse``;
    may include ``min_id`` or ``max_id`` depending on direction and cursor.
    Returns ``NavigationFailure`` on invalid cursor tokens.
    """
    navigation_result = parse_history_navigation_input(
        navigation,
        retry_tool=retry_tool,
    )
    if isinstance(navigation_result, NavigationFailure):
        return navigation_result

    navigation_token, direction = navigation_result
    from_beginning = direction == HistoryDirection.OLDEST
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
    """Resolve sender filter: None → no filter, int → entity_id, failure → not found/ambiguous."""
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
    messages: list[MessageLike],
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


def _get_reply_to_id(msg: object) -> int | None:
    """Extract reply_to_msg_id from a Telethon message, or None."""
    reply_to = getattr(msg, "reply_to", None)
    reply_id = getattr(reply_to, "reply_to_msg_id", None) if reply_to else None
    return reply_id if isinstance(reply_id, int) else None


def _get_message_id(msg: object) -> int | None:
    """Extract message id from a Telethon message, or None."""
    msg_id = getattr(msg, "id", None)
    return msg_id if isinstance(msg_id, int) else None


async def _build_reply_map(
    client: TelegramClient,
    *,
    entity_id: int,
    messages: list[MessageLike],
    msg_cache: MessageCache | None = None,
) -> dict[int, MessageLike]:
    """Fetch original messages for all reply references in the message list.

    Tries MessageCache first for each reply ID; falls back to the Telegram API
    only for IDs not found in cache.
    """
    reply_ids = list({rid for msg in messages if (rid := _get_reply_to_id(msg)) is not None})
    if not reply_ids:
        return {}

    result: dict[int, MessageLike] = {}
    uncached_ids: list[int] = []

    if msg_cache is not None:
        for rid in reply_ids:
            rows = msg_cache._conn.execute(
                "SELECT dialog_id, message_id, sent_at, text, sender_id, sender_first_name, "
                "media_description, reply_to_msg_id, forum_topic_id, edit_date, fetched_at "
                "FROM message_cache WHERE dialog_id = ? AND message_id = ?",
                (entity_id, rid),
            ).fetchall()
            if rows:
                result[rid] = cast("MessageLike", CachedMessage.from_row(rows[0]))
            else:
                uncached_ids.append(rid)
    else:
        uncached_ids = reply_ids

    if uncached_ids:
        replied = await client.get_messages(entity_id, ids=uncached_ids)
        replied_list = replied if isinstance(replied, list) else [replied]
        for message in replied_list:
            if message is not None and (mid := _get_message_id(message)) is not None:
                result[mid] = message

    return result


async def _build_reaction_names_map(
    client: TelegramClient,
    *,
    cache: EntityCache,
    entity_id: int,
    messages: list[MessageLike],
    reaction_names_threshold: int,
) -> dict[int, dict[str, list[str]]]:
    """Fetch reactor names for messages with total reactions ≤ threshold.

    Skips messages above threshold (busy groups). Caches results in
    ReactionMetadataCache. Side effect: upserts reactor users into EntityCache.
    """
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
            logger.debug(
                "reaction_names_fetch_failed msg_id=%d dialog_id=%d",
                message_id, entity_id, exc_info=True,
            )
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
    client: TelegramClient,
    *,
    entity_id: int,
    hits: list[MessageLike],
    context_radius: int,
) -> dict[int, MessageLike]:
    """Fetch surrounding context messages for search hits."""
    context_ids_needed: set[int] = set()
    hit_ids = {mid for hit in hits if (mid := _get_message_id(hit)) is not None}
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
        mid: message
        for message in fetched_list
        if message is not None and (mid := _get_message_id(message)) is not None
    }


async def _build_cross_topic_name_getter(
    client: TelegramClient,
    *,
    cache: EntityCache,
    entity_id: int,
    dialog_name: str,
    fetched_messages: list[MessageLike],
    topic_catalog: TopicCatalog | None,
    topic_cache: TopicMetadataCache | None,
    load_topics: TopicLoader | None,
) -> Callable[[object], str | None] | None:
    """Return a topic-label getter for groups, or None for non-group dialogs."""
    cached_entity = cache.get(entity_id, GROUP_TTL)
    if (
        not fetched_messages
        or cached_entity is None
        or cached_entity.get("type") != "group"
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
        # requested_topic=None → always TopicCatalog, never ResolvedForumTopic
        active_topic_catalog = topic_capability  # type: ignore[assignment]

    if active_topic_catalog is None:
        return None
    if not (
        messages_need_forum_topic_labels(fetched_messages)
        or len(active_topic_catalog["choices"]) > 1
    ):
        return None
    return build_topic_name_getter(active_topic_catalog)


def _reraise_topic_invalid(exc: RPCError | None, context: str) -> NoReturn:
    """Re-raise captured TOPIC_ID_INVALID or raise RuntimeError if unexpectedly absent."""
    if exc is not None:
        raise exc
    raise RuntimeError(f"Missing {context} TOPIC_ID_INVALID exception — this is a bug")


async def fetch_messages_for_topic(
    client: TelegramClient,
    *,
    entity_id: int,
    iter_kwargs: dict[str, object],
    topic_metadata: TopicMetadata,
    topic_cache: TopicMetadataCache,
    allow_headerless_messages: bool,
    fetch_topic_messages_fn: Callable | None = None,
    refresh_topic_by_id_fn: Callable | None = None,
) -> tuple[list[MessageLike] | None, TopicMetadata, dict[str, object]]:
    """Fetch one topic page with one bounded by-ID refresh and retry on stale anchors."""
    current_iter_kwargs = dict(iter_kwargs)
    current_topic_metadata = topic_metadata
    original_exc: RPCError | None = None
    retry_invalid_exc: RPCError | None = None
    fetch_fn = fetch_topic_messages_fn if fetch_topic_messages_fn is not None else fetch_topic_messages
    refresh_fn = refresh_topic_by_id_fn if refresh_topic_by_id_fn is not None else refresh_topic_by_id

    async def scan_dialog_history_for_topic() -> tuple[list[MessageLike], dict[str, object]]:
        """Fallback to dialog-wide history scanning when thread fetch rejects a valid topic anchor."""
        history_iter_kwargs = dict(current_iter_kwargs)
        history_iter_kwargs.pop("reply_to", None)
        messages = await fetch_fn(
            client,
            iter_kwargs=history_iter_kwargs,
            topic_metadata=current_topic_metadata,
            allow_headerless_messages=False,
        )
        return messages, history_iter_kwargs

    try:
        messages = await fetch_fn(
            client,
            iter_kwargs=current_iter_kwargs,
            topic_metadata=current_topic_metadata,
            allow_headerless_messages=allow_headerless_messages,
        )
        return messages, current_topic_metadata, current_iter_kwargs
    except RPCError as exc:
        if not is_topic_id_invalid_error(exc):
            raise
        original_exc = exc

    refreshed_topic = await refresh_fn(
        client,
        entity=entity_id,
        dialog_id=entity_id,
        topic_id=int(current_topic_metadata["topic_id"]),
        topic_cache=topic_cache,
    )
    if refreshed_topic is None:
        _reraise_topic_invalid(original_exc, "original")

    current_topic_metadata = refreshed_topic
    if bool(current_topic_metadata["is_deleted"]):
        return None, current_topic_metadata, current_iter_kwargs

    refreshed_top_message_id = current_topic_metadata["top_message_id"]
    if refreshed_top_message_id is None:
        _reraise_topic_invalid(original_exc, "original")

    refreshed_reply_to = int(refreshed_top_message_id)
    if current_iter_kwargs.get("reply_to") == refreshed_reply_to:
        dialog_messages, dialog_iter_kwargs = await scan_dialog_history_for_topic()
        if dialog_messages:
            return dialog_messages, current_topic_metadata, dialog_iter_kwargs
        _reraise_topic_invalid(original_exc, "original")

    current_iter_kwargs["reply_to"] = refreshed_reply_to
    try:
        messages = await fetch_fn(
            client,
            iter_kwargs=current_iter_kwargs,
            topic_metadata=current_topic_metadata,
            allow_headerless_messages=allow_headerless_messages,
        )
        return messages, current_topic_metadata, current_iter_kwargs
    except RPCError as retry_exc:
        if not is_topic_id_invalid_error(retry_exc):
            raise
        retry_invalid_exc = retry_exc

    dialog_messages, dialog_iter_kwargs = await scan_dialog_history_for_topic()
    if dialog_messages:
        return dialog_messages, current_topic_metadata, dialog_iter_kwargs
    _reraise_topic_invalid(retry_invalid_exc, "retry")
