"""Tests for dialog_kind coercion and normalization in get_my_recent_activity."""

from __future__ import annotations

import pytest

from mcp_telegram.tools.activity import _append_normalized_dialog_kinds, _coerce_dialog_kind_values

# ---------------------------------------------------------------------------
# _coerce_dialog_kind_values
# ---------------------------------------------------------------------------


def test_coerce_none_returns_defaults() -> None:
    result = _coerce_dialog_kind_values(None)
    assert result == ["group", "forum"]


def test_coerce_string_is_wrapped() -> None:
    assert _coerce_dialog_kind_values("user") == ["user"]


def test_coerce_list_passes_through() -> None:
    assert _coerce_dialog_kind_values(["user", "bot"]) == ["user", "bot"]


def test_coerce_tuple_accepted() -> None:
    assert _coerce_dialog_kind_values(("channel",)) == ["channel"]


def test_coerce_set_accepted() -> None:
    result = _coerce_dialog_kind_values({"group", "forum"})
    assert sorted(result) == ["forum", "group"]


def test_coerce_invalid_type_raises() -> None:
    with pytest.raises(ValueError, match="list of strings"):
        _coerce_dialog_kind_values(42)


# ---------------------------------------------------------------------------
# _append_normalized_dialog_kinds — aliases
# ---------------------------------------------------------------------------


def test_append_alias_dms() -> None:
    result: list[str] = []
    _append_normalized_dialog_kinds("dms", result)
    assert result == ["user", "bot"]


def test_append_alias_private() -> None:
    result: list[str] = []
    _append_normalized_dialog_kinds("private", result)
    assert result == ["user", "bot"]


def test_append_alias_personal() -> None:
    result: list[str] = []
    _append_normalized_dialog_kinds("personal", result)
    assert result == ["user", "bot"]


def test_append_alias_direct() -> None:
    result: list[str] = []
    _append_normalized_dialog_kinds("direct", result)
    assert result == ["user", "bot"]


def test_append_alias_groups() -> None:
    result: list[str] = []
    _append_normalized_dialog_kinds("groups", result)
    assert result == ["group", "forum"]


def test_append_alias_supergroup() -> None:
    result: list[str] = []
    _append_normalized_dialog_kinds("supergroup", result)
    assert result == ["group"]


def test_append_alias_supergroups() -> None:
    result: list[str] = []
    _append_normalized_dialog_kinds("supergroups", result)
    assert result == ["group"]


def test_append_alias_chat() -> None:
    result: list[str] = []
    _append_normalized_dialog_kinds("chat", result)
    assert result == ["group"]


def test_append_alias_chats() -> None:
    result: list[str] = []
    _append_normalized_dialog_kinds("chats", result)
    assert result == ["group"]


def test_append_alias_forums() -> None:
    result: list[str] = []
    _append_normalized_dialog_kinds("forums", result)
    assert result == ["forum"]


def test_append_exact_kind_passes_through() -> None:
    result: list[str] = []
    _append_normalized_dialog_kinds("channel", result)
    assert result == ["channel"]


# ---------------------------------------------------------------------------
# _append_normalized_dialog_kinds — edge cases
# ---------------------------------------------------------------------------


def test_append_blank_string_skipped() -> None:
    result: list[str] = []
    _append_normalized_dialog_kinds("   ", result)
    assert result == []


def test_append_case_and_whitespace_insensitive() -> None:
    result: list[str] = []
    _append_normalized_dialog_kinds("  DMS  ", result)
    assert result == ["user", "bot"]


def test_append_duplicate_prevented() -> None:
    result: list[str] = ["user"]
    _append_normalized_dialog_kinds("user", result)
    assert result == ["user"]


def test_append_duplicate_from_alias_expansion_prevented() -> None:
    result: list[str] = ["user"]
    _append_normalized_dialog_kinds("dms", result)
    assert result == ["user", "bot"]


def test_append_invalid_kind_raises() -> None:
    result: list[str] = []
    with pytest.raises(ValueError, match="dialog_kinds entries must be one of"):
        _append_normalized_dialog_kinds("bogus", result)


def test_append_non_string_raises() -> None:
    result: list[str] = []
    with pytest.raises(ValueError, match="dialog_kinds entries must be strings"):
        _append_normalized_dialog_kinds(123, result)
