"""Reaction-only projections for Telegram response objects."""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from .contracts import ReactionAggregate


def project_reaction_aggregates(reactions: object | None) -> tuple[ReactionAggregate, ...]:
    """Project only Telegram's aggregate reaction fields.

    This intentionally accepts the nested ``MessageReactions`` object rather
    than a full message.  JIT reaction reads must not depend on unrelated
    message fields such as text, sender, or timestamp.
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
