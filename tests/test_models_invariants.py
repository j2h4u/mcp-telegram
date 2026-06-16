"""Invariant tests for data-layer contracts."""

from mcp_telegram.daemon_api import _assert_select_columns_match_read_message


def test_list_messages_columns_match_read_message() -> None:
    """Column aliases in _LIST_MESSAGES_BASE_SQL must cover all
    ReadMessage fields except the two post-query-injected ones.
    Fails at import time in production; this test makes the
    invariant visible in the test report."""
    _assert_select_columns_match_read_message()  # raises AssertionError if broken
