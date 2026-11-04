"""Regression coverage for full-history scheduling across durable retry floors."""

import asyncio
from unittest.mock import patch

import pytest

from mcp_telegram.auth_scope import AUTH_SCOPE_VERSION, TelegramAuthScope
from mcp_telegram.config import AutomaticGroupHistoryConfig
from mcp_telegram.image_authorization import ScopeCell, ScopeToken
from mcp_telegram.message_history.contracts import (
    FullHistoryPage,
    HistoryAccessProbeResult,
    MessageHistoryUnavailableError,
)
from mcp_telegram.sync_worker import FullSyncDemandAdapter, FullSyncWorker
from mcp_telegram.telegram_demand import DemandStatus, DurableDemandAdapter, RpcAttemptBudget
from mcp_telegram.telegram_demand_coordinator import TelegramDemandCoordinator
from mcp_telegram.telegram_demand_progress import SliceOutcome, WorkRevision
from mcp_telegram.telegram_rpc_consumers import DURABLE_DEMAND_ORDER, DemandKind
from tests.history_enrollment_helpers import seed_full_history_enrollment
from tests.sqlite_writer_helpers import inline_writer
from tests.test_fact_demand_adapters import _hydration_db

_scope_cell = ScopeCell()
assert _scope_cell.publish(TelegramAuthScope(AUTH_SCOPE_VERSION, 1, 2, 99))


class _ProbeScope:
    def capture_scope(self) -> ScopeToken | None:
        return _scope_cell.capture()

    def claim_scope(self, token: ScopeToken) -> bool:
        return _scope_cell.claim(token)


class IdleAdapter:
    def status(self, now: float, *, excluded: frozenset[WorkRevision] = frozenset()) -> DemandStatus | None:
        return None

    async def run_slice(self, budget: RpcAttemptBudget) -> SliceOutcome:
        raise AssertionError("idle adapter was selected")


class UnusedPort:
    async def fetch_page(self, dialog_id: int, *, before_message_id: int) -> FullHistoryPage:
        raise AssertionError("selection performed a Telegram read")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "timeout", "unavailable"])
async def test_automatic_probe_retries_at_durable_floor_without_restart(failure: str) -> None:
    conn = _hydration_db()
    conn.execute("INSERT INTO dialogs(dialog_id,type,members,created) VALUES (501,'group',1,1)")
    conn.commit()
    now = [1000.0]
    retried, settled, timer, stop = (asyncio.Event() for _ in range(4))

    class Port:
        calls = 0

        async def fetch_page(self, dialog_id: int, *, before_message_id: int) -> FullHistoryPage:
            assert (dialog_id, before_message_id) == (501, 0)
            self.calls += 1
            if self.calls == 1:
                if failure == "timeout":
                    raise TimeoutError
                if failure == "unavailable":
                    raise MessageHistoryUnavailableError("temporary")
                return FullHistoryPage((), None, None)
            return FullHistoryPage((), 1, None)

    class Observer:
        def observe_demand(self, **event: object) -> None:
            if event.get("reason") == "retryable_failure":
                retried.set()
            if event.get("reason") == "no_progress":
                settled.set()

    async def wait_for_clock(_delay: float) -> None:
        await timer.wait()
        timer.clear()

    port = Port()
    worker = FullSyncWorker(
        port, conn, stop, automatic_group_history=AutomaticGroupHistoryConfig(), writer=inline_writer(conn)
    )
    adapter = FullSyncDemandAdapter(worker, account_id=1)
    adapters: dict[DemandKind, DurableDemandAdapter] = {kind: IdleAdapter() for kind in DURABLE_DEMAND_ORDER}
    adapters[DemandKind.FULL_SYNC_PAGE] = adapter
    task: asyncio.Task[None] | None = None
    try:
        with patch("mcp_telegram.sync_worker.time.time", side_effect=lambda: now[0]):
            coordinator = TelegramDemandCoordinator(
                adapters, stop, clock=lambda: now[0], monotonic_clock=lambda: now[0], observer=Observer()
            )
            selected = adapter.status(now[0])
            assert selected is not None
            with patch.object(coordinator, "_wait_for_wakeup", side_effect=wait_for_clock):
                task = asyncio.create_task(coordinator.run())
                await asyncio.wait_for(retried.wait(), 3)
                assert conn.execute(
                    "SELECT value FROM daemon_state WHERE key='automatic_group_history_retry:501'"
                ).fetchone() == ("1060",)
                now[0] = 1059
                assert not adapter.status(now[0]).is_ready(now[0])  # type: ignore[union-attr]
                assert port.calls == 1
                now[0] = 1061
                assert adapter.status(now[0]).selected == selected.selected  # type: ignore[union-attr]
                timer.set()
                await asyncio.wait_for(settled.wait(), 3)
                assert port.calls == 2
                assert conn.execute("SELECT enabled FROM full_history_enrollment WHERE dialog_id=501").fetchone() == (
                    1,
                )
                stop.set()
                timer.set()
                await asyncio.wait_for(task, 3)
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        conn.close()


def test_future_enrolled_head_does_not_hide_ready_group_or_peer() -> None:
    conn = _hydration_db()
    try:
        conn.execute("INSERT INTO dialogs(dialog_id,type,members,created) VALUES (501,'group',1,1)")
        conn.executemany(
            "INSERT INTO synced_dialogs(dialog_id,status,full_sync_retry_at) VALUES (?, 'syncing', ?)",
            [(502, 2000), (503, 0)],
        )
        for dialog_id in (502, 503):
            seed_full_history_enrollment(conn, dialog_id, enabled=True)
        conn.commit()
        worker = FullSyncWorker(
            UnusedPort(),
            conn,
            asyncio.Event(),
            automatic_group_history=AutomaticGroupHistoryConfig(),
            writer=inline_writer(conn),
        )
        adapter = FullSyncDemandAdapter(worker, account_id=1)
        with patch("mcp_telegram.sync_worker.time.time", return_value=1000):
            selected = adapter.status(1000)
            assert selected is not None and selected.selected is not None
            assert selected.selected.key.target == "dialog:503"
            conn.execute("UPDATE synced_dialogs SET status='synced',total_messages=1 WHERE dialog_id=503")
            conn.commit()
            selected = adapter.status(1000)
            assert selected is not None and selected.selected is not None
            assert selected.selected.key.target == "automatic-group:501"
    finally:
        conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(("total", "expected"), [(5000, (1,)), (5001, (0,)), (None, None)])
async def test_undecided_group_requires_fresh_bounded_probe(total: int | None, expected: tuple[int] | None) -> None:
    conn = _hydration_db()
    try:
        conn.execute("INSERT INTO dialogs(dialog_id,type,members,created) VALUES (501,'group',3,1)")
        conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES (501,'own_only')")
        conn.commit()

        class Port:
            async def fetch_page(self, dialog_id: int, *, before_message_id: int) -> FullHistoryPage:
                assert (dialog_id, before_message_id) == (501, 0)
                return FullHistoryPage((), total, None)

        worker = FullSyncWorker(
            Port(),
            conn,
            asyncio.Event(),
            automatic_group_history=AutomaticGroupHistoryConfig(),
            writer=inline_writer(conn),
        )
        adapter = FullSyncDemandAdapter(worker, account_id=1)
        with patch("mcp_telegram.sync_worker.time.time", return_value=1000):
            selected = adapter.status(1000)
            assert selected is not None and selected.selected is not None
            await adapter.run_slice(RpcAttemptBudget(limit=1, selected=selected.selected))
        assert conn.execute("SELECT enabled FROM full_history_enrollment WHERE dialog_id=501").fetchone() == expected
        if total is None:
            assert conn.execute(
                "SELECT value FROM daemon_state WHERE key='automatic_group_history_retry:501'"
            ).fetchone() == ("1060",)
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_new_automatic_retry_floor_is_rechecked_before_dispatch() -> None:
    conn = _hydration_db()
    try:
        conn.execute("INSERT INTO dialogs(dialog_id,type,members,created) VALUES (501,'group',3,1)")
        conn.commit()
        worker = FullSyncWorker(
            UnusedPort(),
            conn,
            asyncio.Event(),
            automatic_group_history=AutomaticGroupHistoryConfig(),
            writer=inline_writer(conn),
        )
        adapter = FullSyncDemandAdapter(worker, account_id=1)
        with patch("mcp_telegram.sync_worker.time.time", return_value=1000):
            status = adapter.status(1000)
            assert status is not None and status.selected is not None
            conn.execute("INSERT INTO daemon_state(key,value) VALUES ('automatic_group_history_retry:501','1060')")
            conn.commit()
            outcome = await adapter.run_slice(RpcAttemptBudget(limit=1, selected=status.selected))
        assert outcome.kind.value == "retryable_failure"
        assert conn.execute("SELECT COUNT(*) FROM full_history_enrollment").fetchone() == (0,)
    finally:
        conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["page", "total"])
@pytest.mark.parametrize("error_type", [TimeoutError, MessageHistoryUnavailableError])
async def test_existing_history_recovers_from_transient_without_new_revision(
    kind: str, error_type: type[Exception]
) -> None:
    conn = _hydration_db()
    conn.execute("UPDATE dialog_directory_state SET account_id=1 WHERE singleton=1")
    conn.execute(
        "INSERT INTO synced_dialogs(dialog_id,status,total_messages) VALUES (501,?,NULL)",
        ("syncing" if kind == "page" else "synced",),
    )
    seed_full_history_enrollment(conn, 501, enabled=True)
    conn.commit()
    now = [1000.0]
    retry, complete, timer, stop = (asyncio.Event() for _ in range(4))

    class Port(_ProbeScope):
        calls = 0

        async def fetch_page(self, dialog_id: int, *, before_message_id: int) -> FullHistoryPage:
            self.calls += 1
            if self.calls == 1:
                raise error_type("temporary")
            return FullHistoryPage((), 1, None)

        async def probe_total_messages(self, dialog_id: int) -> HistoryAccessProbeResult:
            self.calls += 1
            if self.calls == 1:
                raise error_type("temporary")
            return HistoryAccessProbeResult(1, ())

    class Observer:
        def observe_demand(self, **event: object) -> None:
            if event.get("reason") == "retryable_failure":
                retry.set()
            if event.get("outcome") == "completed":
                complete.set()

    async def wait_for_clock(_delay: float) -> None:
        await timer.wait()
        timer.clear()

    port = Port()
    worker = FullSyncWorker(port, conn, stop, total_messages_probe=port, writer=inline_writer(conn))
    adapter = FullSyncDemandAdapter(worker, account_id=1)
    adapters: dict[DemandKind, DurableDemandAdapter] = {item: IdleAdapter() for item in DURABLE_DEMAND_ORDER}
    adapters[DemandKind.FULL_SYNC_PAGE] = adapter
    task: asyncio.Task[None] | None = None
    try:
        with patch("mcp_telegram.sync_worker.time.time", side_effect=lambda: now[0]):
            coordinator = TelegramDemandCoordinator(
                adapters, stop, clock=lambda: now[0], monotonic_clock=lambda: now[0], observer=Observer()
            )
            selected = adapter.status(now[0])
            assert selected is not None
            with patch.object(coordinator, "_wait_for_wakeup", side_effect=wait_for_clock):
                task = asyncio.create_task(coordinator.run())
                await asyncio.wait_for(retry.wait(), 3)
                assert adapter.status(now[0]).selected == selected.selected  # type: ignore[union-attr]
                assert port.calls == 1
                now[0] = 1031
                timer.set()
                await asyncio.wait_for(complete.wait(), 3)
                assert port.calls == 2
                assert conn.execute("SELECT total_messages FROM synced_dialogs WHERE dialog_id=501").fetchone() == (1,)
                stop.set()
                timer.set()
                await asyncio.wait_for(task, 3)
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_explicit_decision_wins_during_probe(enabled: bool) -> None:
    from mcp_telegram.history_enrollment import disable_history, enable_history

    conn = _hydration_db()
    try:
        conn.execute("INSERT INTO dialogs(dialog_id,type,members,created) VALUES (501,'group',3,1)")
        conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES (501,'own_only')")
        conn.commit()

        class Port:
            async def fetch_page(self, dialog_id: int, *, before_message_id: int) -> FullHistoryPage:
                if enabled:
                    enable_history(conn, dialog_id, now=1000)
                else:
                    disable_history(conn, dialog_id, now=1000)
                return FullHistoryPage((), 1, None)

        worker = FullSyncWorker(
            Port(),
            conn,
            asyncio.Event(),
            automatic_group_history=AutomaticGroupHistoryConfig(),
            writer=inline_writer(conn),
        )
        adapter = FullSyncDemandAdapter(worker, account_id=1)
        with patch("mcp_telegram.sync_worker.time.time", return_value=1000):
            status = adapter.status(1000)
            assert status is not None and status.selected is not None
            await adapter.run_slice(RpcAttemptBudget(limit=1, selected=status.selected))
        assert conn.execute("SELECT enabled FROM full_history_enrollment WHERE dialog_id=501").fetchone() == (
            int(enabled),
        )
    finally:
        conn.close()


def test_ready_automatic_probe_precedes_ready_total_repair() -> None:
    conn = _hydration_db()
    try:
        conn.execute("INSERT INTO dialogs(dialog_id,type,members,created) VALUES (501,'group',3,1)")
        conn.execute("INSERT INTO synced_dialogs(dialog_id,status,total_messages) VALUES (502,'synced',NULL)")
        seed_full_history_enrollment(conn, 502, enabled=True)
        conn.execute("INSERT INTO daemon_state(key,value) VALUES ('automatic_group_history_retry:501','1060')")
        conn.commit()
        worker = FullSyncWorker(
            UnusedPort(),
            conn,
            asyncio.Event(),
            automatic_group_history=AutomaticGroupHistoryConfig(),
            writer=inline_writer(conn),
        )
        adapter = FullSyncDemandAdapter(worker, account_id=1)
        with patch("mcp_telegram.sync_worker.time.time", return_value=1061):
            selected = adapter.status(1061)
            assert selected is not None and selected.selected is not None
            assert selected.selected.key.target == "automatic-group:501"
            conn.execute("UPDATE daemon_state SET value='1100' WHERE key='automatic_group_history_retry:501'")
            conn.commit()
            selected = adapter.status(1061)
            assert selected is not None and selected.selected is not None
            assert selected.selected.key.target == "total:502"
    finally:
        conn.close()


def test_disabled_group_is_not_selected_for_automatic_probe() -> None:
    conn = _hydration_db()
    try:
        conn.execute("INSERT INTO dialogs(dialog_id,type,members,created) VALUES (501,'group',3,1)")
        conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES (501,'own_only')")
        seed_full_history_enrollment(conn, 501, enabled=False)
        conn.commit()
        worker = FullSyncWorker(
            UnusedPort(),
            conn,
            asyncio.Event(),
            automatic_group_history=AutomaticGroupHistoryConfig(),
            writer=inline_writer(conn),
        )
        adapter = FullSyncDemandAdapter(worker, account_id=1)
        with patch("mcp_telegram.sync_worker.time.time", return_value=1000):
            assert adapter.status(1000) is None
        assert conn.execute("SELECT enabled FROM full_history_enrollment WHERE dialog_id=501").fetchone() == (0,)
    finally:
        conn.close()
