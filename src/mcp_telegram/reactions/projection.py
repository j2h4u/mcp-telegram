"""Reaction-only projections for Telegram response objects."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import cast

from .contracts import ReactionAggregate, ReactionEvent


def project_reaction_aggregates(reactions: object | None) -> tuple[ReactionAggregate, ...]:
    """Project only Telegram's aggregate reaction fields.

    This intentionally accepts the nested ``MessageReactions`` object rather
    than a full message, so aggregate projection does not depend on unrelated
    fields such as text, sender, or timestamp.
    """
    if reactions is None:
        return ()
    results = cast(Sequence[object], getattr(reactions, "results", ()) or ())
    aggregates: list[ReactionAggregate] = []
    for item in results:
        reaction = getattr(item, "reaction", None)
        emoji = _emoji(reaction)
        count = getattr(item, "count", 0)
        if emoji is not None:
            aggregates.append(ReactionAggregate(emoji=emoji, count=int(count)))
    return tuple(aggregates)


def _emoji(reaction: object | None) -> str | None:
    if reaction is None:
        return None
    emoticon = getattr(reaction, "emoticon", None)
    if isinstance(emoticon, str):
        return emoticon
    document_id = getattr(reaction, "document_id", None)
    if isinstance(document_id, int):
        return f"custom:{document_id}"
    if reaction.__class__.__name__ == "ReactionPaid":
        return "paid"
    return None


def project_reaction_event(item: object) -> ReactionEvent:
    """Preserve one returned reaction actor without implying list completeness."""
    peer = getattr(item, "peer_id", None)
    user_id = getattr(peer, "user_id", None)
    chat_id = getattr(peer, "chat_id", None)
    channel_id = getattr(peer, "channel_id", None)
    reactor_id = None
    if isinstance(user_id, int):
        reactor_id = user_id
    elif isinstance(chat_id, int):
        reactor_id = -chat_id
    elif isinstance(channel_id, int):
        reactor_id = -1000000000000 - channel_id
    reaction = getattr(item, "reaction", None)
    date = getattr(item, "date", None)
    emoji = _emoji(reaction)
    return ReactionEvent(
        reactor_id=reactor_id,
        emoji=emoji if emoji is not None else str(reaction),
        reacted_at=int(date.timestamp()) if isinstance(date, datetime) else None,
    )
