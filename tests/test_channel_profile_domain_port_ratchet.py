"""Ratchets for the channel profile application boundary."""

from __future__ import annotations

from pathlib import Path

from mcp_telegram.entity_profile.ports import ChannelProfilePort


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


def test_broadcast_link_is_not_written_to_legacy_entity_detail_payload() -> None:
    source = (Path(__file__).parents[1] / "src/mcp_telegram/daemon_entity_info.py").read_text()
    assert 'key != "linked_chat_id"' in source
