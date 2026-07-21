from __future__ import annotations

from typing import cast

import pytest
from pydantic import ValidationError

from mcp_telegram.daemon_reading import DaemonReadingService
from mcp_telegram.tools.reading import ListMessages, SearchMessages


@pytest.mark.parametrize(
    "model, kwargs",
    [
        (ListMessages, {"exact_dialog_id": 17}),
        (SearchMessages, {"query": "needle"}),
    ],
)
def test_reading_tools_accept_utc_bounds_and_reject_reversed_ranges(model: type, kwargs: dict) -> None:
    args = cast(
        ListMessages | SearchMessages,
        model(**kwargs, since_utc="2026-01-01T00:00:00Z", until_utc="2026-02-01T00:00:00+00:00"),
    )
    assert args.since_utc is not None
    assert args.until_utc is not None
    assert args.since_utc.endswith("Z")
    assert args.until_utc.endswith("00:00")

    with pytest.raises(ValidationError, match="since_utc must be earlier than until_utc"):
        model(**kwargs, since_utc="2026-02-01T00:00:00Z", until_utc="2026-01-01T00:00:00Z")

    with pytest.raises(ValidationError, match="UTC"):
        model(**kwargs, since_utc="2026-01-01T00:00:00")


@pytest.mark.parametrize(
    "model, kwargs",
    [
        (ListMessages, {"exact_dialog_id": 17}),
        (SearchMessages, {"query": "needle"}),
    ],
)
@pytest.mark.parametrize(
    "field, value",
    [
        ("since_utc", "2026-01-01 00:00:00Z"),
        ("until_utc", "2026-01-01T00:00:00,123Z"),
        ("since_utc", "2026-01-01T00:00:00+0000"),
        ("since_utc", "2026-01-01T00:00:00.1234567Z"),
        ("until_utc", "2026-01-01T00:00Z"),
        ("since_utc", "２０２６-01-01T00:00:00Z"),
    ],
)
def test_reading_tools_reject_non_rfc3339_utc_bounds(
    model: type,
    kwargs: dict,
    field: str,
    value: str,
) -> None:
    with pytest.raises(ValidationError, match="RFC 3339"):
        model(**kwargs, **{field: value})


@pytest.mark.parametrize(
    "model, kwargs",
    [
        (ListMessages, {"exact_dialog_id": 17}),
        (SearchMessages, {"query": "needle"}),
    ],
)
@pytest.mark.parametrize("field", ["since_utc", "until_utc"])
@pytest.mark.parametrize("value", ["2026-01-01T00:00:00", "2026-01-01T00:00:00+01:00"])
def test_reading_tools_reject_missing_or_non_utc_offsets(
    model: type,
    kwargs: dict,
    field: str,
    value: str,
) -> None:
    with pytest.raises(ValidationError, match=r"must include the UTC offset Z or \+00:00"):
        model(**kwargs, **{field: value})


def test_daemon_request_parsers_normalize_utc_bounds_to_epoch_seconds() -> None:
    list_request = DaemonReadingService._parse_list_messages_request(
        {
            "dialog_id": 17,
            "since_utc": "2026-01-01T00:00:00.123456Z",
            "until_utc": "2026-01-01T00:00:01.999999+00:00",
        }
    )
    search_request = DaemonReadingService._parse_search_messages_request(
        {
            "query": "needle",
            "since_utc": "2026-01-01T00:00:00.000001Z",
            "until_utc": "2026-01-01T00:00:01.000000Z",
        }
    )

    assert (list_request.since_utc, list_request.until_utc) == (1767225600, 1767225601)
    assert (search_request.since_utc, search_request.until_utc) == (1767225600, 1767225601)
