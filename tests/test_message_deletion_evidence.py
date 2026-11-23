from telethon.tl import types
from telethon.tl.types import messages

from mcp_telegram.message_deletion_evidence import classify_channel_lookup


def test_channel_deletions_require_id_evidence_and_same_channel_control() -> None:
    peer = types.PeerChannel(42)
    control = types.Message(90, peer, None, "")
    response = messages.Messages([types.MessageEmpty(2, None), control, types.MessageEmpty(1, peer)], [], [], [])
    result = classify_channel_lookup(response, -1000000000042, frozenset({1, 2, 3}), 90)
    assert result.deleted_ids == {1, 2}
    assert result.surviving_messages == (control,)


def test_ambiguous_or_unverified_observations_do_not_delete() -> None:
    peer = types.PeerChannel(42)
    wrong = types.PeerChannel(43)
    control = types.Message(90, peer, None, "")
    duplicate = messages.Messages([control, types.MessageEmpty(1, None), types.MessageEmpty(1, None)], [], [], [])
    wrong_peer = messages.Messages([control, types.MessageEmpty(1, wrong)], [], [], [])
    no_control = messages.Messages([types.MessageEmpty(1, None)], [], [], [])
    for response in (duplicate, wrong_peer, no_control, messages.MessagesNotModified(2)):
        assert not classify_channel_lookup(response, -1000000000042, frozenset({1}), 90).deleted_ids


def test_dm_lookup_uses_ids_and_requires_unique_same_peer_control() -> None:
    from mcp_telegram.message_deletion_evidence import classify_dm_lookup

    control = types.Message(90, types.PeerUser(42), None, "")
    empty = types.MessageEmpty(20, None)
    response = messages.Messages([empty, control], [], [], [])
    assert classify_dm_lookup(response, 42, frozenset({10, 20, 90})).deleted_ids == {20}
    duplicate = messages.Messages([empty, empty, control], [], [], [])
    assert not classify_dm_lookup(duplicate, 42, frozenset({20, 90})).deleted_ids
    assert not classify_dm_lookup(response, 43, frozenset({20, 90})).deleted_ids
    assert not classify_dm_lookup(messages.Messages([empty], [], [], []), 42, frozenset({20})).deleted_ids
