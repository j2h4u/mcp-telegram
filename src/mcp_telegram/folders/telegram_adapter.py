"""Telethon adapter for the one folder-rule observation RPC."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, cast

from telethon import utils as telethon_utils  # type: ignore[import-untyped]
from telethon.errors import RPCError  # type: ignore[import-untyped]
from telethon.tl import types as tl  # type: ignore[import-untyped]
from telethon.tl.functions.messages import GetDialogFiltersRequest  # type: ignore[import-untyped]
from telethon.tl.types import DialogFilter, DialogFilterChatlist, DialogFilterDefault  # type: ignore[import-untyped]

from ..flood import TelegramRpcThrottled
from ..message_composition import normalize_telegram_fact
from .contracts import (
    DEFAULT_FOLDER_NAMESPACE,
    FILTER_FOLDER_NAMESPACE,
    DialogCategory,
    FolderRule,
    FolderRuleKind,
    FolderRuleObservation,
    FolderRuleUpdate,
    FolderSourceUnavailableError,
)
from .contracts import (
    folder_observation_token as _observation_token,
)
from .ports import TelegramFolderGateway


class FolderClient(Protocol):
    async def __call__(self, request: object) -> object: ...


def _peer_ids(peers: object) -> tuple[int, ...]:
    if not isinstance(peers, (list, tuple)):
        raise TypeError("folder peer list is unavailable")
    return tuple(int(telethon_utils.get_peer_id(peer)) for peer in peers)


def _categories(folder: object) -> frozenset[DialogCategory]:
    return frozenset(
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


def _folder_rule(folder: object, position: int) -> FolderRule | None:
    title_value = getattr(folder, "title", "")
    title = str(getattr(title_value, "text", title_value))
    kind_name = folder.__class__.__name__
    if isinstance(folder, DialogFilterDefault) or kind_name == DialogFilterDefault.__name__:
        return FolderRule(0, title or "All chats", DEFAULT_FOLDER_NAMESPACE, FolderRuleKind.DEFAULT, position)
    known = {DialogFilter.__name__, DialogFilterChatlist.__name__}
    if not (isinstance(folder, (DialogFilter, DialogFilterChatlist)) or kind_name in known):
        return None
    kind = (
        FolderRuleKind.CHATLIST
        if isinstance(folder, DialogFilterChatlist) or kind_name == DialogFilterChatlist.__name__
        else FolderRuleKind.FILTER
    )
    folder_id = _as_int(getattr(folder, "id", None))
    if folder_id == 1:
        raise ValueError("custom filter id 1 is reserved for the Telegram archive folder")
    entities, complete = _title_entities(title_value)
    try:
        included = _peer_ids(getattr(folder, "include_peers", ()))
        pinned = _peer_ids(getattr(folder, "pinned_peers", ()))
        excluded = _peer_ids(getattr(folder, "exclude_peers", ()))
    except TypeError, ValueError:
        return None
    return FolderRule(
        folder_id,
        title,
        FILTER_FOLDER_NAMESPACE,
        kind,
        position,
        included,
        pinned,
        excluded,
        _categories(folder),
        bool(getattr(folder, "exclude_archived", False)),
        bool(getattr(folder, "exclude_read", False)),
        bool(getattr(folder, "exclude_muted", False)),
        entities,
        bool(getattr(folder, "title_noanimate", False)),
        getattr(folder, "emoticon", None),
        getattr(folder, "color", None),
        bool(getattr(folder, "has_my_invites", False)),
        complete,
    )


def folder_update_from_native(update: object) -> FolderRuleUpdate:
    if isinstance(update, tl.UpdateDialogFilter):
        rule = None if update.filter is None else _folder_rule(update.filter, 0)
        if update.filter is not None and (rule is None or not rule.complete or rule.folder_id != update.id):
            return FolderRuleUpdate("invalid")
        return FolderRuleUpdate("filter", folder_id=update.id, rule=rule)
    if isinstance(update, tl.UpdateDialogFilterOrder):
        return FolderRuleUpdate("order", order=tuple(update.order))
    return FolderRuleUpdate("invalid")


def _title_entities(title: object) -> tuple[tuple[dict[str, object], ...], bool]:
    result: list[dict[str, object]] = []
    for entity in cast(Sequence[object], getattr(title, "entities", ())):
        name = type(entity).__name__
        if (
            name == "MessageEntityUnknown"
            or not name.startswith("MessageEntity")
            or getattr(tl, name, None) is not type(entity)
        ):
            return tuple(result), False
        value = normalize_telegram_fact(entity)
        if not isinstance(value, dict) or not str(value.get("_", "")).startswith("MessageEntity"):
            return tuple(result), False
        if any(not isinstance(item, (str, int, bool, type(None))) for item in value.values()):
            return tuple(result), False
        result.append(value)
    return tuple(result), True


class TelethonTelegramFolderGateway(TelegramFolderGateway):
    """Reads rules only; canonical directory ownership stays outside folders."""

    def __init__(self, client: FolderClient) -> None:
        self._client = client

    async def fetch_rules(self, *, started_at: int) -> FolderRuleObservation:
        try:
            response = await self._client(GetDialogFiltersRequest())
        except TelegramRpcThrottled:
            raise
        except (RPCError, TimeoutError, OSError) as exc:
            raise FolderSourceUnavailableError("Telegram folder source is unavailable") from exc
        raw_filters = cast(Sequence[object], getattr(response, "filters", ()))
        rules = tuple(
            rule for position, item in enumerate(raw_filters) if (rule := _folder_rule(item, position)) is not None
        )
        complete = len(rules) == len(raw_filters) and all(rule.complete for rule in rules)
        return FolderRuleObservation(rules, _observation_token(rules), started_at, complete)


def _as_int(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        return int(value)
    raise TypeError("Telegram folder id is invalid")
