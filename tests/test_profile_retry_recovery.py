"""The durable executor retries missing facts and avoids false paired cursors."""

# SQLite row shapes are asserted below.
# pyright: reportAny=false

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from mcp_telegram.daemon_entity_info import DaemonEntityInfoService
from mcp_telegram.entity_profile.contracts import (
    PROFILE_SECTIONS,
    CommonChatsObservation,
    ObservationBoundary,
    ProfileCollectionPage,
    ProjectionStatus,
    UserAvatarHistoryObservation,
    UserReference,
)
from mcp_telegram.entity_profile.refresh import EntityProfileDemandAdapter
from mcp_telegram.sync_transactions import write_savepoint
from mcp_telegram.telegram_rpc_scheduler import current_rpc_scope
from tests.demand_adapter_helpers import run_ready_slice
from tests.test_dm_profile_repository import _collected, _peer, _repository
from tests.test_entity_profile_full_user_pair import _pair_service, _PairClient


class _RetryingFacts:
    def __init__(self) -> None:
        self.calls = 0

    def _attempt(self) -> None:
        scope = current_rpc_scope()
        assert scope.attempt_budget is not None
        scope.attempt_budget.debit()
        self.calls += 1
        if self.calls == 1:
            raise TimeoutError("first missing-section acquisition timed out")

    async def fetch_common_chats(self, reference: UserReference) -> CommonChatsObservation:
        self._attempt()
        return CommonChatsObservation(reference.user_id, (), 0, ProjectionStatus.USABLE, None, 1000, 1000)

    async def fetch_user_avatar_history(self, reference: UserReference) -> UserAvatarHistoryObservation:
        self._attempt()
        return UserAvatarHistoryObservation(reference.user_id, (), 0, ProjectionStatus.USABLE, None, 1000, 1000)

    async def fetch_profile_collection_page(
        self, reference: UserReference, section: str, cursor: Mapping[str, object]
    ) -> ProfileCollectionPage:
        self._attempt()
        return ProfileCollectionPage(reference.user_id, section, (), ObservationBoundary(1000, 1000), 0, terminal=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("section", ("common_chats", "avatar_history"))
async def test_executor_persists_missing_section_failure_for_central_gate(tmp_path: Path, section: str) -> None:
    conn, repo = _repository(tmp_path / "retry-executor.sqlite")
    _peer(conn, 42)
    _collected(conn, 42, owner=42)
    with write_savepoint(conn):
        conn.execute("DELETE FROM entity_detail_sections WHERE section=?", (section,))
    client = _PairClient(channel_id=None)
    service = cast(DaemonEntityInfoService, _pair_service(conn, client, enabled=True))
    facts = _RetryingFacts()
    client.collection_port = facts
    clock = [1000.0]
    service._deps = replace(
        service._deps,
        now_provider=lambda: clock[0],
        common_chats_port=facts,
        user_avatar_history_port=facts,
    )
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    adapter = EntityProfileDemandAdapter(coordinator)
    repo.request_section_refresh(42, (section,), now=1000, pair_mode="enabled")
    try:
        await run_ready_slice(adapter, now=clock[0])
        assert facts.calls == 1
        state = conn.execute("SELECT status,retry_at,next_section FROM entity_profile_refresh_state").fetchone()
        assert state == ("failed", 1000, section)
        # This helper invokes one selected slice directly; retry eligibility is
        # owned by the central Gate, so a second direct call would bypass it.
        assert facts.calls == 1
        assert conn.execute(
            "SELECT status,retry_at FROM entity_detail_sections WHERE section=?", (section,)
        ).fetchone() == ("unavailable", 1000)
        assert repo.next_due_refresh(now=int(clock[0])) is not None
        assert client.full_user_calls == 0
    finally:
        await service.shutdown()
        conn.close()


@pytest.mark.asyncio
async def test_executor_acquires_only_missing_personal_channel_without_pair_loop(tmp_path: Path) -> None:
    conn, repo = _repository(tmp_path / "personal-executor.sqlite")
    _peer(conn, 42)
    _collected(conn, 42, owner=42)
    with write_savepoint(conn):
        conn.execute("DELETE FROM entity_detail_sections WHERE section='personal_channel'")
    client = _PairClient(channel_id=None)
    service = cast(DaemonEntityInfoService, _pair_service(conn, client, enabled=True))
    service._deps = replace(service._deps, now_provider=lambda: 100_000.0)
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    adapter = EntityProfileDemandAdapter(coordinator)
    repo.request_section_refresh(42, ("personal_channel",), now=100_000, pair_mode="enabled")
    try:
        for _ in range(len(PROFILE_SECTIONS)):
            await run_ready_slice(adapter, now=100_000.0)
        assert client.full_user_calls == 1
        assert repo.next_due_refresh(now=100_000) is None
        assert conn.execute(
            "SELECT status,observed_at FROM entity_detail_sections WHERE section='full_profile'"
        ).fetchone() == ("fresh", 100)
        assert conn.execute("SELECT generation,status FROM entity_profile_refresh_state").fetchone() == (2, "complete")
    finally:
        await service.shutdown()
        conn.close()
