from __future__ import annotations

from typing import cast

from .cache import EntityCache, TopicMetadataCache
from .dialog_target import resolve_dialog_target
from .forum_topics import load_forum_topic_capability
from .models import (
    DialogResolver,
    DialogTargetFailure,
    ForumTopicFailure,
    ListTopicsCapabilityResult,
    ListTopicsExecution,
    TopicCatalog,
    TopicLoader,
)


async def execute_list_topics_capability(
    client: object,
    *,
    cache: EntityCache,
    dialog_query: str,
    retry_tool: str,
    resolve_dialog: DialogResolver,
    load_topics: TopicLoader | None = None,
) -> ListTopicsCapabilityResult:
    """Resolve a dialog and return its forum topic catalog.

    Returns ``ListTopicsExecution`` on success, or ``DialogTargetFailure`` /
    ``ForumTopicFailure`` on resolution or catalog errors (never raises for
    expected failures — callers pattern-match on the return type).
    """
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

    # requested_topic=None → always TopicCatalog, never ResolvedForumTopic
    topic_catalog = cast("TopicCatalog", topic_capability)
    active_topics = tuple(
        topic_catalog["metadata_by_id"][topic_id]
        for topic_id in sorted(topic_catalog["choices"])
    )
    return ListTopicsExecution(
        resolve_prefix=dialog_target.resolve_prefix,
        dialog_name=dialog_target.display_name,
        active_topics=active_topics,
    )
