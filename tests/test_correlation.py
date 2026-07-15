"""Tests for request-correlation ID tracking (contextvars-based transport boundary)."""

from __future__ import annotations

import asyncio
import pytest

from mcp_telegram.correlation import correlation_context, current_correlation_ids, record_correlation_id


class TestCorrelationContext:
    def test_context_lifecycle(self) -> None:
        """correlation_context sets up and tears down isolated correlation state."""
        before = current_correlation_ids()
        assert before == (), "no IDs before context"

        with correlation_context():
            inner = current_correlation_ids()
            assert inner == (), "fresh context starts empty"

        after = current_correlation_ids()
        assert after == (), "context is cleaned up after exit"

    def test_lifecycle_does_not_leak(self) -> None:
        """Exiting correlation_context does not retain request IDs in outer scope."""
        with correlation_context():
            record_correlation_id("a")
            record_correlation_id("b")

        assert current_correlation_ids() == (), "no IDs after context exit"


class TestRecordCorrelationId:
    def test_records_within_active_context(self) -> None:
        with correlation_context():
            record_correlation_id("req-1")
            record_correlation_id("req-2")
            ids = current_correlation_ids()
            assert ids == ("req-1", "req-2"), "both IDs recorded in order"

    def test_silently_ignored_without_context(self) -> None:
        record_correlation_id("orphan")
        assert current_correlation_ids() == (), "no context → no recording"


class TestCurrentCorrelationIds:
    def test_returns_snapshot_tuple(self) -> None:
        with correlation_context():
            record_correlation_id("one")
            snapshot = current_correlation_ids()
            assert isinstance(snapshot, tuple), "returns an immutable tuple"
            assert snapshot == ("one",)

    def test_empty_snapshot_without_context(self) -> None:
        assert current_correlation_ids() == ()

    def test_empty_snapshot_in_fresh_context(self) -> None:
        with correlation_context():
            assert current_correlation_ids() == ()

    def test_snapshot_is_immutable(self) -> None:
        with correlation_context():
            record_correlation_id("id-1")
            snapshot = current_correlation_ids()
            with pytest.raises((TypeError, AttributeError)):
                snapshot.append("id-2")  # type: ignore[union-attr]


class TestConcurrentIsolation:
    """Verify that concurrent asyncio tasks do not observe each other's correlation IDs."""

    async def _task(self, name: str, results: dict[str, tuple[str, ...]]) -> None:
        with correlation_context():
            record_correlation_id(f"{name}-1")
            record_correlation_id(f"{name}-2")
            await asyncio.sleep(0)
            results[name] = current_correlation_ids()

    async def test_concurrent_contexts_are_isolated(self) -> None:
        results: dict[str, tuple[str, ...]] = {}
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._task("A", results))
            tg.create_task(self._task("B", results))
            tg.create_task(self._task("C", results))

        assert results["A"] == ("A-1", "A-2"), "task A isolated"
        assert results["B"] == ("B-1", "B-2"), "task B isolated"
        assert results["C"] == ("C-1", "C-2"), "task C isolated"
        # No task should see another's IDs
        for other in ("B-1", "B-2", "C-1", "C-2"):
            assert other not in results["A"], f"task A must not see {other}"


def test_context_manager_yields_none() -> None:
    """correlation_context is a context manager that yields None."""
    assert hasattr(correlation_context(), "__enter__") and hasattr(correlation_context(), "__exit__")
    with correlation_context() as val:
        assert val is None
