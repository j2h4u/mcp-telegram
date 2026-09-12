"""Telethon adapter for the one folder-rule observation RPC."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Protocol, cast

from telethon import utils as telethon_utils  # type: ignore[import-untyped]
from telethon.errors import RPCError  # type: ignore[import-untyped]
from telethon.tl.functions.messages import GetDialogFiltersRequest  # type: ignore[import-untyped]
from telethon.tl.types import DialogFilter, DialogFilterChatlist, DialogFilterDefault  # type: ignore[import-untyped]

from ..flood import TelegramRpcThrottled
from .contracts import (
    DEFAULT_FOLDER_NAMESPACE,
    FILTER_FOLDER_NAMESPACE,
    DialogCategory,
    FolderRule,
    FolderRuleKind,
    FolderRuleObservation,
    FolderSourceUnavailableError,
)
from .ports import TelegramFolderGateway


class FolderClient(Protocol):
    async def __call__(self, request: object) -> object: ...


def _peer_ids(peers: object) -> tuple[int, ...]:
    if not isinstance(peers, (list, tuple)):
        return ()
    result: list[int] = []
    seen: set[int] = set()
    for peer in peers:
        peer_id = int(telethon_utils.get_peer_id(peer))
        if peer_id not in seen:
            seen.add(peer_id)
            result.append(peer_id)
    return tuple(result)


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
    kind = FolderRuleKind.CHATLIST if isinstance(folder, DialogFilterChatlist) or kind_name == DialogFilterChatlist.__name__ else FolderRuleKind.FILTER
    return FolderRule(
        _as_int(getattr(folder, "id", None)),
        title,
        FILTER_FOLDER_NAMESPACE,
        kind,
        position,
        _peer_ids(getattr(folder, "include_peers", ())),
        _peer_ids(getattr(folder, "pinned_peers", ())),
        _peer_ids(getattr(folder, "exclude_peers", ())),
        _categories(folder),
        bool(getattr(folder, "exclude_archived", False)),
        bool(getattr(folder, "exclude_read", False)),
        bool(getattr(folder, "exclude_muted", False)),
    )


def _observation_token(rules: tuple[FolderRule, ...]) -> str:
    payload = [
        (rule.namespace, rule.folder_id, rule.title, rule.kind.value, rule.source_position, rule.included_ids, rule.pinned_ids,
         rule.excluded_ids, sorted(category.value for category in rule.categories), rule.exclude_archived,
         rule.exclude_read, rule.exclude_muted)
        for rule in rules
    ]
    return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()


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
        rules = tuple(rule for position, item in enumerate(raw_filters) if (rule := _folder_rule(item, position)) is not None)
        return FolderRuleObservation(rules, _observation_token(rules), started_at)


def _as_int(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        return int(value)
    raise TypeError("Telegram folder id is invalid")
