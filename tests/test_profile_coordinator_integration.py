"""Exercise an entity-profile slice under the process-wide demand root."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from mcp_telegram.entity_profile.contracts import TargetKind, UserProfileObservation
from mcp_telegram.entity_profile.refresh import EntityProfileDemandAdapter
from mcp_telegram.telegram_demand import DemandStatus, DurableDemandAdapter, RpcAttemptBudget, current_demand_token
from mcp_telegram.telegram_demand_coordinator import TelegramDemandCoordinator
from mcp_telegram.telegram_demand_progress import SliceOutcome
from mcp_telegram.telegram_rpc_consumers import DURABLE_DEMAND_ORDER, DemandKind
from tests.test_entity_profile_full_user_pair import _PairClient, _prepare


class _IdleAdapter:
    """Keep unrelated durable domains inert for this focused integration."""

    def status(self, _now: float, *, excluded: frozenset[object] = frozenset()) -> DemandStatus | None:
        del excluded
        return None

    async def run_slice(self, _budget: RpcAttemptBudget) -> SliceOutcome:
        raise AssertionError("an unrelated durable domain must stay idle")


class _CompletionRecorder:
    def __init__(self) -> None:
        self.outcomes: list[tuple[str, DemandKind]] = []

    def observe_demand(self, *, outcome: str, demand_kind: DemandKind, **_fields: object) -> None:
        self.outcomes.append((outcome, demand_kind))


@pytest.mark.asyncio
async def test_profile_slice_runs_under_real_coordinator_root_and_accepts_its_receipt(tmp_path: Path) -> None:
    conn, raw_service = _prepare(tmp_path / "profile-coordinator.sqlite", migrated=True)
    service = raw_service
    shutdown = asyncio.Event()
    client = cast(_PairClient, service._deps.client)  # type: ignore[attr-defined]
    fetch_user_profile = client.fetch_user_profile

    async def fetch_and_stop(
        user_id: int, target_kind: TargetKind, *, owner_account_id: int | None = None
    ) -> UserProfileObservation:
        token = current_demand_token()
        assert token.kind is DemandKind.ENTITY_PROFILE_REFRESH
        observation = await fetch_user_profile(user_id, target_kind, owner_account_id=owner_account_id)
        shutdown.set()
        return observation

    client.fetch_user_profile = fetch_and_stop  # type: ignore[method-assign]
    service._deps = replace(service._deps, client=client, user_profile_port=client)  # type: ignore[attr-defined]
    profile_adapter = EntityProfileDemandAdapter(service.refresh_coordinator)  # type: ignore[attr-defined]
    adapters: dict[DemandKind, DurableDemandAdapter] = {
        kind: profile_adapter
        if kind is DemandKind.ENTITY_PROFILE_REFRESH
        else cast(DurableDemandAdapter, _IdleAdapter())
        for kind in DURABLE_DEMAND_ORDER
    }
    recorder = _CompletionRecorder()
    telegram_coordinator = TelegramDemandCoordinator(adapters, shutdown, clock=lambda: 100.0, observer=recorder)

    try:
        assert telegram_coordinator.ready_kinds == (DemandKind.ENTITY_PROFILE_REFRESH,)
        await telegram_coordinator.run()

        detail = service._profiles.read(42, now=100)  # type: ignore[attr-defined]
        assert client.full_user_calls == 1
        assert detail is not None
        assert detail.detail["about"] == "about"
        persisted = cast(
            tuple[object] | None,
            conn.execute("SELECT detail_json FROM entity_details WHERE entity_id=42").fetchone(),
        )
        assert persisted is not None
        persisted_json = persisted[0]
        assert isinstance(persisted_json, str)
        assert json.loads(persisted_json)["about"] == "about"
        assert conn.execute(
            "SELECT status FROM entity_detail_sections WHERE entity_id=42 AND section='full_profile'"
        ).fetchone() == ("fresh",)
        assert recorder.outcomes == [
            ("selected", DemandKind.ENTITY_PROFILE_REFRESH),
            ("completed", DemandKind.ENTITY_PROFILE_REFRESH),
        ]
        assert telegram_coordinator.ready_kinds == ()
    finally:
        await service.shutdown()  # type: ignore[attr-defined]
        conn.close()
