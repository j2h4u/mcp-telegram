from __future__ import annotations

from typing import TYPE_CHECKING

from telethon.errors import RPCError  # type: ignore[import-untyped]
from telethon.tl.functions.messages import GetPeerDialogsRequest  # type: ignore[import-untyped]

if TYPE_CHECKING:
    from telethon import TelegramClient  # type: ignore[import-untyped]

from .cache import EntityCache, MessageCache, TopicMetadataCache, _should_try_cache
from .dialog_target import resolve_dialog_target
from .pagination import HistoryDirection
from .errors import deleted_topic_text, inaccessible_topic_text, rpc_error_detail
from .forum_topics import load_forum_topic_capability, topic_empty_state_text
from .message_ops import (
    _build_cross_topic_name_getter,
    _build_reaction_names_map,
    _build_reply_map,
    _cache_message_senders,
    _build_history_iter_kwargs,
    _resolve_sender_entity,
    fetch_messages_for_topic,
)
from .models import (
    CapabilityNavigation,
    DialogResolver,
    DialogTargetFailure,
    ExactTargetHints,
    ForumTopicFailure,
    HistoryReadCapabilityResult,
    HistoryReadExecution,
    MessageLike,
    MessageReadFailure,
    NavigationFailure,
    ResolvedForumTopic,
    TopicCatalog,
    TopicFetcher,
    TopicLoader,
    TopicMetadata,
    TopicRefresher,
)
from .pagination import encode_history_navigation


async def execute_history_read_capability(
    client: TelegramClient,
    *,
    cache: EntityCache,
    dialog_query: str | None,
    limit: int,
    navigation: str | None,
    sender_query: str | None,
    topic_query: str | None,
    unread: bool,
    retry_tool: str,
    resolve_dialog: DialogResolver,
    reaction_names_threshold: int,
    load_topics: TopicLoader | None = None,
    fetch_topic_messages_fn: TopicFetcher | None = None,
    refresh_topic_by_id_fn: TopicRefresher | None = None,
    exact: ExactTargetHints | None = None,
) -> HistoryReadCapabilityResult:
    """Read message history from one dialog, with optional topic/sender/unread filters.

    Returns ``HistoryReadExecution`` on success, or one of
    ``DialogTargetFailure``, ``ForumTopicFailure``, ``MessageReadFailure``,
    ``NavigationFailure`` on expected errors (never raises — callers
    pattern-match on the return type).

    When *exact* is provided, its ``dialog_id`` / ``topic_id`` bypass fuzzy
    resolution.  ``fetched_messages`` holds the raw API result; ``messages``
    holds the (possibly sender-filtered) subset used for cursor generation.
    """
    exact_dialog_id = exact.dialog_id if exact else None
    exact_dialog_name = exact.dialog_name if exact else None
    exact_topic_id = exact.topic_id if exact else None
    exact_topic_name = exact.topic_name if exact else None
    exact_topic_metadata = exact.topic_metadata if exact else None

    dialog_target = await resolve_dialog_target(
        cache=cache,
        query=dialog_query,
        retry_tool=retry_tool,
        resolve_dialog=resolve_dialog,
        exact_dialog_id=exact_dialog_id,
        exact_dialog_name=exact_dialog_name,
    )
    if isinstance(dialog_target, DialogTargetFailure):
        return dialog_target

    sender_entity_id = _resolve_sender_entity(
        cache=cache,
        sender_query=sender_query,
        retry_tool=retry_tool,
    )
    if isinstance(sender_entity_id, MessageReadFailure):
        return sender_entity_id

    topic_metadata: TopicMetadata | None = None
    topic_cache: TopicMetadataCache | None = None
    topic_catalog: TopicCatalog | None = None
    topic_name: str | None = None
    topic_reply_to_message_id: int | None = None
    filter_sender_after_fetch = False
    entity_id = dialog_target.entity_id

    if topic_query or exact_topic_id is not None or exact_topic_metadata is not None:
        topic_cache = TopicMetadataCache(cache._conn)
        topic_capability = await load_forum_topic_capability(
            client,
            entity=entity_id,
            dialog_id=entity_id,
            dialog_name=dialog_target.display_name,
            topic_cache=topic_cache,
            requested_topic=topic_query,
            retry_tool=retry_tool,
            load_topics=load_topics,
            exact_topic_id=exact_topic_id,
            exact_topic_name=exact_topic_name,
            exact_topic_metadata=exact_topic_metadata,
            refresh_topic_by_id_fn=refresh_topic_by_id_fn,
        )
        if isinstance(topic_capability, ForumTopicFailure):
            return topic_capability
        if isinstance(topic_capability, ResolvedForumTopic):
            topic_catalog = topic_capability.topic_catalog
            topic_metadata = topic_capability.metadata
            topic_name = topic_capability.display_name
            topic_reply_to_message_id = topic_capability.reply_to_message_id
            filter_sender_after_fetch = (
                topic_reply_to_message_id is not None
                and isinstance(sender_entity_id, int)
            )

    iter_kwargs = _build_history_iter_kwargs(
        entity_id=dialog_target.entity_id,
        limit=limit,
        navigation=navigation,
        topic_id=int(topic_metadata["topic_id"]) if topic_metadata is not None else None,
        retry_tool=retry_tool,
    )
    if isinstance(iter_kwargs, (MessageReadFailure, NavigationFailure)):
        return iter_kwargs

    if topic_metadata is not None and topic_reply_to_message_id is not None:
        iter_kwargs["reply_to"] = topic_reply_to_message_id

    if isinstance(sender_entity_id, int) and not filter_sender_after_fetch:
        iter_kwargs["from_user"] = sender_entity_id

    if unread:
        input_peer = await client.get_input_entity(entity_id)
        peer_result = await client(GetPeerDialogsRequest(peers=[input_peer]))
        iter_kwargs["min_id"] = peer_result.dialogs[0].read_inbox_max_id

    # Cache-first read (CACHE-03, CACHE-05, BYP-01, BYP-02)
    msg_cache = MessageCache(cache._conn)
    topic_id_for_cache = int(topic_metadata["topic_id"]) if topic_metadata is not None else None

    cache_direction = HistoryDirection.OLDEST if iter_kwargs.get("reverse") else HistoryDirection.NEWEST
    cache_anchor_id: int | None = None
    if cache_direction == HistoryDirection.NEWEST:
        max_id = iter_kwargs.get("max_id")
        cache_anchor_id = int(max_id) if isinstance(max_id, int) else None
    else:
        min_id = iter_kwargs.get("min_id")
        # min_id=1 is the "from beginning" sentinel set by _build_history_iter_kwargs
        # when no cursor is provided — treat as no anchor so the query starts at ID 1
        if isinstance(min_id, int) and min_id > 1:
            cache_anchor_id = int(min_id)
        else:
            cache_anchor_id = None

    cached_page: list[MessageLike] | None = None
    if _should_try_cache(navigation, unread=unread):
        cached_page = msg_cache.try_read_page(  # type: ignore[assignment]
            entity_id,
            topic_id=topic_id_for_cache,
            anchor_id=cache_anchor_id,
            limit=limit,
            direction=cache_direction,
        )

    if cached_page is not None:
        raw_messages: list[MessageLike] = cached_page
    else:
        try:
            use_topic_scoped_fetch = (
                topic_metadata is not None
                and (
                    unread
                    or (
                        not bool(topic_metadata["is_general"])
                        and "reply_to" in iter_kwargs
                    )
                )
            )
            if use_topic_scoped_fetch and topic_metadata is not None and topic_cache is not None:
                fetched_messages, topic_metadata, iter_kwargs = await fetch_messages_for_topic(
                    client,
                    entity_id=entity_id,
                    iter_kwargs=iter_kwargs,
                    topic_metadata=topic_metadata,
                    topic_cache=topic_cache,
                    allow_headerless_messages=bool(topic_metadata["is_general"]) or not unread,
                    fetch_topic_messages_fn=fetch_topic_messages_fn,
                    refresh_topic_by_id_fn=refresh_topic_by_id_fn,
                )
                if bool(topic_metadata["is_deleted"]):
                    deleted_name = topic_name or topic_query or "Topic"
                    return MessageReadFailure(
                        kind="deleted",
                        text=deleted_topic_text(deleted_name, retry_tool=retry_tool),
                    )
                raw_messages = [] if fetched_messages is None else fetched_messages
                topic_cache.clear_topic_inaccessible(entity_id, int(topic_metadata["topic_id"]))
            else:
                raw_messages = [msg async for msg in client.iter_messages(**iter_kwargs)]
        except RPCError as exc:
            if topic_metadata is not None and topic_cache is not None:
                detail = rpc_error_detail(exc)
                topic_cache.mark_topic_inaccessible(
                    entity_id,
                    int(topic_metadata["topic_id"]),
                    detail,
                )
                topic_metadata["inaccessible_error"] = detail
                topic_label = topic_name or topic_query or f'Topic {int(topic_metadata["topic_id"])}'
                return MessageReadFailure(
                    kind="inaccessible",
                    text=inaccessible_topic_text(
                        topic_label,
                        exc,
                        resolved=True,
                        retry_tool=retry_tool,
                    ),
                )
            raise

        # Cache population: store every API fetch result (CACHE-05)
        msg_cache.store_messages(entity_id, raw_messages)

    if filter_sender_after_fetch and isinstance(sender_entity_id, int):
        messages = [
            msg
            for msg in raw_messages
            if getattr(msg, "sender_id", None) == sender_entity_id
        ]
    else:
        messages = raw_messages

    _cache_message_senders(
        cache=cache,
        messages=raw_messages,
    )
    reply_map = await _build_reply_map(
        client,
        entity_id=entity_id,
        messages=messages,
        msg_cache=msg_cache,
    )
    reaction_names_map = await _build_reaction_names_map(
        client,
        cache=cache,
        entity_id=entity_id,
        messages=messages,
        reaction_names_threshold=reaction_names_threshold,
    )

    topic_name_getter = None
    if topic_metadata is None:
        try:
            topic_name_getter = await _build_cross_topic_name_getter(
                client,
                cache=cache,
                entity_id=entity_id,
                dialog_name=dialog_target.display_name,
                fetched_messages=raw_messages,
                topic_catalog=topic_catalog,
                topic_cache=topic_cache,
                load_topics=load_topics,
            )
        except RPCError:
            import logging
            logging.getLogger(__name__).debug(
                "cross_topic_label_build_failed entity_id=%r", entity_id, exc_info=True,
            )
            topic_name_getter = None

    cursor_source_messages = raw_messages if filter_sender_after_fetch else messages
    navigation_result: CapabilityNavigation | None = None
    if len(cursor_source_messages) == limit and cursor_source_messages:
        last_message_id = getattr(cursor_source_messages[-1], "id", None)
        if isinstance(last_message_id, int):
            history_direction: HistoryDirection = (
                HistoryDirection.OLDEST
                if bool(iter_kwargs.get("reverse", False))
                else HistoryDirection.NEWEST
            )
            navigation_result = CapabilityNavigation(
                kind="history",
                token=encode_history_navigation(
                    last_message_id,
                    entity_id,
                    topic_id=int(topic_metadata["topic_id"]) if topic_metadata is not None else None,
                    direction=history_direction,
                ),
            )

    return HistoryReadExecution(
        entity_id=entity_id,
        resolve_prefix=dialog_target.resolve_prefix,
        topic_name=topic_name,
        messages=tuple(messages),
        fetched_messages=tuple(raw_messages),
        reply_map=reply_map,
        reaction_names_map=reaction_names_map,
        topic_name_getter=topic_name_getter,
        navigation=navigation_result,
    )
