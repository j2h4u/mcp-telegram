from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal, NotRequired, TypedDict

FORUM_TOPICS_PAGE_SIZE = 100
TOPIC_METADATA_TTL_SECONDS = 600
GENERAL_TOPIC_ID = 1
GENERAL_TOPIC_TITLE = "General"


class DialogType(StrEnum):
    """Canonical dialog/entity type — the single vocabulary for this project.

    The Telegram API has no flat type: the kind is an entity class (``User`` /
    ``Chat`` / ``Channel``) plus boolean flags (``megagroup`` / ``forum`` /
    ``bot``). This enum is the canonical stored vocabulary and is parsed from a
    stored/legacy string by :meth:`parse`. Values are lowercase to match what
    the ``dialogs.type`` column already stores (no data migration).

    Historical hazard this replaces: the codebase previously flattened the type in
    three divergent ways, and a capitalized ``"Group"`` meant a MEGAGROUP while a
    lowercase ``"group"`` meant a LEGACY BASIC GROUP — opposites. :meth:`parse` uses
    an explicit map (never ``.lower()``) so that hazard cannot recur.
    """

    USER = "user"
    BOT = "bot"
    CHANNEL = "channel"  # broadcast channel (Channel, megagroup=False)
    SUPERGROUP = "supergroup"  # megagroup (Channel, megagroup=True, forum=False)
    FORUM = "forum"  # forum supergroup (Channel, megagroup=True, forum=True)
    GROUP = "group"  # legacy basic group (Chat)
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, raw: str | DialogType | None) -> DialogType:
        """Parse a stored/legacy type string to the canonical enum.

        Trap-aware by design — NEVER lowercase-and-match. The capitalized vocabulary
        (``"Group"`` = megagroup, ``"Chat"`` = legacy basic group) is the inverse of
        the lowercase one, so both casings are mapped explicitly.
        """
        if isinstance(raw, DialogType):
            return raw
        if raw is None:
            return cls.UNKNOWN
        return _DIALOG_TYPE_ALIASES.get(raw.strip(), cls.UNKNOWN)


# Explicit alias map for DialogType.parse — the trap-aware replacement for `.lower()`.
# Lowercase entries = the `dialogs.type` storage vocabulary; capitalized entries =
# the legacy `_classify_dialog_type` vocabulary. Note "Group"->SUPERGROUP (megagroup)
# vs "group"->GROUP (legacy), and "Chat"->GROUP — these inversions are intentional.
_DIALOG_TYPE_ALIASES: dict[str, DialogType] = {
    "user": DialogType.USER,
    "User": DialogType.USER,
    "bot": DialogType.BOT,
    "Bot": DialogType.BOT,
    "channel": DialogType.CHANNEL,
    "Channel": DialogType.CHANNEL,
    "supergroup": DialogType.SUPERGROUP,
    "megagroup": DialogType.SUPERGROUP,
    "Group": DialogType.SUPERGROUP,  # capitalized "Group" = megagroup
    "forum": DialogType.FORUM,
    "Forum": DialogType.FORUM,
    "group": DialogType.GROUP,  # lowercase "group" = legacy basic group
    "Chat": DialogType.GROUP,  # capitalized "Chat" = legacy basic group
    "unknown": DialogType.UNKNOWN,
    "Unknown": DialogType.UNKNOWN,
}

TraceGapSeverity = Literal["info", "warning", "action_required"]
TraceCoverageState = Literal["complete", "partial", "unknown"]
TraceAuthorshipBasis = Literal["effective_sender_id", "post_author_signature"]
TraceCoverageGoal = Literal["observed", "best_effort_visible"]


class TraceResolvedAccount(TypedDict):
    """Account resolution metadata returned with every Account Trace result."""

    confidence: Literal["resolved", "ambiguous", "unresolved"]
    account_id: int | None
    display_name: str | None
    username: str | None
    candidate_ids: list[int]
    display_aliases: list[str]
    resolution_source: str


class TraceEvidenceItem(TypedDict):
    """One observable authored-message evidence item in an Account Trace page."""

    source: str
    evidence_kind: Literal["authored_message"]
    dialog_id: int
    dialog_title: str | None
    dialog_type: str | None
    topic_id: int | None
    topic_title: str | None
    message_id: int
    sent_at: int
    sender_id: int | None
    effective_sender_id: int | None
    authorship_basis: TraceAuthorshipBasis
    author_signature: str | None
    text: str | None
    media_description: str | None


class TraceCoverageGap(TypedDict):
    """A controlled explanation for missing or partial Account Trace coverage."""

    kind: str
    severity: TraceGapSeverity
    detail: str
    dialog_id: NotRequired[int]
    topic_id: NotRequired[int]
    action: NotRequired[dict[str, object]]
    next_action: NotRequired[dict[str, object]]


class TraceCoverageSummary(TypedDict):
    """Bounded coverage accounting for the returned evidence page."""

    state: TraceCoverageState
    observed_message_count: int
    dialogs_considered: int
    dialogs_considered_basis: str
    dialogs_with_hits: int
    dialogs_with_gaps: int
    as_of: int


# ---------------------------------------------------------------------------
# Message data model — read side
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class ReadReactionEvent:
    """One individual Telegram reaction fact projected to read surfaces.

    ``reacted_at`` is Telegram's event timestamp and is intentionally nullable:
    the API may omit it.  It must never be replaced with message, fetch, or
    persistence time.
    """

    reactor_id: int | None
    emoji: str
    reacted_at: int | None


@dataclass(frozen=True, slots=True, kw_only=True)
class ReadMessage:
    """Message row as returned by list_messages and search queries.

    Field names match the SELECT column names in _LIST_MESSAGES_BASE_SQL.
    reactions_display and dialog_name are injected after the DB query.
    """

    # Core fields — always present in every query path
    message_id: int
    sent_at: int
    dialog_id: int
    # Fields present in full list_messages query; default to None/0 for
    # partial queries (search snippets, unread summary) where they are absent.
    text: str | None = None
    sender_id: int | None = None
    sender_first_name: str | None = None
    media_description: str | None = None
    reply_to_msg_id: int | None = None
    forum_topic_id: int | None = None
    is_deleted: int = 0
    deleted_at: int | None = None
    edit_date: int | None = None
    topic_title: str | None = None
    effective_sender_id: int | None = None
    is_service: int = 0
    out: int = 0
    fwd_from_name: str | None = None
    post_author: str | None = None
    # injected after DB query
    # Telegram outbox read date, when available. This remains nullable for
    # incoming/group messages and for privacy/retention-limited responses.
    read_at: int | None = None
    # Individual reaction facts are separate from aggregate counters.  The
    # status reports whether Telegram detail retrieval was complete, partial,
    # missing, or unavailable for this message.
    reaction_events: tuple[ReadReactionEvent, ...] = ()
    reaction_events_status: str = "unavailable"
    reactions_display: str = ""
    dialog_name: str | None = None

    @property
    def id(self) -> int:
        return self.message_id

    @property
    def date(self) -> datetime:
        return datetime.fromtimestamp(self.sent_at, tz=UTC)


TopicNameGetter = Callable[[ReadMessage], str | None]
LinePrefixGetter = Callable[[ReadMessage], str | None]


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
    """Topic resolution failed.

    Kinds: catalog_unavailable (dialog has no topic catalog), inaccessible (TOPIC_PRIVATE RPC),
    not_found (no match), ambiguous (multiple matches), deleted (matched but tombstoned),
    deleted_ambiguous (all matches tombstoned).
    """

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
    """Message read failed.

    Kinds: invalid_cursor (malformed navigation token), sender_not_found (sender filter matched
    nothing), sender_ambiguous (multiple sender matches), deleted (dialog tombstoned in sync.db),
    inaccessible (Telegram RPC denied access).
    """

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
    """Invalid or mismatched navigation token — returned when decode/validation fails."""

    kind: Literal["invalid_navigation"]
    text: str


@dataclass(frozen=True)
class CapabilityNavigation:
    """Opaque next-page token for history or search continuation."""

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
    messages: tuple[ReadMessage, ...]
    fetched_messages: tuple[ReadMessage, ...]
    reply_map: dict[int, ReadMessage]
    reaction_names_map: dict[int, dict[str, list[str]]]
    topic_name_getter: TopicNameGetter | None
    navigation: CapabilityNavigation | None = None


@dataclass(frozen=True)
class SearchExecution:
    """Successful search result with rendered text and optional pagination."""

    entity_id: int
    dialog_name: str
    resolve_prefix: str
    hits: tuple[ReadMessage, ...]
    context_messages_by_id: dict[int, ReadMessage]
    reaction_names_map: dict[int, dict[str, list[str]]]
    next_offset: int | None
    navigation: CapabilityNavigation | None = None
    rendered_text: str = ""


# Type aliases for callable signatures
DialogTargetResult = ResolvedDialogTarget | DialogTargetFailure
ForumTopicCapabilityResult = TopicCatalog | ResolvedForumTopic | ForumTopicFailure
ListTopicsCapabilityResult = ListTopicsExecution | DialogTargetFailure | ForumTopicFailure
HistoryReadCapabilityResult = (
    HistoryReadExecution | DialogTargetFailure | ForumTopicFailure | MessageReadFailure | NavigationFailure
)
SearchCapabilityResult = SearchExecution | DialogTargetFailure | NavigationFailure
TopicLoader = Callable[..., Awaitable[TopicCatalog]]
TopicFetcher = Callable[..., Awaitable[list[ReadMessage]]]
TopicRefresher = Callable[..., Awaitable[TopicMetadata | None]]


# ---------------------------------------------------------------------------
# Phase 39.3: bidirectional read-state
# ---------------------------------------------------------------------------


CursorState = Literal["populated", "null", "all_read"]
"""Tri-state cursor tag:

- ``populated`` — cursor has a value AND there are messages past it (or count == 0 AND cursor is up-to-date).
- ``null``     — cursor is NULL in sync.db (bootstrap pending). NEVER means "all read".
- ``all_read`` — cursor value >= highest known message_id on that side (caught up).
"""


class ReadState(TypedDict):
    """Bidirectional read-state snapshot for one DM.

    Emitted by daemon-side helpers; consumed by formatter-side header /
    inline-marker helpers. All counts are integers; dates are unix seconds (UTC).

    Fields:
    - inbox_unread_count  — incoming unread by me (peer → me).
    - inbox_oldest_unread_date — unix seconds of the oldest unread incoming
      message (MIN(sent_at) WHERE out=0 AND message_id > read_inbox_max_id);
      omitted when count == 0 or cursor is NULL.
    - inbox_cursor_state  — CursorState tag for the inbox side.
    - inbox_max_id_anchor — current ``read_inbox_max_id`` cursor; omitted when NULL.
    - outbox_unread_count — outgoing unread by peer (me → peer).
    - outbox_oldest_unread_date — symmetric MIN over ``out=1``.
    - outbox_cursor_state — CursorState tag.
    - outbox_max_id_anchor — current ``read_outbox_max_id`` cursor; omitted when NULL.
    """

    inbox_unread_count: int
    inbox_oldest_unread_date: NotRequired[int]
    inbox_cursor_state: CursorState
    inbox_max_id_anchor: NotRequired[int]
    outbox_unread_count: int
    outbox_oldest_unread_date: NotRequired[int]
    outbox_cursor_state: CursorState
    outbox_max_id_anchor: NotRequired[int]
