from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from telethon import functions  # type: ignore[import-untyped]
from telethon.errors import RPCError  # type: ignore[import-untyped]

if TYPE_CHECKING:
    from telethon import TelegramClient  # type: ignore[import-untyped]

from .cache import TopicMetadataCache
from .errors import (
    ambiguous_deleted_topic_text,
    ambiguous_topic_text,
    deleted_topic_text,
    dialog_topics_unavailable_text,
    inaccessible_topic_text,
    rpc_error_detail,
    topic_not_found_text,
)
from .models import (
    MessageLike,
    FORUM_TOPICS_PAGE_SIZE,
    GENERAL_TOPIC_ID,
    GENERAL_TOPIC_TITLE,
    TOPIC_METADATA_TTL_SECONDS,
    ForumTopicCapabilityResult,
    ForumTopicFailure,
    ResolvedForumTopic,
    TopicCatalog,
    TopicLoader,
    TopicMatch,
    TopicMetadata,
    TopicRefresher,
)
from .resolver import Candidates, NotFound, Resolved, resolve


def build_get_forum_topics_request(
    *,
    entity: object,
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


def build_get_forum_topics_by_id_request(*, entity: object, topic_ids: list[int]) -> object:
    """Build a by-ID forum-topics request across Telethon API variants."""
    request_cls = getattr(functions.messages, "GetForumTopicsByIDRequest", None)
    if request_cls is not None:
        return request_cls(peer=entity, topics=topic_ids)

    return functions.channels.GetForumTopicsByIDRequest(
        channel=entity,
        topics=topic_ids,
    )


def topic_status(topic: TopicMetadata) -> str:
    """Return one short topic status label for listings."""
    if bool(topic["is_deleted"]):
        return "deleted"
    if bool(topic["is_general"]):
        return "general"
    if topic.get("inaccessible_error"):
        return "previously_inaccessible"
    return "active"


def topic_row_text(topic: TopicMetadata) -> str:
    """Return one stable topic row for ListTopics output."""
    line = (
        f'topic_id={topic["topic_id"]} '
        f'title="{topic["title"]}" '
        f'top_message_id={topic["top_message_id"]} '
        f'status={topic_status(topic)}'
    )
    if topic.get("inaccessible_error"):
        line += f' last_error={topic["inaccessible_error"]}'
    return line


def append_topic_match_metadata(
    match: dict[str, object],
    metadata_by_id: dict[int, TopicMetadata],
) -> str:
    """Return one topic match line enriched with cached metadata."""
    line = f'id={match["entity_id"]} name="{match["display_name"]}" score={match["score"]}'
    entity_id = int(match["entity_id"])  # type: ignore[call-overload]
    topic = metadata_by_id.get(entity_id)
    if topic is None:
        return line

    line += f' status={topic_status(topic)}'
    if topic["top_message_id"] is not None:
        line += f' top_message_id={topic["top_message_id"]}'
    if topic.get("inaccessible_error"):
        line += f' last_error={topic["inaccessible_error"]}'
    return line


def topic_empty_state_text(*, unread: bool) -> str:
    """Return the empty-state body for one ListMessages response."""
    if unread:
        return "no unread messages"
    return ""


def normalize_topic_metadata(
    topic: object,
    *,
    existing_topic: TopicMetadata | None = None,
) -> TopicMetadata:
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
    normalized_topic: TopicMetadata = {
        "topic_id": topic_id,
        "title": GENERAL_TOPIC_TITLE if is_general else title,
        "top_message_id": top_message_id,
        "is_general": is_general,
        "is_deleted": raw_title is None,
    }
    if existing_topic is not None and existing_topic.get("inaccessible_error") is not None:
        normalized_topic["inaccessible_error"] = existing_topic["inaccessible_error"]
    if existing_topic is not None and existing_topic.get("inaccessible_at") is not None:
        normalized_topic["inaccessible_at"] = existing_topic["inaccessible_at"]
    return normalized_topic


def with_general_topic(topics: list[TopicMetadata]) -> list[TopicMetadata]:
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


def build_topic_catalog(topics: list[TopicMetadata]) -> TopicCatalog:
    """Return one topic catalog payload from normalized metadata rows."""
    metadata_by_id = {int(topic["topic_id"]): topic for topic in with_general_topic(topics)}
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


async def fetch_forum_topics_page(
    client: TelegramClient,
    *,
    entity: object,
    offset_date: object | None = None,
    offset_id: int = 0,
    offset_topic: int = 0,
    limit: int = FORUM_TOPICS_PAGE_SIZE,
) -> tuple[list[object], int]:
    """Fetch one raw page of forum topics using Telegram's forum-topics RPC."""
    response = await client(
        build_get_forum_topics_request(
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


async def fetch_all_forum_topics(
    client: TelegramClient,
    *,
    entity: object,
    page_size: int = FORUM_TOPICS_PAGE_SIZE,
) -> list[TopicMetadata]:
    """Fetch and normalize all forum topics for one dialog via raw paginated RPC calls."""
    offset_date: object | None = None
    offset_id = 0
    offset_topic = 0
    topics_by_id: dict[int, TopicMetadata] = {}
    seen_server_topic_ids: set[int] = set()
    total_count: int | None = None

    while True:
        page_topics, page_count = await fetch_forum_topics_page(
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
            normalized_topic = normalize_topic_metadata(topic)
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

    return with_general_topic(list(topics_by_id.values()))


async def refresh_topic_by_id(
    client: TelegramClient,
    *,
    entity: object,
    dialog_id: int,
    topic_id: int,
    topic_cache: TopicMetadataCache,
    ttl_seconds: int = TOPIC_METADATA_TTL_SECONDS,
) -> TopicMetadata | None:
    """Refresh one topic by ID and persist tombstones when Telegram reports deletion."""
    existing_topic = topic_cache.get_topic(
        dialog_id,
        topic_id,
        ttl_seconds,
        allow_stale=True,
    )
    response = await client(
        build_get_forum_topics_by_id_request(entity=entity, topic_ids=[topic_id])
    )
    response_topics = list(getattr(response, "topics", []))
    if not response_topics:
        return None

    topic = normalize_topic_metadata(response_topics[0], existing_topic=existing_topic)
    topic_cache.upsert_topics(dialog_id, [topic])
    return topic


async def load_dialog_topics(
    client: TelegramClient,
    *,
    entity: object,
    dialog_id: int,
    topic_cache: TopicMetadataCache,
    ttl_seconds: int = TOPIC_METADATA_TTL_SECONDS,
) -> TopicCatalog:
    """Load one dialog's topic catalog from cache or Telegram and keep tombstones available."""
    cached_topics = topic_cache.get_dialog_topics(
        dialog_id,
        ttl_seconds,
        include_deleted=True,
    )
    if cached_topics is None:
        cached_topics = await fetch_all_forum_topics(client, entity=entity)
        if cached_topics:
            topic_cache.upsert_topics(dialog_id, cached_topics)
    else:
        normalized_topics = with_general_topic(cached_topics)
        if len(normalized_topics) != len(cached_topics):
            topic_cache.upsert_topics(dialog_id, normalized_topics)
        cached_topics = normalized_topics

    return build_topic_catalog(cached_topics)


def resolve_deleted_topic(
    requested_topic: str,
    deleted_topics: dict[int, TopicMetadata],
) -> Resolved | Candidates | None:
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


def resolve_forum_topic(
    *,
    requested_topic: str,
    topic_catalog: TopicCatalog,
    retry_tool: str,
) -> ResolvedForumTopic | ForumTopicFailure:
    """Resolve one requested topic against one loaded topic catalog."""
    topic_result = resolve(requested_topic, topic_catalog["choices"])
    if isinstance(topic_result, NotFound):
        deleted_result = resolve_deleted_topic(requested_topic, topic_catalog["deleted_topics"])
        if isinstance(deleted_result, Candidates):
            matches = tuple(
                TopicMatch(
                    entity_id=int(match["entity_id"]),
                    display_name=str(match["display_name"]),
                    score=int(match["score"]),
                    status="deleted",
                )
                for match in deleted_result.matches
            )
            match_lines = [
                f'id={match.entity_id} name="{match.display_name}" score={match.score} [deleted]'
                for match in matches
            ]
            return ForumTopicFailure(
                kind="deleted_ambiguous",
                query=requested_topic,
                text=ambiguous_deleted_topic_text(
                    requested_topic,
                    match_lines,
                    retry_tool=retry_tool,
                ),
                matches=matches,
                topic_catalog=topic_catalog,
            )
        if deleted_result is not None:
            return ForumTopicFailure(
                kind="deleted",
                query=requested_topic,
                text=deleted_topic_text(deleted_result.display_name, retry_tool=retry_tool),
                topic_catalog=topic_catalog,
            )
        return ForumTopicFailure(
            kind="not_found",
            query=requested_topic,
            text=topic_not_found_text(requested_topic, retry_tool=retry_tool),
            topic_catalog=topic_catalog,
        )

    if isinstance(topic_result, Candidates):
        candidate_matches: list[TopicMatch] = []
        match_lines = []
        for match in topic_result.matches:
            entity_id = int(match["entity_id"])  # type: ignore[call-overload]
            topic = topic_catalog["metadata_by_id"].get(entity_id)
            topic_match = TopicMatch(
                entity_id=entity_id,
                display_name=str(match["display_name"]),
                score=int(match["score"]),
                status=topic_status(topic) if topic is not None else None,
                top_message_id=int(topic["top_message_id"]) if topic is not None and topic["top_message_id"] is not None else None,
                last_error=str(topic["inaccessible_error"]) if topic is not None and topic.get("inaccessible_error") else None,
            )
            candidate_matches.append(topic_match)
            match_lines.append(append_topic_match_metadata(match, topic_catalog["metadata_by_id"]))
        return ForumTopicFailure(
            kind="ambiguous",
            query=requested_topic,
            text=ambiguous_topic_text(requested_topic, match_lines, retry_tool=retry_tool),
            matches=tuple(candidate_matches),
            topic_catalog=topic_catalog,
        )

    topic_metadata = topic_catalog["metadata_by_id"].get(topic_result.entity_id)
    if topic_metadata is None:
        return ForumTopicFailure(
            kind="not_found",
            query=requested_topic,
            text=topic_not_found_text(requested_topic, retry_tool=retry_tool),
            topic_catalog=topic_catalog,
        )
    if bool(topic_metadata["is_deleted"]):
        return ForumTopicFailure(
            kind="deleted",
            query=requested_topic,
            text=deleted_topic_text(topic_result.display_name, retry_tool=retry_tool),
            topic_catalog=topic_catalog,
        )

    reply_to_message_id: int | None = None
    if not bool(topic_metadata["is_general"]):
        top_message_id = topic_metadata["top_message_id"]
        if top_message_id is not None:
            reply_to_message_id = int(top_message_id)

    return ResolvedForumTopic(
        query=requested_topic,
        display_name=topic_result.display_name,
        metadata=topic_metadata,
        topic_catalog=topic_catalog,
        reply_to_message_id=reply_to_message_id,
    )


async def resolve_exact_topic_target(
    client: TelegramClient,
    *,
    entity: object,
    dialog_id: int,
    topic_cache: TopicMetadataCache,
    retry_tool: str,
    exact_topic_id: int | None = None,
    exact_topic_name: str | None = None,
    exact_topic_metadata: TopicMetadata | None = None,
    ttl_seconds: int = TOPIC_METADATA_TTL_SECONDS,
    refresh_topic_by_id_fn: TopicRefresher | None = None,
) -> ResolvedForumTopic | ForumTopicFailure:
    """Resolve one exact topic target from metadata, cache, or one by-ID refresh."""
    topic_metadata = exact_topic_metadata
    topic_id = exact_topic_id
    if topic_id is None and topic_metadata is not None:
        topic_id = int(topic_metadata["topic_id"])
    if topic_id is None:
        raise ValueError("exact_topic_id or exact_topic_metadata is required")

    if topic_metadata is None:
        cached_topic = topic_cache.get_topic(dialog_id, topic_id, ttl_seconds)
        if cached_topic is not None:
            topic_metadata = cached_topic
        else:
            active_refresh_topic_by_id = (
                refresh_topic_by_id_fn
                if refresh_topic_by_id_fn is not None
                else refresh_topic_by_id
            )
            topic_metadata = await active_refresh_topic_by_id(
                client,
                entity=entity,
                dialog_id=dialog_id,
                topic_id=topic_id,
                topic_cache=topic_cache,
                ttl_seconds=ttl_seconds,
            )

    if topic_metadata is None:
        topic_label = exact_topic_name or f"Topic {topic_id}"
        return ForumTopicFailure(
            kind="not_found",
            query=str(topic_id),
            text=topic_not_found_text(topic_label, retry_tool=retry_tool),
            topic_catalog={
                "choices": {},
                "metadata_by_id": {},
                "deleted_topics": {},
            },
        )

    display_name = exact_topic_name or str(topic_metadata["title"])
    topic_catalog = build_topic_catalog([topic_metadata])
    if bool(topic_metadata["is_deleted"]):
        return ForumTopicFailure(
            kind="deleted",
            query=str(topic_id),
            text=deleted_topic_text(display_name, retry_tool=retry_tool),
            topic_catalog=topic_catalog,
        )

    reply_to_message_id: int | None = None
    if not bool(topic_metadata["is_general"]):
        top_message_id = topic_metadata["top_message_id"]
        if top_message_id is not None:
            reply_to_message_id = int(top_message_id)

    return ResolvedForumTopic(
        query=str(topic_id),
        display_name=display_name,
        metadata=topic_metadata,
        topic_catalog=topic_catalog,
        reply_to_message_id=reply_to_message_id,
    )


async def load_forum_topic_capability(
    client: TelegramClient,
    *,
    entity: object,
    dialog_id: int,
    dialog_name: str,
    topic_cache: TopicMetadataCache,
    requested_topic: str | None,
    retry_tool: str,
    ttl_seconds: int = TOPIC_METADATA_TTL_SECONDS,
    load_topics: TopicLoader | None = None,
    exact_topic_id: int | None = None,
    exact_topic_name: str | None = None,
    exact_topic_metadata: TopicMetadata | None = None,
    refresh_topic_by_id_fn: TopicRefresher | None = None,
) -> ForumTopicCapabilityResult:
    """Load one dialog's topic capability result for listing or topic-scoped reads."""
    if exact_topic_id is not None or exact_topic_metadata is not None:
        return await resolve_exact_topic_target(
            client,
            entity=entity,
            dialog_id=dialog_id,
            topic_cache=topic_cache,
            retry_tool=retry_tool,
            exact_topic_id=exact_topic_id,
            exact_topic_name=exact_topic_name or requested_topic,
            exact_topic_metadata=exact_topic_metadata,
            ttl_seconds=ttl_seconds,
            refresh_topic_by_id_fn=refresh_topic_by_id_fn,
        )

    active_loader = load_topics if load_topics is not None else load_dialog_topics
    try:
        topic_catalog = await active_loader(
            client,
            entity=entity,
            dialog_id=dialog_id,
            topic_cache=topic_cache,
            ttl_seconds=ttl_seconds,
        )
    except RPCError as exc:
        if requested_topic is None:
            return ForumTopicFailure(
                kind="catalog_unavailable",
                query=dialog_name,
                text=dialog_topics_unavailable_text(dialog_name, exc),
            )
        return ForumTopicFailure(
            kind="inaccessible",
            query=requested_topic,
            text=inaccessible_topic_text(
                requested_topic,
                exc,
                resolved=False,
                retry_tool=retry_tool,
            ),
        )

    if requested_topic is None:
        return topic_catalog
    return resolve_forum_topic(
        requested_topic=requested_topic,
        topic_catalog=topic_catalog,
        retry_tool=retry_tool,
    )


def forum_topic_anchor_id(msg: object) -> int | None:
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


def messages_need_forum_topic_labels(messages: list[MessageLike]) -> bool:
    """Return True when a mixed message page appears to come from a forum dialog."""
    return any(forum_topic_anchor_id(msg) is not None for msg in messages)


def build_topic_name_getter(
    topic_catalog: TopicCatalog,
) -> Callable[[object], str | None]:
    """Build a formatter callback that labels cross-topic forum messages."""
    topic_name_by_anchor: dict[int, str] = {}
    for topic in topic_catalog["metadata_by_id"].values():
        if bool(topic["is_deleted"]):
            continue

        topic_name_by_anchor[int(topic["topic_id"])] = str(topic["title"])
        top_message_id = topic["top_message_id"]
        if top_message_id is not None:
            topic_name_by_anchor[int(top_message_id)] = str(topic["title"])

    def topic_name_for_message(msg: object) -> str | None:
        anchor_id = forum_topic_anchor_id(msg)
        if anchor_id is None:
            return GENERAL_TOPIC_TITLE
        return topic_name_by_anchor.get(anchor_id)

    return topic_name_for_message


def is_topic_id_invalid_error(exc: RPCError) -> bool:
    """Return True when Telegram reports the cached thread anchor is invalid."""
    return "TOPIC_ID_INVALID" in rpc_error_detail(exc).upper()


def message_matches_topic(
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


async def fetch_topic_messages(
    client: TelegramClient,
    *,
    iter_kwargs: dict[str, object],
    topic_metadata: TopicMetadata,
    allow_headerless_messages: bool,
) -> list[MessageLike]:
    """Fetch a topic page and strip any leaked adjacent-topic messages."""
    requested_limit = int(iter_kwargs.get("limit", 0) or 0)  # type: ignore[call-overload]
    if requested_limit <= 0:
        return []

    topic_id = int(topic_metadata["topic_id"])
    raw_top_message_id = topic_metadata["top_message_id"]
    top_message_id = int(raw_top_message_id) if raw_top_message_id is not None else None
    is_general = bool(topic_metadata["is_general"])

    batch_kwargs = dict(iter_kwargs)
    batch_limit = requested_limit
    boundary_key = "min_id" if bool(batch_kwargs.get("reverse")) else "max_id"
    topic_messages: list[MessageLike] = []

    while len(topic_messages) < requested_limit:
        raw_messages = [msg async for msg in client.iter_messages(**batch_kwargs)]
        if not raw_messages:
            break

        for msg in raw_messages:
            if message_matches_topic(
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
