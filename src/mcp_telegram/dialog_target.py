from __future__ import annotations

from .cache import EntityCache, GROUP_TTL, USER_TTL
from .errors import ambiguous_dialog_text, dialog_not_found_text
from .models import (
    DialogMatch,
    DialogResolver,
    DialogTargetFailure,
    DialogTargetResult,
    ResolvedDialogTarget,
)
from .resolver import Candidates, NotFound

from typing import Literal

from telethon.tl.types import Channel, Chat  # type: ignore[import-untyped]

# Our internal taxonomy — independent of Telegram's group/supergroup/channel evolution.
# Behavior is driven by participant count, not by Telegram entity type.
DialogCategory = Literal["user", "bot", "group", "channel"]


def classify_dialog(dialog: object) -> DialogCategory:
    """Map one Telethon dialog to our internal category.

    Telegram's taxonomy leaks implementation details: supergroups (megagroups) are
    Channel entities with is_channel=True, even for 3-person groups with topics enabled.
    We collapse that into a simpler model:

    - "user"    — 1:1 with a human
    - "bot"     — 1:1 with a bot
    - "group"   — any multi-user chat (basic group, supergroup, megagroup)
    - "channel" — broadcast-only (no member posting)
    """
    if getattr(dialog, "is_user", False):
        entity = getattr(dialog, "entity", None)
        if entity is not None and getattr(entity, "bot", False):
            return "bot"
        return "user"
    if getattr(dialog, "is_group", False):
        return "group"
    if getattr(dialog, "is_channel", False):
        return "channel"
    return "group"


def get_sender_type(sender: object) -> str:
    """Determine sender type from Telethon entity instance."""
    if isinstance(sender, Channel):
        return "channel"
    elif isinstance(sender, Chat):
        return "group"
    return "user"


def _dialog_match_from_dict(match: dict[str, object]) -> DialogMatch:
    return DialogMatch(
        entity_id=int(match["entity_id"]),  # type: ignore[call-overload]
        display_name=str(match["display_name"]),
        score=int(match["score"]),  # type: ignore[call-overload]
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
    query: str | None,
    retry_tool: str,
    resolve_dialog: DialogResolver,
    exact_dialog_id: int | None = None,
    exact_dialog_name: str | None = None,
) -> DialogTargetResult:
    """Resolve one dialog query into an inspectable target or actionable failure."""
    if exact_dialog_id is not None:
        cached_dialog = cache.get(
            exact_dialog_id,
            ttl_seconds=max(USER_TTL, GROUP_TTL),
        )
        display_name = exact_dialog_name
        if display_name is None and cached_dialog is not None:
            cached_name = cached_dialog.get("name")
            if isinstance(cached_name, str) and cached_name:
                display_name = cached_name
        if display_name is None:
            display_name = str(exact_dialog_id)

        return ResolvedDialogTarget(
            entity_id=exact_dialog_id,
            query=str(exact_dialog_id),
            display_name=display_name,
            resolve_prefix="",
        )

    if query is None:
        raise ValueError("query is required when exact_dialog_id is not provided")

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
    message_id = getattr(result, "message_id", None)
    return ResolvedDialogTarget(
        entity_id=result.entity_id,
        query=query,
        display_name=result.display_name,
        resolve_prefix=resolve_prefix,
        message_id=message_id,
    )
