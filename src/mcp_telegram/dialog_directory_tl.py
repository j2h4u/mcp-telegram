"""Raw TL adapter for the account-wide dialog directory.

Telethon's ``iter_dialogs`` wrappers deliberately do not appear here: the
directory needs the constructors, source order, and raw records to make an
honest continuation decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast

from telethon.tl import functions, types  # type: ignore[import-untyped]
from telethon.tl.types import (  # type: ignore[import-untyped]
    InputPeerChannel,
    InputPeerChat,
    InputPeerEmpty,
    InputPeerSelf,
    InputPeerUser,
)

type InputPeer = InputPeerChannel | InputPeerChat | InputPeerEmpty | InputPeerSelf | InputPeerUser


@dataclass(frozen=True, slots=True)
class DialogCursor:
    """The exact raw continuation identity required by ``messages.getDialogs``."""

    offset_date: datetime
    offset_id: int
    offset_peer: InputPeer


@dataclass(frozen=True, slots=True)
class RawDialogFact:
    """One normalized raw dialog and its optional matched top message."""

    dialog_id: int
    entity: object | None
    dialog: object
    top_message_date: datetime | None


@dataclass(frozen=True, slots=True)
class RawDialogPage:
    """A normalized response with an explicit completion or failure outcome."""

    kind: Literal["page", "terminal", "incomplete", "invalid", "not_modified"]
    facts: tuple[RawDialogFact, ...]
    cursor: DialogCursor | None
    reason: str | None = None


def empty_cursor() -> InputPeerEmpty:
    """Return the explicit initial offset peer required by Telegram."""
    return InputPeerEmpty()


def get_dialogs_request(cursor: DialogCursor | None) -> functions.messages.GetDialogsRequest:
    """Build the only ordinary account-wide directory request."""
    return functions.messages.GetDialogsRequest(
        offset_date=cursor.offset_date if cursor is not None else None,
        offset_id=cursor.offset_id if cursor is not None else 0,
        offset_peer=cursor.offset_peer if cursor is not None else empty_cursor(),
        limit=100,
        hash=0,
        exclude_pinned=True,
        folder_id=None,
    )


def get_pinned_dialogs_request(folder_id: int) -> functions.messages.GetPinnedDialogsRequest:
    """Build a pinned-source request for one of Telegram's peer folders."""
    if folder_id not in {0, 1}:
        raise ValueError("pinned dialog folder_id must be 0 or 1")
    return functions.messages.GetPinnedDialogsRequest(folder_id=folder_id)


def normalize_dialogs_response(  # noqa: PLR0911, PLR0912 - distinct TL failures have distinct durable outcomes
    response: object, prior_cursor: DialogCursor | None
) -> RawDialogPage:
    """Normalize one raw ordinary response without treating wrapper length as EOF."""
    if isinstance(response, types.messages.DialogsNotModified):
        return RawDialogPage("not_modified", (), None, "dialogs_not_modified_without_cache")
    if not isinstance(response, (types.messages.DialogsSlice, types.messages.Dialogs)):
        return RawDialogPage("invalid", (), None, f"unexpected_dialogs_response:{type(response).__name__}")

    dialogs = tuple(cast(list[object], response.dialogs))
    if not dialogs:
        return RawDialogPage("terminal", (), None)

    entities = _entity_map(response)
    messages = _message_map(response)
    facts: list[RawDialogFact] = []
    identities: dict[int, tuple[str, int]] = {}
    candidate: DialogCursor | None = None

    for raw_dialog in dialogs:
        if isinstance(raw_dialog, types.DialogFolder):
            continue
        if not isinstance(raw_dialog, types.Dialog):
            return RawDialogPage("invalid", tuple(facts), None, f"unexpected_dialog:{type(raw_dialog).__name__}")
        identity = _peer_identity(raw_dialog.peer)
        if identity is None:
            return RawDialogPage("invalid", tuple(facts), None, "unresolvable_dialog_peer")
        dialog_id = _canonical_dialog_id(identity)
        prior = identities.get(dialog_id)
        current = (identity[0], int(raw_dialog.top_message))
        if prior is not None:
            if prior == current:
                continue
            return RawDialogPage("invalid", tuple(facts), None, "conflicting_duplicate_dialog")
        identities[dialog_id] = current

        entity = entities.get(identity)
        message = messages.get((identity, int(raw_dialog.top_message)))
        message_date = getattr(message, "date", None) if message is not None else None
        if not isinstance(message_date, datetime):
            message_date = None
        facts.append(RawDialogFact(dialog_id, entity, raw_dialog, message_date))

        offset_peer = _input_peer_for(entity, identity)
        if message_date is not None and offset_peer is not None:
            # Source order is significant: the last row that has every
            # continuation fact is the only safe advancing cursor.
            candidate = DialogCursor(message_date, int(raw_dialog.top_message), offset_peer)

    if not facts:
        if isinstance(response, types.messages.Dialogs):
            return RawDialogPage("terminal", (), None)
        return RawDialogPage("incomplete", (), None, "stalled:missing_safe_cursor")
    if isinstance(response, types.messages.Dialogs):
        # A terminal constructor is authoritative without a continuation.
        # Missing entities, message dates, and input peers are row facts whose
        # availability is independent from catalog completeness.
        return RawDialogPage("terminal", tuple(facts), None)
    if candidate is None:
        return RawDialogPage("incomplete", tuple(facts), None, "stalled:missing_safe_cursor")
    if prior_cursor is not None and _cursor_key(candidate) == _cursor_key(prior_cursor):
        return RawDialogPage("incomplete", tuple(facts), None, "stalled:non_advancing_cursor")
    return RawDialogPage("page", tuple(facts), candidate)


def normalize_pinned_dialogs_response(response: object) -> RawDialogPage:
    """Normalize a pinned response while retaining raw source order.

    Pinned membership needs a canonical peer identity. Missing top-message
    objects do not make that membership unknowable, unlike ordinary paging.
    """
    if isinstance(response, types.messages.DialogsNotModified):
        return RawDialogPage("not_modified", (), None, "pinned_dialogs_not_modified_without_cache")
    if not isinstance(response, types.messages.PeerDialogs):
        return RawDialogPage("invalid", (), None, f"unexpected_pinned_response:{type(response).__name__}")
    entities = _entity_map(response)
    messages = _message_map(response)
    facts: list[RawDialogFact] = []
    identities: set[int] = set()
    for raw_dialog in cast(list[object], response.dialogs):
        if isinstance(raw_dialog, types.DialogFolder):
            continue
        if not isinstance(raw_dialog, types.Dialog):
            return RawDialogPage("invalid", tuple(facts), None, f"unexpected_dialog:{type(raw_dialog).__name__}")
        identity = _peer_identity(raw_dialog.peer)
        if identity is None:
            return RawDialogPage("invalid", tuple(facts), None, "unresolvable_dialog_peer")
        dialog_id = _canonical_dialog_id(identity)
        if dialog_id in identities:
            return RawDialogPage("invalid", tuple(facts), None, "duplicate_pinned_dialog")
        identities.add(dialog_id)
        entity = entities.get(identity)
        message = messages.get((identity, int(raw_dialog.top_message)))
        message_date = getattr(message, "date", None) if message is not None else None
        facts.append(
            RawDialogFact(
                dialog_id,
                entity,
                raw_dialog,
                message_date if isinstance(message_date, datetime) else None,
            )
        )
    return RawDialogPage("terminal", tuple(facts), None)


def _entity_map(
    response: types.messages.Dialogs | types.messages.DialogsSlice | types.messages.PeerDialogs,
) -> dict[tuple[str, int], object]:
    entities: dict[tuple[str, int], object] = {}
    for entity in [*response.users, *response.chats]:
        identity = _entity_identity(entity)
        if identity is not None:
            entities[identity] = entity
    return entities


def _message_map(
    response: types.messages.Dialogs | types.messages.DialogsSlice | types.messages.PeerDialogs,
) -> dict[tuple[tuple[str, int], int], object]:
    messages: dict[tuple[tuple[str, int], int], object] = {}
    for message in response.messages:
        identity = _peer_identity(getattr(message, "peer_id", None))
        message_id = getattr(message, "id", None)
        if identity is not None and isinstance(message_id, int):
            messages[(identity, message_id)] = message
    return messages


def _entity_identity(entity: object) -> tuple[str, int] | None:
    if isinstance(entity, types.User):
        return ("user", int(entity.id))
    if isinstance(entity, (types.Chat, types.ChatForbidden)):
        return ("chat", int(entity.id))
    if isinstance(entity, (types.Channel, types.ChannelForbidden)):
        return ("channel", int(entity.id))
    return None


def _peer_identity(peer: object) -> tuple[str, int] | None:
    if isinstance(peer, types.PeerUser):
        return ("user", int(peer.user_id))
    if isinstance(peer, types.PeerChat):
        return ("chat", int(peer.chat_id))
    if isinstance(peer, types.PeerChannel):
        return ("channel", int(peer.channel_id))
    return None


def _canonical_dialog_id(identity: tuple[str, int]) -> int:
    kind, raw_id = identity
    if kind == "user":
        return raw_id
    if kind == "chat":
        return -raw_id
    return -1_000_000_000_000 - raw_id


def _input_peer_for(  # noqa: PLR0911 - raw peer constructors have explicit security gates
    entity: object | None, identity: tuple[str, int]
) -> InputPeer | None:
    kind, raw_id = identity
    if kind == "user" and isinstance(entity, types.User):
        if entity.is_self:
            return InputPeerSelf()
        if entity.min or not isinstance(entity.access_hash, int) or isinstance(entity.access_hash, bool):
            return None
        return InputPeerUser(raw_id, entity.access_hash)
    if kind == "chat":
        return InputPeerChat(raw_id)
    if kind == "channel" and isinstance(entity, (types.Channel, types.ChannelForbidden)):
        if (
            getattr(entity, "min", False)
            or not isinstance(entity.access_hash, int)
            or isinstance(entity.access_hash, bool)
        ):
            return None
        return InputPeerChannel(raw_id, entity.access_hash)
    return None


def _cursor_key(cursor: DialogCursor) -> tuple[datetime, int, str, int, int]:
    peer = cursor.offset_peer
    if isinstance(peer, InputPeerUser):
        return (cursor.offset_date, cursor.offset_id, "user", peer.user_id, peer.access_hash)
    if isinstance(peer, InputPeerChat):
        return (cursor.offset_date, cursor.offset_id, "chat", peer.chat_id, 0)
    if isinstance(peer, InputPeerChannel):
        return (cursor.offset_date, cursor.offset_id, "channel", peer.channel_id, peer.access_hash)
    if isinstance(peer, InputPeerSelf):
        return (cursor.offset_date, cursor.offset_id, "self", 0, 0)
    return (cursor.offset_date, cursor.offset_id, "empty", 0, 0)
