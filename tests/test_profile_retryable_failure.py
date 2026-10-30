"""Only accepted failures from actual profile reads authorize bounded retries."""

from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from telethon.errors import RPCError

from mcp_telegram.daemon_entity_info import DaemonEntityInfoService
from mcp_telegram.entity_profile.contracts import TargetKind, UserProfileObservation
from mcp_telegram.entity_profile.refresh import EntityProfileDemandAdapter
from mcp_telegram.telegram_demand import DurableDemandAdapter
from mcp_telegram.telegram_demand_coordinator import TelegramDemandCoordinator
from mcp_telegram.telegram_demand_progress import SliceOutcomeKind
from mcp_telegram.telegram_rpc_consumers import DemandKind
from mcp_telegram.telegram_rpc_scheduler import current_rpc_scope
from tests.demand_adapter_helpers import run_ready_slice
from tests.test_entity_profile_full_user_pair import _pair_service, _PairClient, _prepare
from tests.test_telegram_demand_coordinator import _adapters, _Clock


@pytest.mark.asyncio
async def test_coordinator_retries_persisted_timeout_after_backoff(tmp_path: Path) -> None:
    conn, prior = _prepare(tmp_path / "coordinator.sqlite", migrated=True)
    await prior.shutdown()  # type: ignore[attr-defined]

    class FlakyClient(_PairClient):
        calls = 0

        async def fetch_user_profile(
            self, user_id: int, target_kind: TargetKind, *, owner_account_id: int | None = None
        ) -> UserProfileObservation:
            self.calls += 1
            if self.calls == 1:
                budget = current_rpc_scope().attempt_budget
                assert budget is not None
                budget.debit()
                raise TimeoutError("read timeout")
            return await super().fetch_user_profile(user_id, target_kind, owner_account_id=owner_account_id)

    client = FlakyClient()
    service = cast(DaemonEntityInfoService, _pair_service(conn, client, enabled=True))
    clock = _Clock()
    service._deps = replace(service._deps, now_provider=clock)
    refresh = service.refresh_coordinator
    assert refresh is not None
    adapters: dict[DemandKind, DurableDemandAdapter] = dict(_adapters())
    kind = DemandKind.ENTITY_PROFILE_REFRESH
    adapters[kind] = EntityProfileDemandAdapter(refresh)
    coordinator = TelegramDemandCoordinator(adapters, clock=clock, monotonic_clock=clock)
    try:
        coordinator.scan()
        selected = service._durable_refresh_status(100)
        assert selected is not None and selected.selected is not None
        revision = selected.selected
        await coordinator._execute_slice(kind)
        assert conn.execute("SELECT status,reason FROM entity_profile_refresh_state WHERE entity_id=42").fetchone() == (
            "failed",
            "timeouterror",
        )
        after_failure = service._durable_refresh_status(100)
        assert after_failure is not None and after_failure.selected == revision
        budget = coordinator._invocations[revision].budget
        assert budget.attempts == 1 and client.calls == 1
        coordinator._active_kind = None
        clock.value = 129
        coordinator.scan()
        assert kind not in coordinator.ready_kinds and client.calls == 1
        clock.value = 130
        coordinator.scan()
        assert kind in coordinator.ready_kinds
        await coordinator._execute_slice(kind)
        assert client.calls == 2 and budget.attempts == 2
        assert conn.execute("SELECT status FROM entity_profile_refresh_state WHERE entity_id=42").fetchone() != (
            "failed",
        )
    finally:
        await service.shutdown()
        conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (TimeoutError("read timeout"), SliceOutcomeKind.RETRYABLE_FAILURE),
        (ConnectionError("read disconnected"), SliceOutcomeKind.RETRYABLE_FAILURE),
        (OSError("read transport"), SliceOutcomeKind.RETRYABLE_FAILURE),
        (RPCError(None, "server", code=500), SliceOutcomeKind.RETRYABLE_FAILURE),
        (RPCError(None, "bad request", code=400), SliceOutcomeKind.DEFERRED),
        (ValueError("invalid response"), SliceOutcomeKind.DEFERRED),
    ],
)
async def test_full_user_failure_receipt_classifies_only_transient_reads(
    tmp_path: Path, error: Exception, expected: SliceOutcomeKind
) -> None:
    conn, prior = _prepare(tmp_path / "profile.sqlite", migrated=True)
    await prior.shutdown()  # type: ignore[attr-defined]

    class FailingClient(_PairClient):
        async def fetch_user_profile(
            self, user_id: int, target_kind: TargetKind, *, owner_account_id: int | None = None
        ) -> UserProfileObservation:
            budget = current_rpc_scope().attempt_budget
            assert budget is not None
            budget.debit()
            raise error

    service = cast(DaemonEntityInfoService, _pair_service(conn, FailingClient(), enabled=True))
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    try:
        outcome = await run_ready_slice(EntityProfileDemandAdapter(coordinator), now=100)
        assert outcome is not None and outcome.kind is expected, conn.execute(
            "SELECT status,reason,next_section,generation,profile_revision FROM entity_profile_refresh_state"
        ).fetchall()
        assert outcome.committed_revision is None
        assert conn.execute("SELECT status,reason FROM entity_profile_refresh_state WHERE entity_id=42").fetchone() == (
            "failed",
            type(error).__name__.lower(),
        )
    finally:
        await service.shutdown()
        conn.close()


@pytest.mark.asyncio
async def test_local_reservation_oserror_does_not_become_network_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, prior = _prepare(tmp_path / "local-error.sqlite", migrated=True)
    await prior.shutdown()  # type: ignore[attr-defined]
    service = cast(DaemonEntityInfoService, _pair_service(conn, _PairClient(), enabled=True))
    coordinator = service.refresh_coordinator
    assert coordinator is not None

    async def fail_local(_scope: object) -> int:
        raise OSError("local writer failure")

    monkeypatch.setattr(service, "_reserve_profile_observation", fail_local)
    try:
        outcome = await run_ready_slice(EntityProfileDemandAdapter(coordinator), now=100)
        assert outcome is not None and outcome.kind is SliceOutcomeKind.DEFERRED
    finally:
        await service.shutdown()
        conn.close()


@pytest.mark.asyncio
async def test_rejected_failure_cas_is_stale_not_retryable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mcp_telegram.entity_profile.repository import EntityProfileRepository

    conn, prior = _prepare(tmp_path / "rejected.sqlite", migrated=True)
    await prior.shutdown()  # type: ignore[attr-defined]

    class FailingClient(_PairClient):
        async def fetch_user_profile(
            self, user_id: int, target_kind: TargetKind, *, owner_account_id: int | None = None
        ) -> UserProfileObservation:
            raise TimeoutError("read timeout")

    monkeypatch.setattr(EntityProfileRepository, "mark_section_failure", lambda *_args, **_kwargs: False)
    service = cast(DaemonEntityInfoService, _pair_service(conn, FailingClient(), enabled=True))
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    try:
        outcome = await run_ready_slice(EntityProfileDemandAdapter(coordinator), now=100)
        assert outcome is not None and outcome.kind is SliceOutcomeKind.STALE
    finally:
        await service.shutdown()
        conn.close()


@pytest.mark.asyncio
async def test_deadline_expiring_inside_read_is_retryable(tmp_path: Path) -> None:
    import asyncio
    from dataclasses import replace

    conn, prior = _prepare(tmp_path / "deadline.sqlite", migrated=True)
    await prior.shutdown()  # type: ignore[attr-defined]

    class SlowClient(_PairClient):
        async def fetch_user_profile(
            self, user_id: int, target_kind: TargetKind, *, owner_account_id: int | None = None
        ) -> UserProfileObservation:
            await asyncio.sleep(10)
            raise AssertionError("read timeout should cancel the pending request")

    service = cast(DaemonEntityInfoService, _pair_service(conn, SlowClient(), enabled=True))
    service._deps = replace(
        service._deps,
        refresh_limits=replace(service._deps.refresh_limits, foreground_resolve_seconds=0.005, per_rpc_seconds=0.01),
    )
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    try:
        outcome = await run_ready_slice(EntityProfileDemandAdapter(coordinator), now=100)
        assert outcome is not None and outcome.kind is SliceOutcomeKind.RETRYABLE_FAILURE
    finally:
        await service.shutdown()
        conn.close()
