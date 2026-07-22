from __future__ import annotations

from mcp_telegram.correlation import correlation_context, current_correlation_ids, record_correlation_id


def test_no_context_record_is_noop() -> None:
    record_correlation_id("req-1")
    assert current_correlation_ids() == ()


def test_correlation_context_records_ids_in_order() -> None:
    with correlation_context():
        record_correlation_id("first")
        record_correlation_id("second")
        record_correlation_id("third")
        assert current_correlation_ids() == ("first", "second", "third")
    assert current_correlation_ids() == ()
