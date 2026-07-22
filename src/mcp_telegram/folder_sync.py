"""Daemon-side refresh of Telegram custom dialog folders."""

from __future__ import annotations

import datetime as dt
import sqlite3
from collections.abc import AsyncIterator, Sequence
from typing import Protocol, cast

from telethon import utils as telethon_utils  # type: ignore[import-untyped]
from telethon.tl.functions.messages import GetDialogFiltersRequest  # type: ignore[import-untyped]
from telethon.tl.types import (  # type: ignore[import-untyped]
    Channel,
    Chat,
    DialogFilter,
    DialogFilterChatlist,
    User,
)

from .folder_store import replace_folder_snapshot


class FolderClient(Protocol):
    async def __call__(self, request: object) -> object: ...
    def iter_dialogs(self, **kwargs: object) -> AsyncIterator[object]: ...


def _title(value: object) -> str:
    text = getattr(value, "text", value)
    return str(text)


def _peer_ids(peers: object) -> set[int]:
    if not isinstance(peers, (list, tuple)):
        return set()
    return {int(telethon_utils.get_peer_id(peer)) for peer in peers}


def _is_muted(dialog: object) -> bool:
    notify = getattr(getattr(dialog, "dialog", None), "notify_settings", None)
    until = getattr(notify, "mute_until", None)
    if isinstance(until, dt.datetime):
        now = dt.datetime.now(tz=until.tzinfo) if until.tzinfo else dt.datetime.now(tz=dt.UTC).replace(tzinfo=None)
        return until > now
    return isinstance(until, int) and until > int(dt.datetime.now(tz=dt.UTC).timestamp())


def _user_category_match(folder: object, entity: object) -> bool:
    if bool(getattr(entity, "bot", False)):
        return bool(getattr(folder, "bots", False))
    is_contact = bool(getattr(entity, "contact", False) or getattr(entity, "mutual_contact", False))
    return bool(getattr(folder, "contacts" if is_contact else "non_contacts", False))


def _category_match(folder: object, dialog: object) -> bool:
    entity = cast(object | None, getattr(dialog, "entity", None))
    if entity is None:
        return False
    kind = entity.__class__.__name__
    if isinstance(entity, User) or kind == User.__name__:
        return _user_category_match(folder, entity)
    if isinstance(entity, Chat) or kind == Chat.__name__ or bool(getattr(entity, "megagroup", False)):
        return bool(getattr(folder, "groups", False))
    if isinstance(entity, Channel) or kind == Channel.__name__:
        return bool(getattr(folder, "broadcasts", False))
    return False


def _passes_dynamic_exclusions(folder: object, dialog: object) -> bool:
    if bool(getattr(folder, "exclude_archived", False)) and bool(getattr(dialog, "archived", False)):
        return False
    unread = int(getattr(dialog, "unread_count", 0) or 0) or int(getattr(dialog, "unread_mentions_count", 0) or 0)
    if bool(getattr(folder, "exclude_read", False)) and not unread:
        return False
    return not (bool(getattr(folder, "exclude_muted", False)) and _is_muted(dialog))


def _matches(folder: object, dialog: object) -> bool:
    dialog_id = int(dialog.id)  # type: ignore[attr-defined]
    excluded = _peer_ids(getattr(folder, "exclude_peers", ()))
    if dialog_id in excluded:
        return False
    included = _peer_ids(getattr(folder, "include_peers", ())) | _peer_ids(getattr(folder, "pinned_peers", ()))
    if dialog_id in included:
        return True
    if isinstance(folder, DialogFilterChatlist) or folder.__class__.__name__ == DialogFilterChatlist.__name__:
        return False
    if not _category_match(folder, dialog):
        return False
    return _passes_dynamic_exclusions(folder, dialog)


async def refresh_folder_snapshot(conn: sqlite3.Connection, client: FolderClient) -> None:
    """Fetch filters and dialogs, compute Telegram rules, replace one DB snapshot."""
    response = await client(GetDialogFiltersRequest())
    raw_filters = cast(Sequence[object], getattr(response, "filters", ()))
    filter_names = {DialogFilter.__name__, DialogFilterChatlist.__name__}
    filters = [
        item
        for item in raw_filters
        if isinstance(item, (DialogFilter, DialogFilterChatlist)) or item.__class__.__name__ in filter_names
    ]
    dialogs = [dialog async for dialog in client.iter_dialogs()]
    folders = [(int(item.id), _title(getattr(item, "title", ""))) for item in filters]  # type: ignore[attr-defined]
    memberships = [
        (int(folder.id), int(dialog.id))  # type: ignore[attr-defined]
        for folder in filters
        for dialog in dialogs
        if _matches(folder, dialog)
    ]
    replace_folder_snapshot(conn, folders, memberships)
