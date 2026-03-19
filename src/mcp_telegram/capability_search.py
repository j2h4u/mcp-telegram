from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from telethon import TelegramClient  # type: ignore[import-untyped]

from .cache import EntityCache, MessageCache
from .dialog_target import resolve_dialog_target
from .errors import invalid_navigation_text
from .formatter import format_search_message_groups
from .message_ops import (
    _build_context_message_map,
    _build_reaction_names_map,
    _cache_message_senders,
)
from .models import (
    CapabilityNavigation,
    DialogResolver,
    DialogTargetFailure,
    ExactTargetHints,
    NavigationFailure,
    SearchCapabilityResult,
    SearchExecution,
)
from .pagination import decode_search_navigation, encode_search_navigation


async def execute_search_messages_capability(
    client: TelegramClient,
    *,
    cache: EntityCache,
    dialog_query: str | None,
    query: str,
    limit: int,
    navigation: str | None,
    retry_tool: str,
    resolve_dialog: DialogResolver,
    reaction_names_threshold: int,
    context_radius: int = 3,
    exact: ExactTargetHints | None = None,
) -> SearchCapabilityResult:
    """Search messages in one dialog and return rendered results with context.

    Returns ``SearchExecution`` on success, or ``DialogTargetFailure`` /
    ``NavigationFailure`` on resolution or cursor errors (never raises for
    expected failures — callers pattern-match on the return type).
    When *exact* is provided, ``dialog_query`` is bypassed.
    """
    exact_dialog_id = exact.dialog_id if exact else None
    exact_dialog_name = exact.dialog_name if exact else None

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

    entity_id = dialog_target.entity_id
    page_offset = 0
    if navigation is not None:
        try:
            page_offset = decode_search_navigation(
                navigation,
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
    )

    # BYP-04: Search always hits API (no cache read), but results populate cache
    # for future ListMessages page 2+ hits (CACHE-05)
    msg_cache = MessageCache(cache._conn)
    msg_cache.store_messages(entity_id, hits)

    context_messages_by_id = await _build_context_message_map(
        client,
        entity_id=entity_id,
        hits=hits,
        context_radius=context_radius,
    )
    _cache_message_senders(
        cache=cache,
        messages=list(context_messages_by_id.values()),
    )

    reaction_names_map = await _build_reaction_names_map(
        client,
        cache=cache,
        entity_id=entity_id,
        messages=hits,
        reaction_names_threshold=reaction_names_threshold,
    )
    rendered_text = format_search_message_groups(
        hits,
        context_messages_by_id=context_messages_by_id,
        reaction_names_map=reaction_names_map,
        context_radius=context_radius,
    )

    next_offset = None
    next_navigation = None
    if len(hits) == limit:
        next_offset = page_offset + limit
        next_navigation = CapabilityNavigation(
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
        navigation=next_navigation,
        rendered_text=rendered_text,
    )
