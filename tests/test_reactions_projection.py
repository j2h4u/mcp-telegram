from __future__ import annotations

from types import SimpleNamespace

from mcp_telegram.reactions.contracts import ReactionAggregate
from mcp_telegram.reactions.projection import project_reaction_aggregates


def _make_tg_reaction(emoji: str | None = None, count: int = 1, document_id: int | None = None) -> SimpleNamespace:
    reaction_namespace = SimpleNamespace(emoticon=emoji, document_id=document_id)
    return SimpleNamespace(reaction=reaction_namespace, count=count)


def _make_tg_reaction_paid(count: int = 1) -> SimpleNamespace:
    class ReactionPaid:
        pass

    return SimpleNamespace(reaction=ReactionPaid(), count=count)


def _make_tg_reactions(*items: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(results=list(items))


def test_project_reaction_aggregates_none_input() -> None:
    result = project_reaction_aggregates(None)

    assert result == ()


def test_project_reaction_aggregates_empty_results() -> None:
    tg_reactions = SimpleNamespace(results=())

    result = project_reaction_aggregates(tg_reactions)

    assert result == ()


def test_project_reaction_aggregates_standard_emoji() -> None:
    tg_reactions = _make_tg_reactions(_make_tg_reaction(emoji="👍", count=3))

    result = project_reaction_aggregates(tg_reactions)

    assert len(result) == 1
    assert result[0] == ReactionAggregate(emoji="👍", count=3)


def test_project_reaction_aggregates_multiple_emojis() -> None:
    tg_reactions = _make_tg_reactions(
        _make_tg_reaction(emoji="👍", count=5),
        _make_tg_reaction(emoji="❤️", count=2),
        _make_tg_reaction(emoji="😂", count=10),
    )

    result = project_reaction_aggregates(tg_reactions)

    assert result == (
        ReactionAggregate(emoji="👍", count=5),
        ReactionAggregate(emoji="❤️", count=2),
        ReactionAggregate(emoji="😂", count=10),
    )


def test_project_reaction_aggregates_custom_emoji() -> None:
    tg_reactions = _make_tg_reactions(_make_tg_reaction(emoji=None, document_id=1234567890, count=4))

    result = project_reaction_aggregates(tg_reactions)

    assert result == (ReactionAggregate(emoji="custom:1234567890", count=4),)


def test_project_reaction_aggregates_paid_reaction() -> None:
    tg_reactions = _make_tg_reactions(_make_tg_reaction_paid(count=1))

    result = project_reaction_aggregates(tg_reactions)

    assert result == (ReactionAggregate(emoji="paid", count=1),)


def test_project_reaction_aggregates_mixed() -> None:
    tg_reactions = _make_tg_reactions(
        _make_tg_reaction(emoji="👍", count=3),
        _make_tg_reaction(emoji=None, document_id=999, count=2),
        _make_tg_reaction_paid(count=1),
    )

    result = project_reaction_aggregates(tg_reactions)

    assert result == (
        ReactionAggregate(emoji="👍", count=3),
        ReactionAggregate(emoji="custom:999", count=2),
        ReactionAggregate(emoji="paid", count=1),
    )


def test_project_reaction_aggregates_skips_items_without_emoji() -> None:
    tg_empty = SimpleNamespace(reaction=None, count=5)
    tg_reactions = SimpleNamespace(results=[tg_empty])

    result = project_reaction_aggregates(tg_reactions)

    assert result == ()
