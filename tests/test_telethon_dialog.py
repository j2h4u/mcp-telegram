"""Tests for Telethon entity — dialog type classification."""

from __future__ import annotations

from types import SimpleNamespace

from telethon.tl.types import Channel, Chat  # type: ignore[import-untyped]

from mcp_telegram.models import DialogType
from mcp_telegram.telethon_dialog import classify_dialog_type


def _make_channel(*, forum: bool = False, megagroup: bool = False) -> Channel:
    entity = Channel.__new__(Channel)
    entity.forum = forum
    entity.megagroup = megagroup
    return entity


def _make_chat() -> Chat:
    return Chat.__new__(Chat)


class TestClassifyDialogType:
    def test_forum(self) -> None:
        assert classify_dialog_type(_make_channel(forum=True, megagroup=True)) == DialogType.FORUM

    def test_supergroup(self) -> None:
        assert classify_dialog_type(_make_channel(forum=False, megagroup=True)) == DialogType.SUPERGROUP

    def test_channel(self) -> None:
        assert classify_dialog_type(_make_channel(forum=False, megagroup=False)) == DialogType.CHANNEL

    def test_group(self) -> None:
        assert classify_dialog_type(_make_chat()) == DialogType.GROUP

    def test_user(self) -> None:
        entity = SimpleNamespace(first_name="Ivan", bot=False)
        assert classify_dialog_type(entity) == DialogType.USER

    def test_bot(self) -> None:
        entity = SimpleNamespace(first_name="BotFather", bot=True)
        assert classify_dialog_type(entity) == DialogType.BOT

    def test_none_is_unknown(self) -> None:
        assert classify_dialog_type(None) == DialogType.UNKNOWN

    def test_arbitrary_object_is_unknown(self) -> None:
        assert classify_dialog_type(object()) == DialogType.UNKNOWN

    def test_user_without_bot_attr_is_user(self) -> None:
        entity = SimpleNamespace(first_name="Ivan")
        assert classify_dialog_type(entity) == DialogType.USER

    def test_entity_without_first_name_is_unknown(self) -> None:
        entity = SimpleNamespace()
        assert classify_dialog_type(entity) == DialogType.UNKNOWN


def test_return_type_is_dialog_type_enum() -> None:
    result = classify_dialog_type(None)
    assert isinstance(result, DialogType), "return value is a DialogType instance"
