from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable, Literal, TypedDict

from telethon import functions
from telethon.errors import RPCError
from telethon.tl.functions.messages import (
    GetMessageReactionsListRequest,
    GetPeerDialogsRequest,
)

from .cache import (
    GROUP_TTL,
    USER_TTL,
    EntityCache,
    ReactionMetadataCache,
    TopicMetadataCache,
)
from .pagination import (
    decode_history_navigation,
    decode_navigation_token,
    decode_search_navigation,
    encode_cursor,
    encode_history_navigation,
    encode_search_navigation,
)
from .resolver import Candidates, NotFound, Resolved, resolve

FORUM_TOPICS_PAGE_SIZE = 100
TOPIC_METADATA_TTL_SECONDS = 600
GENERAL_TOPIC_ID = 1
GENERAL_TOPIC_TITLE = "General"
HISTORY_NAVIGATION_NEWEST = "newest"
HISTORY_NAVIGATION_OLDEST = "oldest"
HistoryNavigationMode = Literal["newest", "oldest"]


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


@dataclass(frozen=True)
class MessageReadFailure:
    kind: Literal[
        "invalid_cursor",
        "sender_not_found",
        "sender_ambiguous",
        "deleted",
        "inaccessible",
    ]
    text: str


@dataclass(frozen=True)
class NavigationFailure:
    kind: Literal["invalid_navigation"]
    text: str


@dataclass(frozen=True)
class CapabilityNavigation:
    kind: Literal["history", "search"]
    token: str


@dataclass(frozen=True)
class ListTopicsExecution:
    resolve_prefix: str
    dialog_name: str
    active_topics: tuple[TopicMetadata, ...]


@dataclass(frozen=True)
class HistoryReadExecution:
    entity_id: int
    resolve_prefix: str
    topic_name: str | None
    messages: tuple[object, ...]
    fetched_messages: tuple[object, ...]
    reply_map: dict[int, object]
    reaction_names_map: dict[int, dict[str, list[str]]]
    topic_name_getter: Callable[[object], str | None] | None
    next_cursor: str | None
    navigation: CapabilityNavigation | None = None


@dataclass(frozen=True)
class SearchExecution:
    entity_id: int
    dialog_name: str
    resolve_prefix: str
    hits: tuple[object, ...]
    context_messages_by_id: dict[int, object]
    reaction_names_map: dict[int, dict[str, list[str]]]
    next_offset: int | None
    navigation: CapabilityNavigation | None = None


DialogResolveResult = Resolved | Candidates | NotFound
DialogTargetResult = ResolvedDialogTarget | DialogTargetFailure
ForumTopicCapabilityResult = TopicCatalog | ResolvedForumTopic | ForumTopicFailure
ListTopicsCapabilityResult = ListTopicsExecution | DialogTargetFailure | ForumTopicFailure
HistoryReadCapabilityResult = (
    HistoryReadExecution
    | DialogTargetFailure
    | ForumTopicFailure
    | MessageReadFailure
    | NavigationFailure
)
SearchCapabilityResult = SearchExecution | DialogTargetFailure | NavigationFailure
DialogResolver = Callable[[EntityCache, str], Awaitable[DialogResolveResult]]
TopicLoader = Callable[..., Awaitable[TopicCatalog]]
TopicFetcher = Callable[..., Awaitable[list[object]]]
TopicRefresher = Callable[..., Awaitable[TopicMetadata | None]]
SenderTypeGetter = Callable[[object], str]


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


def invalid_cursor_text(detail: str, *, retry_tool: str) -> str:
    """Return an action-oriented response for malformed cursors."""
    return action_text(
        f"Cursor is invalid: {detail}",
        f"Retry {retry_tool} without cursor to start from the first page, or reuse the exact next_cursor value from the previous {retry_tool} response.",
    )


def invalid_navigation_text(detail: str, *, retry_tool: str) -> str:
    """Return an action-oriented response for malformed shared navigation tokens."""
    return action_text(
        f"Navigation token is invalid: {detail}",
        f"Retry {retry_tool} without navigation to start from the first page, or reuse the exact next_navigation value from the previous {retry_tool} response.",
    )


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


def sender_not_found_text(sender_name: str, *, retry_tool: str) -> str:
    """Return an action-oriented response for missing senders."""
    return action_text(
        f'Sender "{sender_name}" was not found.',
        f"Retry {retry_tool} without sender, or use an exact sender name or @username that appears in this dialog.",
    )


def ambiguous_sender_text(sender_name: str, match_lines: list[str], *, retry_tool: str) -> str:
    """Return an action-oriented response for ambiguous senders."""
    matches = "\n".join(match_lines)
    return (
        f'Sender "{sender_name}" matched multiple users.\n'
        f"Action: Retry {retry_tool} with sender set to one exact match from the list below.\n"
        f"{matches}"
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
    load_topics: TopicLoader | None = None,
) -> ForumTopicCapabilityResult:
    """Load one dialog's topic capability result for listing or topic-scoped reads."""
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

    sender_result = resolve(sender_query, cache.all_names_with_ttl(USER_TTL, GROUP_TTL), cache)
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
    get_sender_type: SenderTypeGetter,
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


async def execute_list_topics_capability(
    client: object,
    *,
    cache: EntityCache,
    dialog_query: str,
    retry_tool: str,
    resolve_dialog: DialogResolver,
    load_topics: TopicLoader | None = None,
) -> ListTopicsCapabilityResult:
    dialog_target = await resolve_dialog_target(
        cache=cache,
        query=dialog_query,
        retry_tool=retry_tool,
        resolve_dialog=resolve_dialog,
    )
    if isinstance(dialog_target, DialogTargetFailure):
        return dialog_target

    topic_capability = await load_forum_topic_capability(
        client,
        entity=dialog_target.entity_id,
        dialog_id=dialog_target.entity_id,
        dialog_name=dialog_target.display_name,
        topic_cache=TopicMetadataCache(cache._conn),
        requested_topic=None,
        retry_tool=retry_tool,
        load_topics=load_topics,
    )
    if isinstance(topic_capability, ForumTopicFailure):
        return topic_capability

    active_topics = tuple(
        topic_capability["metadata_by_id"][topic_id]
        for topic_id in sorted(topic_capability["choices"])
    )
    return ListTopicsExecution(
        resolve_prefix=dialog_target.resolve_prefix,
        dialog_name=dialog_target.display_name,
        active_topics=active_topics,
    )


async def execute_history_read_capability(
    client: object,
    *,
    cache: EntityCache,
    dialog_query: str,
    limit: int,
    navigation: str | None,
    sender_query: str | None,
    topic_query: str | None,
    unread: bool,
    retry_tool: str,
    resolve_dialog: DialogResolver,
    get_sender_type: SenderTypeGetter,
    reaction_names_threshold: int,
    load_topics: TopicLoader | None = None,
    fetch_topic_messages_fn: TopicFetcher | None = None,
    refresh_topic_by_id_fn: TopicRefresher | None = None,
) -> HistoryReadCapabilityResult:
    dialog_target = await resolve_dialog_target(
        cache=cache,
        query=dialog_query,
        retry_tool=retry_tool,
        resolve_dialog=resolve_dialog,
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

    if topic_query:
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
            topic_metadata["inaccessible_error"] = None
            topic_metadata["inaccessible_at"] = None
        else:
            raw_messages = [msg async for msg in client.iter_messages(**iter_kwargs)]
    except RPCError as exc:
        if topic_query and topic_metadata is not None and topic_cache is not None:
            detail = rpc_error_detail(exc)
            topic_cache.mark_topic_inaccessible(
                entity_id,
                int(topic_metadata["topic_id"]),
                detail,
            )
            topic_metadata["inaccessible_error"] = detail
            return MessageReadFailure(
                kind="inaccessible",
                text=inaccessible_topic_text(
                    topic_name or topic_query,
                    exc,
                    resolved=True,
                    retry_tool=retry_tool,
                ),
            )
        raise

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
        get_sender_type=get_sender_type,
    )
    reply_map = await _build_reply_map(
        client,
        entity_id=entity_id,
        messages=messages,
    )
    reaction_names_map = await _build_reaction_names_map(
        client,
        cache=cache,
        entity_id=entity_id,
        messages=messages,
        reaction_names_threshold=reaction_names_threshold,
    )

    topic_name_getter: Callable[[object], str | None] | None = None
    if topic_query is None:
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
            topic_name_getter = None

    cursor_source_messages = raw_messages if filter_sender_after_fetch else messages
    next_cursor: str | None = None
    navigation: CapabilityNavigation | None = None
    if len(cursor_source_messages) == limit and cursor_source_messages:
        last_message_id = getattr(cursor_source_messages[-1], "id", None)
        if isinstance(last_message_id, int):
            history_direction: HistoryNavigationMode = (
                HISTORY_NAVIGATION_OLDEST
                if bool(iter_kwargs["reverse"])
                else HISTORY_NAVIGATION_NEWEST
            )
            next_cursor = encode_cursor(last_message_id, entity_id)
            navigation = CapabilityNavigation(
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
        next_cursor=next_cursor,
        navigation=navigation,
    )


async def execute_search_messages_capability(
    client: object,
    *,
    cache: EntityCache,
    dialog_query: str,
    query: str,
    limit: int,
    offset: int | None,
    retry_tool: str,
    resolve_dialog: DialogResolver,
    get_sender_type: SenderTypeGetter,
    reaction_names_threshold: int,
    context_radius: int = 3,
    navigation_token: str | None = None,
) -> SearchCapabilityResult:
    dialog_target = await resolve_dialog_target(
        cache=cache,
        query=dialog_query,
        retry_tool=retry_tool,
        resolve_dialog=resolve_dialog,
    )
    if isinstance(dialog_target, DialogTargetFailure):
        return dialog_target

    entity_id = dialog_target.entity_id
    page_offset = 0 if offset is None else offset
    if navigation_token is not None:
        try:
            page_offset = decode_search_navigation(
                navigation_token,
                expected_dialog_id=entity_id,
                expected_query=query,
            )
        except ValueError as exc:
            return NavigationFailure(
                kind="invalid_navigation",
                text=invalid_navigation_text(str(exc), retry_tool=retry_tool),
            )
    hits = [
        message
        async for message in client.iter_messages(
            entity_id,
            search=query,
            limit=limit,
            add_offset=page_offset,
        )
    ]
    _cache_message_senders(
        cache=cache,
        messages=hits,
        get_sender_type=get_sender_type,
    )

    context_messages_by_id = await _build_context_message_map(
        client,
        entity_id=entity_id,
        hits=hits,
        context_radius=context_radius,
    )
    _cache_message_senders(
        cache=cache,
        messages=list(context_messages_by_id.values()),
        get_sender_type=get_sender_type,
    )

    reaction_names_map = await _build_reaction_names_map(
        client,
        cache=cache,
        entity_id=entity_id,
        messages=hits,
        reaction_names_threshold=reaction_names_threshold,
    )

    next_offset = None
    navigation = None
    if len(hits) == limit:
        next_offset = page_offset + limit
        navigation = CapabilityNavigation(
            kind="search",
            token=encode_search_navigation(next_offset, entity_id, query),
        )

    return SearchExecution(
        entity_id=entity_id,
        dialog_name=dialog_target.display_name,
        resolve_prefix=dialog_target.resolve_prefix,
        hits=tuple(hits),
        context_messages_by_id=context_messages_by_id,
        reaction_names_map=reaction_names_map,
        next_offset=next_offset,
        navigation=navigation,
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
    fetch_topic_messages_fn: TopicFetcher | None = None,
    refresh_topic_by_id_fn: TopicRefresher | None = None,
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
