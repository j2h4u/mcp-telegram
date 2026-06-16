"""Tests for the canonical DialogType enum (models.py).

Locks the semantic trap that caused divergent vocabularies before unification:
capitalized "Group" = MEGAGROUP (supergroup), but lowercase "group" = LEGACY BASIC
GROUP — opposites. parse() must map both casings explicitly, never via .lower().
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from mcp_telegram.models import DialogType

# --- parse(): trap-aware string parsing -------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # lowercase storage vocabulary
        ("channel", DialogType.CHANNEL),
        ("supergroup", DialogType.SUPERGROUP),
        ("forum", DialogType.FORUM),
        ("group", DialogType.GROUP),  # lowercase group = legacy basic group
        ("user", DialogType.USER),
        ("bot", DialogType.BOT),
        ("unknown", DialogType.UNKNOWN),
        # capitalized legacy vocabulary
        ("Channel", DialogType.CHANNEL),
        ("Group", DialogType.SUPERGROUP),  # TRAP: capitalized Group = megagroup
        ("Chat", DialogType.GROUP),  # TRAP: Chat = legacy basic group
        ("Forum", DialogType.FORUM),
        ("User", DialogType.USER),
        ("Bot", DialogType.BOT),
        # aliases / fallbacks
        ("megagroup", DialogType.SUPERGROUP),
        (" channel ", DialogType.CHANNEL),  # whitespace tolerated
        ("nonsense", DialogType.UNKNOWN),
        (None, DialogType.UNKNOWN),
    ],
)
def test_parse(raw, expected):
    assert DialogType.parse(raw) == expected


def test_parse_is_idempotent_on_enum():
    assert DialogType.parse(DialogType.SUPERGROUP) is DialogType.SUPERGROUP


def test_parse_trap_group_vs_group_are_opposites():
    # The whole point of the explicit map: these MUST differ.
    assert DialogType.parse("Group") == DialogType.SUPERGROUP
    assert DialogType.parse("group") == DialogType.GROUP
    assert DialogType.parse("Group") != DialogType.parse("group")


def test_strenum_binds_as_lowercase_value():
    # StrEnum is a str → safe to bind directly in SQL `WHERE type = ?`.
    assert DialogType.SUPERGROUP == "supergroup"
    assert f"{DialogType.CHANNEL}" == "channel"


# --- from_entity(): the sole Telethon-flag reader ---------------------------


class _FakeChannel:
    """Stand-in matching telethon.tl.types.Channel via isinstance is not possible
    without the real class, so from_entity is exercised against real telethon types
    below; here we only assert the None/duck-typed branches."""


def test_from_entity_none_is_unknown():
    assert DialogType.from_entity(None) == DialogType.UNKNOWN


def test_from_entity_user_and_bot_ducktyped():
    user = SimpleNamespace(first_name="Max", bot=False)
    bot = SimpleNamespace(first_name="HelperBot", bot=True)
    assert DialogType.from_entity(user) == DialogType.USER
    assert DialogType.from_entity(bot) == DialogType.BOT


def test_from_entity_real_telethon_types():
    from telethon.tl.types import Channel, Chat

    # Construct minimal real instances (flags drive classification).
    broadcast = Channel(id=1, title="b", photo=None, date=None, megagroup=False)
    supergroup = Channel(id=2, title="s", photo=None, date=None, megagroup=True)
    forum = Channel(id=3, title="f", photo=None, date=None, megagroup=True, forum=True)
    legacy = Chat(id=4, title="g", photo=None, participants_count=2, date=None, version=1)

    assert DialogType.from_entity(broadcast) == DialogType.CHANNEL
    assert DialogType.from_entity(supergroup) == DialogType.SUPERGROUP
    assert DialogType.from_entity(forum) == DialogType.FORUM
    assert DialogType.from_entity(legacy) == DialogType.GROUP
