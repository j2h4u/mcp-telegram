"""Backwards-compatibility re-export shim.

All types and functions formerly in this module have been split into:
  models.py, budget.py, dialog_target.py, forum_topics.py,
  message_ops.py, capability_topics.py, capability_search.py, capability_history.py

This shim re-exports every public name so that existing ``from .capabilities import X``
statements continue to work.
"""
from __future__ import annotations

from .models import (
    FORUM_TOPICS_PAGE_SIZE,
    GENERAL_TOPIC_ID,
    GENERAL_TOPIC_TITLE,
    TOPIC_METADATA_TTL_SECONDS,
    CapabilityNavigation,
    DialogMatch,
    DialogResolver,
    DialogResolveResult,
    DialogTargetFailure,
    DialogTargetResult,
    ExactTargetHints,
    ForumTopicCapabilityResult,
    ForumTopicFailure,
    HistoryDirection,
    HistoryReadCapabilityResult,
    HistoryReadExecution,
    ListTopicsCapabilityResult,
    ListTopicsExecution,
    MessageReadFailure,
    NavigationFailure,
    ResolvedDialogTarget,
    ResolvedForumTopic,
    SearchCapabilityResult,
    SearchExecution,
    TopicCatalog,
    TopicFetcher,
    TopicLoader,
    TopicMatch,
    TopicMetadata,
    TopicRefresher,
)

from .budget import (
    UNREAD_TIER_BOT_DM,
    UNREAD_TIER_CHANNEL,
    UNREAD_TIER_HUMAN_DM,
    UNREAD_TIER_MENTION_DM,
    UNREAD_TIER_MENTION_GROUP,
    UNREAD_TIER_SMALL_GROUP,
    allocate_message_budget_proportional,
    unread_chat_tier,
)

from .dialog_target import (
    get_sender_type,
    resolve_dialog_target,
)

from .forum_topics import (
    build_get_forum_topics_by_id_request,
    build_get_forum_topics_request,
    build_topic_catalog,
    build_topic_name_getter,
    fetch_all_forum_topics,
    fetch_forum_topics_page,
    fetch_topic_messages,
    forum_topic_anchor_id,
    is_topic_id_invalid_error,
    load_dialog_topics,
    load_forum_topic_capability,
    message_matches_topic,
    messages_need_forum_topic_labels,
    normalize_topic_metadata,
    refresh_topic_by_id,
    resolve_deleted_topic,
    resolve_exact_topic_target,
    resolve_forum_topic,
    topic_empty_state_text,
    topic_row_text,
    topic_status,
    with_general_topic,
)

from .message_ops import (
    fetch_messages_for_topic,
    parse_history_navigation_input,
)

from .capability_topics import execute_list_topics_capability
from .capability_search import execute_search_messages_capability
from .capability_history import execute_history_read_capability
from .capability_unread import execute_unread_messages_capability

