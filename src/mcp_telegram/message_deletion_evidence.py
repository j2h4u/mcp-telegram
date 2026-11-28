"""Classify bounded Telegram message lookups without positional assumptions."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from telethon.tl import types  # type: ignore[import-untyped]
from telethon.tl.types import messages as message_types  # type: ignore[import-untyped]
from telethon.utils import get_peer_id  # type: ignore[import-untyped]

from .image_authorization import ScopeToken

MAX_TARGETS = 99
CHANNEL_ID_OFFSET = 1_000_000_000_000
MAX_MESSAGE_ID = 2**31 - 1
MIN_DIALOG_ID = -(2**63)


def valid_channel_verification(dialog_id: int, target_ids: tuple[int, ...], control_id: int) -> bool:
    """Check the bounded request before reserving an observation."""
    if type(dialog_id) is not int or not MIN_DIALOG_ID <= dialog_id < -CHANNEL_ID_OFFSET:
        return False
    if not 0 < len(target_ids) <= MAX_TARGETS:
        return False
    if len(set(target_ids)) != len(target_ids) or control_id in target_ids:
        return False
    return all(type(item) is int and 0 < item <= MAX_MESSAGE_ID for item in (*target_ids, control_id))


@dataclass(frozen=True, slots=True)
class DeletionEvidence:
    deleted_ids: frozenset[int]
    surviving_messages: tuple[object, ...]


@dataclass(frozen=True, slots=True)
class DmScanReceipt:
    marked: int
    scope: ScopeToken
    claim_scope: Callable[[ScopeToken], bool]


def _unique_requested(items: Sequence[object], requested: frozenset[int]) -> dict[int, object]:
    seen: dict[int, object] = {}
    duplicates: set[int] = set()
    for item in items:
        item_id = getattr(item, "id", None)
        if type(item_id) is not int or item_id not in requested:
            continue
        if item_id in seen:
            duplicates.add(item_id)
        seen[item_id] = item
    return {key: value for key, value in seen.items() if key not in duplicates}


def _channel_evidence(seen: dict[int, object], dialog_id: int, target_ids: frozenset[int]) -> DeletionEvidence:
    deleted: set[int] = set()
    surviving: list[object] = []
    for item_id, item in seen.items():
        peer = getattr(item, "peer_id", None)
        if peer is not None and (not isinstance(peer, types.PeerChannel) or get_peer_id(peer) != dialog_id):
            continue
        if isinstance(item, types.MessageEmpty) and item_id in target_ids:
            deleted.add(item_id)
        elif isinstance(item, (types.Message, types.MessageService)) and peer is not None:
            surviving.append(item)
    return DeletionEvidence(frozenset(deleted), tuple(surviving))


def classify_channel_lookup(
    response: object, dialog_id: int, target_ids: frozenset[int], control_id: int
) -> DeletionEvidence:
    """Accept only unique, explicit empty messages backed by a same-channel control."""
    empty = DeletionEvidence(frozenset(), ())
    if not valid_channel_verification(dialog_id, tuple(target_ids), control_id):
        return empty
    if not isinstance(response, (message_types.Messages, message_types.MessagesSlice, message_types.ChannelMessages)):
        return empty
    seen = _unique_requested(response.messages, target_ids | {control_id})
    control = seen.get(control_id)
    if not isinstance(control, (types.Message, types.MessageService)):
        return empty
    if not isinstance(control.peer_id, types.PeerChannel) or get_peer_id(control.peer_id) != dialog_id:
        return empty
    return _channel_evidence(seen, dialog_id, target_ids)


def _dm_peer_matches(peer: object, dialog_id: int) -> bool:
    return isinstance(peer, types.PeerUser) and get_peer_id(peer) == dialog_id


def classify_dm_lookup(response: object, dialog_id: int, requested: frozenset[int]) -> DeletionEvidence:
    """Classify an explicit-ID DM response only when it contains a same-peer control."""
    empty = DeletionEvidence(frozenset(), ())
    if not isinstance(response, (message_types.Messages, message_types.MessagesSlice, message_types.ChannelMessages)):
        return empty
    seen = _unique_requested(response.messages, requested)
    surviving = tuple(
        item
        for item in seen.values()
        if isinstance(item, (types.Message, types.MessageService)) and _dm_peer_matches(item.peer_id, dialog_id)
    )
    if not surviving:
        return empty
    deleted = frozenset(
        item_id
        for item_id, item in seen.items()
        if isinstance(item, types.MessageEmpty) and (item.peer_id is None or _dm_peer_matches(item.peer_id, dialog_id))
    )
    return DeletionEvidence(deleted, surviving)
