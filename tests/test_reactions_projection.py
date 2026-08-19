from __future__ import annotations

from types import SimpleNamespace

import pytest
from telethon.tl.types import ReactionPaid

from mcp_telegram.reactions.contracts import ReactionAggregate
from mcp_telegram.reactions.projection import project_reaction_aggregates


@pytest.mark.parametrize(
    ("reactions", "expected"),
    [
        (None, ()),
        (SimpleNamespace(results=[]), ()),
        (
            SimpleNamespace(
                results=[
                    SimpleNamespace(reaction=SimpleNamespace(emoticon="👍"), count=2),
                    SimpleNamespace(reaction=SimpleNamespace(document_id=12345), count=1),
                    SimpleNamespace(reaction=None, count=5),
                ]
            ),
            (ReactionAggregate(emoji="👍", count=2), ReactionAggregate(emoji="custom:12345", count=1)),
        ),
        (
            SimpleNamespace(results=[SimpleNamespace(reaction=ReactionPaid(), count=3)]),
            (ReactionAggregate(emoji="paid", count=3),),
        ),
    ],
)
def test_project_reaction_aggregates_handles_telegram_reaction_shapes(
    reactions: object | None,
    expected: tuple[ReactionAggregate, ...],
) -> None:
    assert project_reaction_aggregates(reactions) == expected
