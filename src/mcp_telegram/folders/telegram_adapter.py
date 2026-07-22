"""Telethon adapter for Telegram dialog-folder facts."""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator, Sequence
from typing import Protocol, cast

from telethon import utils as telethon_utils  # type: ignore[import-untyped]
from telethon.tl.functions.messages import GetDialogFiltersRequest  # type: ignore[import-untyped]
from telethon.tl.types import Channel, Chat, DialogFilter, DialogFilterChatlist, User  # type: ignore[import-untyped]

from .contracts import DialogCategory, DialogFacts, FolderRule, FolderSourceSnapshot
from .ports import TelegramFolderGateway


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
        dialog_id=int(dialog.id),  # type: ignore[attr-defined]
        category=_category(getattr(dialog, "entity", None)),
        archived=bool(getattr(dialog, "archived", False)),
        unread=unread,
        muted=_is_muted(dialog),
    )


class TelethonTelegramFolderGateway(TelegramFolderGateway):
    def __init__(self, client: FolderClient) -> None:
        self._client = client

    async def fetch_snapshot(self) -> FolderSourceSnapshot:
        response = await self._client(GetDialogFiltersRequest())
        raw_filters = cast(Sequence[object], getattr(response, "filters", ()))
        names = {DialogFilter.__name__, DialogFilterChatlist.__name__}
        folders = tuple(
            _folder_rule(item)
            for item in raw_filters
            if isinstance(item, (DialogFilter, DialogFilterChatlist)) or item.__class__.__name__ in names
        )
        dialogs = [_dialog_facts(dialog) async for dialog in self._client.iter_dialogs()]
        return FolderSourceSnapshot(folders=folders, dialogs=tuple(dialogs))
