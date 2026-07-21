from __future__ import annotations

import pytest

from mcp_telegram.tools.unread import _message_date


@pytest.mark.parametrize(
    ("sent_at", "expected"),
    [
        (1_700_000_000, 1_700_000_000),
        ("1700000000", 1_700_000_000),
        (1_700_000_000.9, 1_700_000_000),
        (b"1700000000", 1_700_000_000),
        (None, None),
        (True, None),
        (object(), None),
        ([], None),
    ],
)
def test_message_date_accepts_epoch_inputs_and_rejects_unsupported_values(
    sent_at: object,
    expected: int | None,
) -> None:
    assert _message_date(sent_at) == expected
