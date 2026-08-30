from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from mcp_telegram.tools.unread import GetInbox, _resolve_inbox_since


def test_inbox_since_resolver_canonicalizes_absolute_and_relative_bounds() -> None:
    fixed_now = datetime(2026, 8, 20, 12, 34, 56, 789000, tzinfo=UTC)
    assert _resolve_inbox_since("2026-08-20T10:00:00+00:00", None) == "2026-08-20T10:00:00Z"
    assert _resolve_inbox_since(None, 2, now=fixed_now) == "2026-08-20T10:34:56Z"


@pytest.mark.parametrize("value", [True, 1.0, "1", float("nan"), float("inf")])
def test_inbox_last_hours_requires_strict_integer(value: object) -> None:
    with pytest.raises(ValidationError):
        GetInbox.model_validate({"last_hours": value})


def test_inbox_fractional_since_rounds_up_for_second_storage() -> None:
    assert _resolve_inbox_since("1970-01-01T00:00:00.1Z", None) == "1970-01-01T00:00:01Z"
    assert _resolve_inbox_since("1970-01-01T00:00:00Z", None) == "1970-01-01T00:00:00Z"


@pytest.mark.parametrize(
    "clock",
    [
        datetime(2026, 8, 20, 12, 0, tzinfo=UTC).replace(tzinfo=None),
        datetime(2026, 8, 20, 12, 0, tzinfo=timezone(timedelta(hours=2))),
    ],
)
def test_inbox_relative_resolver_requires_aware_utc_clock(clock: datetime) -> None:
    with pytest.raises(ValueError, match="aware UTC"):
        _resolve_inbox_since(None, 1, now=clock)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"since_utc": "2026-08-20T10:00:00Z", "last_hours": 2},
        {"since_utc": "2026-08-20T10:00:00"},
        {"since_utc": "2026-08-20T10:00:00+05:00"},
        {"last_hours": 0},
        {"last_hours": 721},
    ],
)
def test_inbox_time_filter_validation_is_actionable(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="mutually exclusive|offset|between 1 and 720"):
        GetInbox.model_validate(kwargs)
