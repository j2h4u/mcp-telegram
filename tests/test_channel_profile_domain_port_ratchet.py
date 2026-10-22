"""Ratchets for the channel profile application boundary."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from mcp_telegram.entity_profile.contracts import (
    ChannelContactOverlapObservation,
    ChannelProfileObservation,
    ProjectionStatus,
)
from mcp_telegram.entity_profile.ports import ChannelProfilePort
from mcp_telegram.entity_profile.refresh import EntityProfileDemandAdapter, RefreshLimits
from mcp_telegram.linked_chat_fact import LinkedChatState, read_fact
from mcp_telegram.telegram_demand import RpcAttemptBudget
from tests.helpers import FakeChannelProfilePort, bind_profile_test_account, in_memory_sync_db
from tests.test_entity_profile_progressive import _test_service


def test_channel_profile_port_has_independent_operations() -> None:
    assert hasattr(ChannelProfilePort, "fetch_channel_profile")
    assert hasattr(ChannelProfilePort, "fetch_channel_contact_overlap")


def test_application_service_has_no_channel_rpc_or_participant_iterator_symbols() -> None:
    source = (Path(__file__).parents[1] / "src/mcp_telegram/daemon_entity_info.py").read_text()
    for symbol in (
        "GetFullChannelRequest",
        "GetParticipantsRequest",
        "ChannelParticipantsContacts",
        "iter_participants",
    ):
        assert symbol not in source


@pytest.mark.asyncio
@pytest.mark.parametrize("linked_chat_id", (None, -1_000_000_000_456))
async def test_broadcast_link_is_not_written_to_legacy_entity_detail_payload(linked_chat_id: int | None) -> None:
    channel_id = -1_000_000_000_123
    port = FakeChannelProfilePort(
        ChannelProfileObservation(
            channel_id=channel_id,
            about="Durable channel facts",
            participants_count=10,
            linked_chat_id=linked_chat_id,
            pinned_msg_id=None,
            slow_mode_seconds=None,
            available_reactions={},
            current_photo=None,
            observation_started_at=100,
            observation_completed_at=100,
        ),
        ChannelContactOverlapObservation(channel_id, None, ProjectionStatus.UNAVAILABLE, "not_an_admin"),
    )
    conn = in_memory_sync_db()
    service = _test_service(conn, limits=RefreshLimits())
    bind_profile_test_account(service)
    service._deps = replace(service._deps, channel_profile_port=port, channel_reference_provider=port)
    try:
        await service._write_profiles(
            lambda profiles: profiles.save_core({"id": channel_id, "type": "channel", "name": "Channel"}, now=100)
        )
        await service._write_profiles(lambda profiles: profiles.mark_pending(channel_id, now=100))
        coordinator = service.refresh_coordinator
        assert coordinator is not None
        await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))

        row = cast(
            tuple[str] | None,
            conn.execute("SELECT detail_json FROM entity_details WHERE entity_id=?", (channel_id,)).fetchone(),
        )
        assert row is not None
        detail = cast(dict[str, object], json.loads(row[0]))
        assert detail["about"] == "Durable channel facts"
        assert "linked_chat_id" not in detail
        fact = read_fact(conn, channel_id)
        assert fact.state is (LinkedChatState.KNOWN_NONE if linked_chat_id is None else LinkedChatState.KNOWN_LINK)
        assert fact.linked_chat_id == linked_chat_id
        assert fact.resolved_at == 100
        result = await service.get_entity_info({"entity_id": channel_id})
        assert result["ok"] is True
        data = result["data"]
        assert isinstance(data, dict)
        assert data["linked_chat_id"] == linked_chat_id
        assert port.profile_calls == [channel_id]
        assert port.overlap_calls == []
    finally:
        await service.shutdown()
        conn.close()
