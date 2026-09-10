"""Telethon adapter for Telegram dialog-folder facts."""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator, Sequence
from typing import Protocol, cast

from telethon import utils as telethon_utils  # type: ignore[import-untyped]
from telethon.errors import RPCError  # type: ignore[import-untyped]
from telethon.tl.functions.messages import GetDialogFiltersRequest  # type: ignore[import-untyped]
from telethon.tl.types import (  # type: ignore[import-untyped]
    Channel,
    Chat,
    DialogFilter,
    DialogFilterChatlist,
    InputPeerChannel,
    InputPeerChat,
    InputPeerEmpty,
    InputPeerUser,
    User,
)

from ..flood import TelegramRpcThrottled
from .contracts import (
    DialogCategory,
    DialogFacts,
    FolderDialogCursor,
    FolderDialogItem,
    FolderRule,
    FolderSourceSnapshot,
    FolderSourceUnavailableError,
)
from .ports import TelegramFolderGateway

FOLDER_DIALOG_PAGE_SIZE = 100


class FolderClient(Protocol):
    async def __call__(self, request: object) -> object: ...
    def iter_dialogs(self, **kwargs: object) -> AsyncIterator[object]: ...


def _peer_ids(peers: object) -> frozenset[int]:
    if not isinstance(peers, (list, tuple)):
        return frozenset()
    return frozenset(int(telethon_utils.get_peer_id(peer)) for peer in peers)


def _is_muted(dialog: object) -> bool:
    notify = getattr(getattr(dialog, "dialog", None), "notify_settings", None)
    until = getattr(notify, "mute_until", None)
    if isinstance(until, dt.datetime):
        now = dt.datetime.now(tz=until.tzinfo) if until.tzinfo else dt.datetime.now(tz=dt.UTC).replace(tzinfo=None)
        return until > now
    return isinstance(until, int) and until > int(dt.datetime.now(tz=dt.UTC).timestamp())


def _category(entity: object) -> DialogCategory:
    kind = entity.__class__.__name__
    if isinstance(entity, User) or kind == User.__name__:
        return _user_category(entity)
    if isinstance(entity, Chat) or kind == Chat.__name__ or bool(getattr(entity, "megagroup", False)):
        return DialogCategory.GROUP
    if isinstance(entity, Channel) or kind == Channel.__name__:
        return DialogCategory.BROADCAST
    return DialogCategory.UNKNOWN


def _user_category(entity: object) -> DialogCategory:
    if bool(getattr(entity, "bot", False)):
        return DialogCategory.BOT
    if bool(getattr(entity, "contact", False) or getattr(entity, "mutual_contact", False)):
        return DialogCategory.CONTACT
    return DialogCategory.NON_CONTACT


def _folder_rule(folder: object) -> FolderRule:
    categories = frozenset(
        category
        for attribute, category in (
            ("contacts", DialogCategory.CONTACT),
            ("non_contacts", DialogCategory.NON_CONTACT),
            ("bots", DialogCategory.BOT),
            ("groups", DialogCategory.GROUP),
            ("broadcasts", DialogCategory.BROADCAST),
        )
        if bool(getattr(folder, attribute, False))
    )
    title_value = getattr(folder, "title", "")
    return FolderRule(
        folder_id=int(folder.id),  # type: ignore[attr-defined]
        title=str(getattr(title_value, "text", title_value)),
        included_ids=_peer_ids(getattr(folder, "include_peers", ())),
        pinned_ids=_peer_ids(getattr(folder, "pinned_peers", ())),
        excluded_ids=_peer_ids(getattr(folder, "exclude_peers", ())),
        categories=categories,
        exclude_archived=bool(getattr(folder, "exclude_archived", False)),
        exclude_read=bool(getattr(folder, "exclude_read", False)),
        exclude_muted=bool(getattr(folder, "exclude_muted", False)),
        explicit_only=isinstance(folder, DialogFilterChatlist)
        or folder.__class__.__name__ == DialogFilterChatlist.__name__,
    )


def _dialog_facts(dialog: object) -> DialogFacts:
    raw_dialog = getattr(dialog, "dialog", None)
    unread = bool(
        int(getattr(dialog, "unread_count", 0) or 0)
        or int(getattr(dialog, "unread_mentions_count", 0) or 0)
        or bool(getattr(raw_dialog, "unread_mark", False))
    )
    return DialogFacts(
        dialog_id=int(getattr(dialog, "id", 0) or 0),
        category=_category(getattr(dialog, "entity", None)),
        archived=bool(getattr(dialog, "archived", False)),
        unread=unread,
        muted=_is_muted(dialog),
    )


def _dialog_cursor(dialog: object) -> FolderDialogCursor:
    entity = getattr(dialog, "entity", None)
    entity_id = int(getattr(entity, "id", 0) or 0)
    access_hash = int(getattr(entity, "access_hash", 0) or 0)
    peer_type: str | None = None
    entity_type = entity.__class__.__name__ if entity is not None else None
    if isinstance(entity, User) or entity_type == User.__name__:
        peer_type = "user"
    elif isinstance(entity, Chat) or entity_type == Chat.__name__:
        peer_type = "chat"
    elif isinstance(entity, Channel) or entity_type == Channel.__name__:
        peer_type = "channel"
    message = getattr(dialog, "message", None)
    date = getattr(message, "date", None) or getattr(dialog, "date", None)
    return FolderDialogCursor(
        offset_date=date.isoformat() if isinstance(date, dt.datetime) else None,
        offset_id=int(getattr(message, "id", 0) or 0),
        offset_peer_type=peer_type,
        offset_peer_id=entity_id,
        offset_peer_access_hash=access_hash,
    )


def _offset_peer(cursor: FolderDialogCursor) -> object:
    if cursor.offset_peer_type == "user":
        return InputPeerUser(cursor.offset_peer_id, cursor.offset_peer_access_hash)
    if cursor.offset_peer_type == "chat":
        return InputPeerChat(cursor.offset_peer_id)
    if cursor.offset_peer_type == "channel":
        return InputPeerChannel(cursor.offset_peer_id, cursor.offset_peer_access_hash)
    return InputPeerEmpty()


class TelethonTelegramFolderGateway(TelegramFolderGateway):
    """Folder adapter that inherits the caller's reconciliation RPC scope."""

    def __init__(self, client: FolderClient) -> None:
        self._client = client

    async def fetch_folders(self) -> tuple[FolderRule, ...]:
        try:
            response = await self._client(GetDialogFiltersRequest())
        except TelegramRpcThrottled:
            raise
        except (RPCError, TimeoutError, OSError) as exc:
            raise FolderSourceUnavailableError("Telegram folder source is unavailable") from exc

        raw_filters = cast(Sequence[object], getattr(response, "filters", ()))
        names = {DialogFilter.__name__, DialogFilterChatlist.__name__}
        return tuple(
            _folder_rule(item)
            for item in raw_filters
            if isinstance(item, (DialogFilter, DialogFilterChatlist)) or item.__class__.__name__ in names
        )

    async def iter_dialogs(self, cursor: FolderDialogCursor | None) -> AsyncIterator[FolderDialogItem]:
        options: dict[str, object] = {"limit": FOLDER_DIALOG_PAGE_SIZE, "ignore_pinned": True}
        if cursor is not None:
            options.update(
                {
                    "offset_date": dt.datetime.fromisoformat(cursor.offset_date) if cursor.offset_date else None,
                    "offset_id": cursor.offset_id,
                    "offset_peer": _offset_peer(cursor),
                }
            )
        try:
            async for dialog in self._client.iter_dialogs(**options):
                yield FolderDialogItem(_dialog_facts(dialog), _dialog_cursor(dialog))
        except TelegramRpcThrottled:
            raise
        except (RPCError, TimeoutError, OSError) as exc:
            raise FolderSourceUnavailableError("Telegram folder source is unavailable") from exc

    async def fetch_snapshot(self) -> FolderSourceSnapshot:
        """Acquire a complete snapshot for explicit maintenance callers and tests."""
        folders = await self.fetch_folders()
        dialogs: list[DialogFacts] = []
        cursor: FolderDialogCursor | None = None
        while True:
            page = [item async for item in self.iter_dialogs(cursor)]
            if not page:
                break
            dialogs.extend(item.facts for item in page)
            if len(page) < FOLDER_DIALOG_PAGE_SIZE:
                break
            next_cursor = page[-1].cursor
            if next_cursor == cursor:
                raise FolderSourceUnavailableError("Telegram folder dialog cursor did not advance")
            cursor = next_cursor
        return FolderSourceSnapshot(folders=folders, dialogs=tuple(dialogs))
