from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Awaitable, Callable, Literal, Protocol, TypedDict

from .pagination import HistoryDirection
from .resolver import Candidates, NotFound, Resolved, ResolvedWithMessage

if TYPE_CHECKING:
    from datetime import datetime

FORUM_TOPICS_PAGE_SIZE = 100
TOPIC_METADATA_TTL_SECONDS = 600
GENERAL_TOPIC_ID = 1
GENERAL_TOPIC_TITLE = "General"


# ---------------------------------------------------------------------------
# Protocol types for Telethon message objects
# ---------------------------------------------------------------------------


class SenderLike(Protocol):
    first_name: str | None


class ReplyHeaderLike(Protocol):
    reply_to_msg_id: int | None


class ReactionsLike(Protocol):
    results: list  # list of reaction result objects


class MessageLike(Protocol):
    id: int
    date: datetime
    message: str | None
    sender: SenderLike | None
    reply_to: ReplyHeaderLike | None
    reactions: ReactionsLike | None
    media: object


TopicNameGetter = Callable[[MessageLike], str | None]
LinePrefixGetter = Callable[[MessageLike], str | None]


class TopicMetadata(TypedDict, total=False):
    """Cached metadata for one forum topic.

    ``is_deleted`` means the topic was removed by the owner.
    ``inaccessible_error`` / ``inaccessible_at`` are tombstone fields for topics
    that exist but the current session cannot read (distinct from deleted).
    """

    topic_id: int
    title: str
    top_message_id: int | None
    is_general: bool
    is_deleted: bool
    inaccessible_error: str | None
    inaccessible_at: int | None


class TopicCatalog(TypedDict):
    """Full topic catalog for one dialog.

    ``choices`` maps topic_id → title for fuzzy resolution.
    ``deleted_topics`` carries tombstones separately so callers can report
    "topic was deleted" instead of "topic not found".
    """

    choices: dict[int, str]
    metadata_by_id: dict[int, TopicMetadata]
    deleted_topics: dict[int, TopicMetadata]


@dataclass(frozen=True)
class ExactTargetHints:
    """Bundle of pre-resolved identifiers that bypass fuzzy resolution."""
    dialog_id: int | None = None
    dialog_name: str | None = None
    topic_id: int | None = None
    topic_name: str | None = None
    topic_metadata: TopicMetadata | None = None


@dataclass(frozen=True)
class DialogMatch:
    """One candidate from fuzzy dialog resolution (used in ambiguous responses)."""

    entity_id: int
    display_name: str
    score: int
    username: str | None = None
    entity_type: str | None = None


@dataclass(frozen=True)
class DialogTargetFailure:
    """Dialog resolution failure: ``not_found`` or ``ambiguous`` (with candidate matches)."""
    kind: Literal["not_found", "ambiguous"]
    query: str
    text: str
    matches: tuple[DialogMatch, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ResolvedDialogTarget:
    """Successfully resolved dialog target.

    ``resolve_prefix`` is a human-readable disambiguation note (e.g.
    ``[resolved: "Name"]``) prepended to tool output.
    """

    entity_id: int
    query: str
    display_name: str
    resolve_prefix: str
    message_id: int | None = None


@dataclass(frozen=True)
class TopicMatch:
    """One candidate from fuzzy topic resolution (used in ambiguous responses)."""
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
    """Successfully resolved forum topic. ``reply_to_message_id`` is None for General topic."""
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
    """Successful topic listing — ``active_topics`` excludes deleted topics."""
    resolve_prefix: str
    dialog_name: str
    active_topics: tuple[TopicMetadata, ...]


@dataclass(frozen=True)
class HistoryReadExecution:
    """Successful history read result.

    ``fetched_messages`` is the raw API result.  ``messages`` is the
    (possibly sender-filtered) subset — cursor generation uses ``messages``,
    not ``fetched_messages``.
    """

    entity_id: int
    resolve_prefix: str
    topic_name: str | None
    messages: tuple[MessageLike, ...]
    fetched_messages: tuple[MessageLike, ...]
    reply_map: dict[int, MessageLike]
    reaction_names_map: dict[int, dict[str, list[str]]]
    topic_name_getter: TopicNameGetter | None
    navigation: CapabilityNavigation | None = None


@dataclass(frozen=True)
class SearchExecution:
    """Successful search result with rendered text and optional pagination."""

    entity_id: int
    dialog_name: str
    resolve_prefix: str
    hits: tuple[MessageLike, ...]
    context_messages_by_id: dict[int, MessageLike]
    reaction_names_map: dict[int, dict[str, list[str]]]
    next_offset: int | None
    navigation: CapabilityNavigation | None = None
    rendered_text: str = ""


# Type aliases for callable signatures
DialogResolveResult = Resolved | ResolvedWithMessage | Candidates | NotFound
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
TopicLoader = Callable[..., Awaitable[TopicCatalog]]
TopicFetcher = Callable[..., Awaitable[list[MessageLike]]]
TopicRefresher = Callable[..., Awaitable[TopicMetadata | None]]
