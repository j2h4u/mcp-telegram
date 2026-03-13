from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable, Literal, TypedDict

from telethon import functions
from telethon.errors import RPCError

from .cache import EntityCache, TopicMetadataCache
from .resolver import Candidates, NotFound, Resolved, resolve

FORUM_TOPICS_PAGE_SIZE = 100
TOPIC_METADATA_TTL_SECONDS = 600
GENERAL_TOPIC_ID = 1
GENERAL_TOPIC_TITLE = "General"


class TopicMetadata(TypedDict, total=False):
    topic_id: int
    title: str
    top_message_id: int | None
    is_general: bool
    is_deleted: bool
    inaccessible_error: str | None
    inaccessible_at: int | None


class TopicCatalog(TypedDict):
    choices: dict[int, str]
    metadata_by_id: dict[int, TopicMetadata]
    deleted_topics: dict[int, TopicMetadata]


@dataclass(frozen=True)
class DialogMatch:
    entity_id: int
    display_name: str
    score: int
    username: str | None = None
    entity_type: str | None = None


@dataclass(frozen=True)
class DialogTargetFailure:
    kind: Literal["not_found", "ambiguous"]
    query: str
    text: str
    matches: tuple[DialogMatch, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ResolvedDialogTarget:
    entity_id: int
    query: str
    display_name: str
    resolve_prefix: str


@dataclass(frozen=True)
class TopicMatch:
    entity_id: int
    display_name: str
    score: int
    status: str | None = None
    top_message_id: int | None = None
    last_error: str | None = None


@dataclass(frozen=True)
class ForumTopicFailure:
    kind: Literal[
        "catalog_unavailable",
        "inaccessible",
        "not_found",
        "ambiguous",
        "deleted",
        "deleted_ambiguous",
    ]
    query: str
    text: str
    matches: tuple[TopicMatch, ...] = field(default_factory=tuple)
    topic_catalog: TopicCatalog | None = None


@dataclass(frozen=True)
class ResolvedForumTopic:
    query: str
    display_name: str
    metadata: TopicMetadata
    topic_catalog: TopicCatalog
    reply_to_message_id: int | None


DialogResolveResult = Resolved | Candidates | NotFound
DialogTargetResult = ResolvedDialogTarget | DialogTargetFailure
ForumTopicCapabilityResult = TopicCatalog | ResolvedForumTopic | ForumTopicFailure
DialogResolver = Callable[[EntityCache, str], Awaitable[DialogResolveResult]]


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


def action_text(summary: str, action: str) -> str:
    """Return a short action-oriented response body."""
    return f"{summary}\nAction: {action}"


def dialog_not_found_text(dialog_name: str, *, retry_tool: str) -> str:
    """Return an action-oriented response for missing dialogs."""
    return action_text(
        f'Dialog "{dialog_name}" was not found.',
        f"Call ListDialogs, then retry {retry_tool} with dialog set to an exact dialog id, @username, or full dialog name.",
    )


def ambiguous_dialog_text(dialog_name: str, match_lines: list[str], *, retry_tool: str) -> str:
    """Return an action-oriented response for ambiguous dialogs."""
    matches = "\n".join(match_lines)
    return (
        f'Dialog "{dialog_name}" matched multiple dialogs.\n'
        f"Action: Retry {retry_tool} with dialog set to one of the numeric ids from the matches below.\n"
        f"{matches}"
    )


def deleted_topic_text(topic_name: str, *, retry_tool: str) -> str:
    """Return the explicit user-facing message for deleted topics."""
    return action_text(
        f'Topic "{topic_name}" was deleted and can no longer be fetched.',
        f"Call ListTopics for this dialog, then retry {retry_tool} with an active topic title, or omit topic to read across all topics.",
    )


def rpc_error_detail(exc: RPCError) -> str:
    """Return the stable Telegram RPC detail for one exception."""
    detail = getattr(exc, "message", None) or str(exc)
    return str(detail)


def inaccessible_topic_text(topic_name: str, exc: RPCError, *, resolved: bool, retry_tool: str) -> str:
    """Return a readable user-facing message for inaccessible topics."""
    detail = rpc_error_detail(exc)
    if resolved:
        return action_text(
            f'Topic "{topic_name}" resolved, but Telegram rejected thread fetch ({detail}).',
            f"Retry {retry_tool} without topic to read dialog-wide messages, or call ListTopics and choose another active topic.",
        )

    return action_text(
        f'Topic "{topic_name}" could not be loaded because Telegram rejected topic access ({detail}).',
        f"Call ListTopics for this dialog, then retry {retry_tool} with an exact active topic title, or omit topic.",
    )


def topic_not_found_text(topic_name: str, *, retry_tool: str) -> str:
    """Return an action-oriented response for missing topics."""
    return action_text(
        f'Topic "{topic_name}" was not found.',
        f"Call ListTopics for this dialog, then retry {retry_tool} with an exact topic title.",
    )


def ambiguous_topic_text(topic_name: str, match_lines: list[str], *, retry_tool: str) -> str:
    """Return an action-oriented response for ambiguous topics."""
    matches = "\n".join(match_lines)
    return (
        f'Topic "{topic_name}" matched multiple topics.\n'
        f"Action: Retry {retry_tool} with topic set to one exact topic title from the matches below.\n"
        f"{matches}"
    )


def ambiguous_deleted_topic_text(topic_name: str, match_lines: list[str], *, retry_tool: str) -> str:
    """Return an action-oriented response for ambiguous deleted topics."""
    matches = "\n".join(match_lines)
    return (
        f'Deleted topic query "{topic_name}" matched multiple deleted topics.\n'
        f"Action: Call ListTopics for this dialog, then retry {retry_tool} with an active topic title instead of a deleted one.\n"
        f"{matches}"
    )


def dialog_topics_unavailable_text(dialog_name: str, exc: RPCError) -> str:
    """Return a readable message when one dialog cannot expose a topic catalog."""
    detail = rpc_error_detail(exc)
    return action_text(
        f'Dialog "{dialog_name}" does not expose a readable forum-topic catalog ({detail}).',
        "Do not use ListTopics for this dialog. Retry ListMessages without topic if you want dialog messages, or choose another forum-enabled dialog.",
    )


def no_active_topics_text(dialog_name: str) -> str:
    """Return an action-oriented response for dialogs without active topics."""
    return action_text(
        f'No active forum topics found for "{dialog_name}".',
        "Retry ListMessages without topic to read dialog-wide messages, or choose another forum-enabled dialog.",
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
    entity_id = int(match["entity_id"])
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


def messages_need_forum_topic_labels(messages: list[object]) -> bool:
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


async def fetch_forum_topics_page(
    client: object,
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
    client: object,
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
    client: object,
    *,
    entity: object,
    dialog_id: int,
    topic_id: int,
    topic_cache: TopicMetadataCache,
    ttl_seconds: int = TOPIC_METADATA_TTL_SECONDS,
) -> TopicMetadata | None:
    """Refresh one topic by ID and persist tombstones when Telegram reports deletion."""
    existing_topic = topic_cache.get_topic(dialog_id, topic_id, ttl_seconds)
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
    client: object,
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


def _dialog_match_from_dict(match: dict[str, object]) -> DialogMatch:
    return DialogMatch(
        entity_id=int(match["entity_id"]),
        display_name=str(match["display_name"]),
        score=int(match["score"]),
        username=str(match["username"]) if match.get("username") else None,
        entity_type=str(match["entity_type"]) if match.get("entity_type") else None,
    )


def _dialog_match_line(match: DialogMatch) -> str:
    line = f'id={match.entity_id} name="{match.display_name}" score={match.score}'
    if match.username:
        line += f" @{match.username}"
    if match.entity_type:
        line += f" [{match.entity_type}]"
    return line


async def resolve_dialog_target(
    *,
    cache: EntityCache,
    query: str,
    retry_tool: str,
    resolve_dialog: DialogResolver,
) -> DialogTargetResult:
    """Resolve one dialog query into an inspectable target or actionable failure."""
    result = await resolve_dialog(cache, query)
    if isinstance(result, NotFound):
        return DialogTargetFailure(
            kind="not_found",
            query=query,
            text=dialog_not_found_text(query, retry_tool=retry_tool),
        )
    if isinstance(result, Candidates):
        matches = tuple(_dialog_match_from_dict(match) for match in result.matches)
        match_lines = [_dialog_match_line(match) for match in matches]
        return DialogTargetFailure(
            kind="ambiguous",
            query=query,
            text=ambiguous_dialog_text(query, match_lines, retry_tool=retry_tool),
            matches=matches,
        )

    resolve_prefix = (
        f'[resolved: "{query}" → {result.display_name}]\n'
        if query.strip().lower() != result.display_name.strip().lower()
        else ""
    )
    return ResolvedDialogTarget(
        entity_id=result.entity_id,
        query=query,
        display_name=result.display_name,
        resolve_prefix=resolve_prefix,
    )


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
        matches = []
        match_lines = []
        for match in topic_result.matches:
            entity_id = int(match["entity_id"])
            topic = topic_catalog["metadata_by_id"].get(entity_id)
            topic_match = TopicMatch(
                entity_id=entity_id,
                display_name=str(match["display_name"]),
                score=int(match["score"]),
                status=topic_status(topic) if topic is not None else None,
                top_message_id=int(topic["top_message_id"]) if topic is not None and topic["top_message_id"] is not None else None,
                last_error=str(topic["inaccessible_error"]) if topic is not None and topic.get("inaccessible_error") else None,
            )
            matches.append(topic_match)
            match_lines.append(append_topic_match_metadata(match, topic_catalog["metadata_by_id"]))
        return ForumTopicFailure(
            kind="ambiguous",
            query=requested_topic,
            text=ambiguous_topic_text(requested_topic, match_lines, retry_tool=retry_tool),
            matches=tuple(matches),
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


async def load_forum_topic_capability(
    client: object,
    *,
    entity: object,
    dialog_id: int,
    dialog_name: str,
    topic_cache: TopicMetadataCache,
    requested_topic: str | None,
    retry_tool: str,
    ttl_seconds: int = TOPIC_METADATA_TTL_SECONDS,
) -> ForumTopicCapabilityResult:
    """Load one dialog's topic capability result for listing or topic-scoped reads."""
    try:
        topic_catalog = await load_dialog_topics(
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
    client: object,
    *,
    iter_kwargs: dict[str, object],
    topic_metadata: TopicMetadata,
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


async def fetch_messages_for_topic(
    client: object,
    *,
    entity_id: int,
    iter_kwargs: dict[str, object],
    topic_metadata: TopicMetadata,
    topic_cache: TopicMetadataCache,
    allow_headerless_messages: bool,
) -> tuple[list[object] | None, TopicMetadata, dict[str, object]]:
    """Fetch one topic page with one bounded by-ID refresh and retry on stale anchors."""
    active_iter_kwargs = dict(iter_kwargs)
    active_topic_metadata = topic_metadata
    original_exc: RPCError | None = None
    retry_invalid_exc: RPCError | None = None

    async def scan_dialog_history_for_topic() -> tuple[list[object], dict[str, object]]:
        """Fallback to dialog-wide history scanning when thread fetch rejects a valid topic anchor."""
        history_iter_kwargs = dict(active_iter_kwargs)
        history_iter_kwargs.pop("reply_to", None)
        messages = await fetch_topic_messages(
            client,
            iter_kwargs=history_iter_kwargs,
            topic_metadata=active_topic_metadata,
            allow_headerless_messages=False,
        )
        return messages, history_iter_kwargs

    try:
        messages = await fetch_topic_messages(
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

    refreshed_topic = await refresh_topic_by_id(
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
        messages = await fetch_topic_messages(
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
