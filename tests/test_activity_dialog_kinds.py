"""The MCP and daemon boundaries share activity-filter normalization."""

import pytest
from pydantic import ValidationError

from mcp_telegram.activity_dialog_kinds import normalize_activity_dialog_kinds
from mcp_telegram.daemon_activity_stats import _parse_recent_activity_limits
from mcp_telegram.tools.activity import GetMyRecentActivity


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ["group", "forum"]),
        (" group ", ["group"]),
        ([" FORUM ", "group", "forum", ""], ["forum", "group"]),
        (["unknown", "channel", "user", "bot"], ["unknown", "channel", "user", "bot"]),
        (("group", "forum"), ["group", "forum"]),
        ({"group"}, ["group"]),
        ("all", ["all"]),
        (["forum", "ALL", "dm"], ["all"]),
        *[(alias, ["user", "bot"]) for alias in ("dm", "dms", "private", "personal", "direct")],
        ("groups", ["group", "forum"]),
        *[(alias, ["group"]) for alias in ("supergroup", "supergroups", "chat", "chats")],
        ("forums", ["forum"]),
    ],
)
def test_activity_dialog_kinds_match_at_both_boundaries(value: object, expected: list[str]) -> None:
    assert normalize_activity_dialog_kinds(value) == expected
    assert GetMyRecentActivity.model_validate({"dialog_kinds": value}).dialog_kinds == expected
    _, _, kinds, error = _parse_recent_activity_limits({"dialog_kinds": value})
    assert error is None
    assert kinds == expected


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ([], "dialog_kinds must include at least one kind"),
        ("", "dialog_kinds must include at least one kind"),
        (" \t ", "dialog_kinds must include at least one kind"),
        (["", " "], "dialog_kinds must include at least one kind"),
        (123, "dialog_kinds must be a list of strings"),
        (False, "dialog_kinds must be a list of strings"),
        ({"kind": "group"}, "dialog_kinds must be a list of strings"),
        ([None], "dialog_kinds entries must be strings"),
        (["all", 123], "dialog_kinds entries must be strings"),
        (["all", "spaceship"], "dialog_kinds entries must be one of:"),
        ("spaceship", "dialog_kinds entries must be one of:"),
    ],
)
def test_activity_dialog_kinds_reject_invalid_filters_at_both_boundaries(value: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        normalize_activity_dialog_kinds(value)
    with pytest.raises(ValidationError, match=message):
        GetMyRecentActivity.model_validate({"dialog_kinds": value})
    since_hours, limit, kinds, error = _parse_recent_activity_limits({"dialog_kinds": value})
    assert (since_hours, limit, kinds) == (None, None, None)
    assert error is not None
    assert error["error"] == "invalid_dialog_kinds"
    assert str(error["message"]).startswith(message)


def test_activity_dialog_kinds_omission_uses_default_at_both_boundaries() -> None:
    assert GetMyRecentActivity().dialog_kinds == ["group", "forum"]
    _, _, kinds, error = _parse_recent_activity_limits({})
    assert error is None
    assert kinds == ["group", "forum"]
