"""The durable executor retries missing facts and avoids false paired cursors."""

# SQLite row shapes are asserted below.
# pyright: reportAny=false

from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from mcp_telegram.daemon_entity_info import DaemonEntityInfoService
from mcp_telegram.entity_profile.contracts import (
    CommonChatsObservation,
    ProjectionStatus,
    UserAvatarHistoryObservation,
    UserReference,
)
from mcp_telegram.entity_profile.refresh import EntityProfileDemandAdapter
from mcp_telegram.sync_transactions import write_savepoint
from mcp_telegram.telegram_demand import RpcAttemptBudget
from mcp_telegram.telegram_rpc_scheduler import current_rpc_scope
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


@pytest.mark.asyncio
@pytest.mark.parametrize("section", ("common_chats", "avatar_history"))
async def test_executor_retries_first_timeout_of_missing_section(tmp_path: Path, section: str) -> None:
    conn, repo = _repository(tmp_path / "retry-executor.sqlite")
    _peer(conn, 42)
    _collected(conn, 42, owner=42)
    with write_savepoint(conn):
        conn.execute("DELETE FROM entity_detail_sections WHERE section=?", (section,))
    client = _PairClient(channel_id=None)
    service = cast(DaemonEntityInfoService, _pair_service(conn, client, enabled=True))
    facts = _RetryingFacts()
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
        await adapter.run_slice(RpcAttemptBudget(limit=1))
        assert facts.calls == 1
        state = conn.execute("SELECT status,retry_at,next_section FROM entity_profile_refresh_state").fetchone()
        assert state[0] == "failed" and state[1] > 1000 and state[2] == section
        await adapter.run_slice(RpcAttemptBudget(limit=1))
        assert facts.calls == 1
        clock[0] = float(state[1])
        await adapter.run_slice(RpcAttemptBudget(limit=1))
        assert facts.calls == 2
        assert conn.execute(
            "SELECT status,retry_at FROM entity_detail_sections WHERE section=?", (section,)
        ).fetchone() == ("fresh", None)
        for _ in range(5):
            await adapter.run_slice(RpcAttemptBudget(limit=1))
        assert repo.next_due_refresh(now=int(clock[0])) is None
        assert facts.calls == 2
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
        for _ in range(5):
            await adapter.run_slice(RpcAttemptBudget(limit=1))
        assert client.full_user_calls == 1
        assert repo.next_due_refresh(now=100_000) is None
        assert conn.execute(
            "SELECT status,observed_at FROM entity_detail_sections WHERE section='full_profile'"
        ).fetchone() == ("fresh", 100)
        assert conn.execute("SELECT generation,status FROM entity_profile_refresh_state").fetchone() == (2, "complete")
    finally:
        await service.shutdown()
        conn.close()
