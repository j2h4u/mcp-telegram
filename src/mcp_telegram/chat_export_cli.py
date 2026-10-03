"""Stream a current Telegram group export through finite daemon requests."""

import asyncio
import json
import math
import os
import sys
import tempfile
import time
from collections import OrderedDict
from contextlib import suppress
from pathlib import Path
from typing import TextIO, cast

from .chat_export_projection import (
    clean_facts as _facts,
)
from .chat_export_projection import (
    project_admin_event,
    project_group,
    project_message,
    project_reactor,
)
from .daemon_client import daemon_connection

MIN_RETRY_SECONDS = 0.1
ROLE_CACHE_SIZE = 512
TOPIC_CACHE_SIZE = 64
type Payload = dict[str, object]


class ChatExportError(RuntimeError):
    """A failed export; no completed destination is published."""


def _object(value: object) -> Payload:
    if not isinstance(value, dict):
        raise ChatExportError("Malformed export response: expected object")
    return cast(Payload, value)


def _integer(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ChatExportError("Malformed export response: expected nonnegative integer")
    return value


def _group_id(value: object) -> int:
    if type(value) is not int or value >= 0:
        raise ChatExportError("Malformed export group identity: expected negative dialog_id")
    return value


def _records(value: object) -> list[Payload]:
    if not isinstance(value, list):
        raise ChatExportError("Malformed export response: expected items array")
    return [_object(item) for item in cast(list[object], value)]


def _items(data: Payload) -> list[Payload]:
    if type(data.get("done")) is not bool:
        raise ChatExportError("Malformed export response: expected done flag")
    return _records(data.get("items"))


def _dump(stream: TextIO, value: object) -> None:
    json.dump(value, stream, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _field(stream: TextIO, name: str, value: object) -> None:
    _dump(stream, name)
    stream.write(":")
    _dump(stream, value)


def _retry_delay(response: Payload) -> float:
    delay = response.get("retry_after")
    if not isinstance(delay, (int, float)) or isinstance(delay, bool):
        raise ChatExportError("Malformed export deferral: expected numeric retry_after")
    if not math.isfinite(delay) or delay < MIN_RETRY_SECONDS:
        raise ChatExportError("Malformed export deferral: retry_after must be finite and >= 0.1")
    return float(delay)


def _advance(page: Payload, incoming: int, last: int, upper: int | None = None) -> int:
    next_before = _integer(page.get("next_before_id"))
    if next_before == 0 or (incoming and next_before >= incoming):
        raise ChatExportError("Export pagination stalled")
    if last and next_before > last:
        raise ChatExportError("Export pagination skipped delivered messages")
    if upper is not None and next_before > upper:
        raise ChatExportError("Export pagination out of frozen boundary")
    return next_before


def _ordered_id(item: Payload, before: int, upper: int | None = None) -> int:
    identifier = _integer(item.get("id"))
    if identifier == 0 or (before and identifier >= before):
        raise ChatExportError("Export pagination out of order")
    if upper is not None and identifier > upper:
        raise ChatExportError("History pagination out of frozen boundary")
    return identifier


def _hint(peers: list[Payload]) -> int | None:
    total = 0
    for peer in peers:
        value = peer.get("total_messages")
        if peer.get("total_kind") != "estimated" or type(value) is not int or value < 0:
            return None
        total += _integer(value)
    return total


def _cache_put(cache: OrderedDict[tuple[int, int], Payload], key: tuple[int, int], data: Payload, size: int) -> None:
    cache[key] = data
    if len(cache) > size:
        cache.popitem(last=False)


def _status(data: Payload, allowed: set[str]) -> str:
    value = data.get("status")
    if not isinstance(value, str) or value not in allowed:
        raise ChatExportError("Malformed export enrichment status")
    return value


class _Export:
    """Per-invocation counters, bounded caches and streaming operations."""

    def __init__(self, dialog_id: int | str) -> None:
        self.selector = dialog_id
        self.dialog_id = dialog_id if isinstance(dialog_id, int) else 0
        self.clock = time.monotonic()
        self.counts = {"messages": 0, "admin_events": 0, "reactors": 0, "enrichments": 0}
        self.roles: OrderedDict[tuple[int, int], Payload] = OrderedDict()
        self.topics: OrderedDict[tuple[int, int], Payload] = OrderedDict()
        self.total_hint: int | None = None
        self.history_finished = False
        self.stage = "open"
        self.wait_reason: str | None = None
        self.retry_at = 0.0

    def progress(self) -> None:
        elapsed = time.monotonic() - self.clock
        rate = self.counts["messages"] / elapsed if elapsed else 0
        detail = f"; stage {self.stage}; enrichments {self.counts['enrichments']}"
        if self.wait_reason is not None:
            detail += f"; waiting: {self.wait_reason}; retry in {max(0, self.retry_at - time.monotonic()):.0f}s"
        if not self.history_finished and self.total_hint is not None and rate > 0:
            detail += f"; approximate history ETA {max(0, self.total_hint - self.counts['messages']) / rate:.0f}s"
        print(
            f"Export: {self.counts['messages']} messages; {rate:.1f}/s; elapsed {elapsed:.0f}s{detail}", file=sys.stderr
        )

    async def ticker(self) -> None:
        while True:
            await asyncio.sleep(10)
            self.progress()

    async def defer(self, response: Payload) -> None:
        delay = _retry_delay(response)
        self.wait_reason = str(response.get("reason", response.get("message", "Telegram RPC deferred")))
        self.retry_at = time.monotonic() + delay
        self.progress()
        await asyncio.sleep(delay)
        self.wait_reason = None

    async def request(self, operation: str, peer_id: int | str, **kwargs: object) -> Payload:
        self.stage = operation
        while True:
            async with daemon_connection(timeout_seconds=75) as conn:
                response = _object(
                    await conn.request(
                        {"method": "export_chat", "operation": operation, "dialog_id": peer_id, **kwargs}
                    )
                )
            if response.get("ok") is True:
                return _object(response.get("data"))
            if response.get("error") != "export_deferred":
                raise ChatExportError(
                    f"{operation}: {response.get('error', 'invalid_response')}: "
                    f"{response.get('message', 'export failed')}"
                )
            await self.defer(response)

    async def role(self, peer_id: int, user_id: int) -> Payload:
        key = (peer_id, user_id)
        if key not in self.roles:
            data = await self.request("participant", peer_id, user_id=user_id)
            _status(data, {"complete", "unavailable"})
            _cache_put(self.roles, key, data, ROLE_CACHE_SIZE)
            self.counts["enrichments"] += 1
        self.roles.move_to_end(key)
        return self.roles[key]

    async def identity(self, identity: object, peer_id: int) -> Payload | None:
        if identity is None:
            return None
        result = _facts(_object(identity))
        if result.get("kind") != "user":
            return result
        data = await self.role(self.dialog_id, _integer(result.get("id")))
        participant = data.get("participant")
        if participant is not None:
            result.update(_facts(_object(participant)))
        elif data["status"] == "complete":
            raise ChatExportError("Malformed complete participant")
        if data["status"] == "unavailable":
            result.update({"role": None, "is_admin": None})
        return result

    async def related_users(self, item: Payload, peer_id: int) -> None:
        if "related_users" in item:
            item["related_users"] = [await self.identity(user, peer_id) for user in _records(item["related_users"])]

    async def topic(self, message: Payload, peer_id: int) -> None:
        topic_id = message.get("topic_id")
        if topic_id is None:
            return
        key = (peer_id, _integer(topic_id))
        if key not in self.topics:
            data = await self.request("topic", peer_id, topic_id=topic_id)
            _status(data, {"complete", "unavailable"})
            _cache_put(self.topics, key, data, TOPIC_CACHE_SIZE)
            self.counts["enrichments"] += 1
        self.topics.move_to_end(key)
        data = self.topics[key]
        topic = data.get("topic")
        message["topic"] = None if topic is None else _facts(_object(topic))

    async def reactor_items(self, stream: TextIO, page: Payload, peer_id: int, fetched: int) -> int:
        for raw in _records(page.get("items")):
            reactor = _facts(raw)
            reactor["peer"] = await self.identity(reactor.get("peer"), peer_id)
            if fetched:
                stream.write(",")
            _dump(stream, project_reactor(reactor))
            fetched += 1
            self.counts["reactors"] += 1
        return fetched

    async def reaction_pages(self, stream: TextIO, peer_id: int, message_id: int) -> None:
        offset = ""
        fetched = 0
        while True:
            page = await self.request("reactions", peer_id, message_id=message_id, offset=offset)
            status = _status(page, {"complete", "partial", "unavailable"})
            fetched = await self.reactor_items(stream, page, peer_id, fetched)
            next_offset = page.get("next_offset")
            if status == "unavailable" or next_offset is None:
                return
            if not isinstance(next_offset, str) or not next_offset or next_offset == offset:
                raise ChatExportError("Reaction pagination stalled")
            offset = next_offset

    async def reactions(self, stream: TextIO, reactions: Payload, peer_id: int, message_id: int) -> None:
        status = _status(reactions, {"known_empty", "unknown", "pending", "unavailable"})
        _field(stream, "reactions", _facts(reactions))
        stream.write(',"reactors":[')
        if status == "pending" and reactions.get("can_view_list") is True:
            await self.reaction_pages(stream, peer_id, message_id)
        stream.write("]")

    async def write_message(self, stream: TextIO, item: Payload, peer_id: int) -> None:
        message = _facts(item)
        message_id = _integer(message.get("id"))
        if message.get("dialog_id") != peer_id or message.get("kind") not in {"message", "service"}:
            raise ChatExportError("Malformed history message identity")
        _object(message.get("raw"))
        message["author"] = await self.identity(message.get("author"), peer_id)
        await self.related_users(message, peer_id)
        await self.topic(message, peer_id)
        reactions = _object(message.pop("reactions"))
        stream.write("{")
        for name, value in project_message(message).items():
            _field(stream, name, value)
            stream.write(",")
        await self.reactions(stream, reactions, peer_id, message_id)
        stream.write("}")
        self.counts["messages"] += 1

    async def open_peers(self) -> list[Payload]:
        peers: list[Payload] = []
        selector: int | str | None = self.selector
        seen: set[int] = set()
        while selector is not None:
            opened = await self.request("open", selector)
            peer_id = _group_id(_object(opened.get("group")).get("dialog_id"))
            if peer_id in seen:
                raise ChatExportError("Group migration cycle")
            seen.add(peer_id)
            _integer(opened.get("upper_id"))
            peers.append({**opened, "dialog_id": peer_id})
            predecessor = opened.get("migrated_from_dialog_id")
            selector = None if predecessor is None else _group_id(predecessor)
            if selector in seen:
                raise ChatExportError("Group migration cycle")
        self.dialog_id = _group_id(_object(peers[0]["group"]).get("dialog_id"))
        self.total_hint = _hint(peers)
        return peers

    async def history_peer(self, stream: TextIO, peer: Payload, first: bool) -> bool:
        before = 0
        peer_id = cast(int, peer["dialog_id"])
        upper = _integer(peer["upper_id"])
        while True:
            page = await self.request("history", peer_id, upper_id=upper, before_id=before)
            last = before
            for item in _items(page):
                last = _ordered_id(item, last, upper)
                if not first:
                    stream.write(",")
                await self.write_message(stream, item, peer_id)
                first = False
            if page["done"]:
                return first
            before = _advance(page, before, last, upper)

    async def admin_items(self, stream: TextIO, page: Payload, before: int, first: bool) -> tuple[int, bool]:
        last = before
        for raw in _items(page):
            event = _facts(raw)
            last = _ordered_id(event, last)
            event["actor"] = await self.identity(event.get("actor"), self.dialog_id)
            await self.related_users(event, self.dialog_id)
            if not first:
                stream.write(",")
            _dump(stream, project_admin_event(event, self.dialog_id))
            first = False
            self.counts["admin_events"] += 1
        return last, first

    async def admin_log(self, stream: TextIO) -> None:
        before = 0
        first = True
        while True:
            page = await self.request("admin_log", self.dialog_id, before_id=before)
            status = _status(page, {"complete", "unavailable"})
            if status == "unavailable":
                return
            last, first = await self.admin_items(stream, page, before, first)
            if page["done"]:
                return
            before = _advance(page, before, last)

    def summary(self) -> Payload:
        return {name: self.counts[name] for name in ("messages", "admin_events", "reactors")}

    async def write(self, stream: TextIO) -> Payload:
        peers = await self.open_peers()
        stream.write('{"format_version":1,"group":')
        _dump(stream, project_group(_object(peers[0]["group"])))
        stream.write(',"metadata":')
        _dump(
            stream,
            {
                "order": "newest_to_oldest within each peer; primary then migrated predecessors",
                "peers": [project_group(_object(peer["group"])) for peer in peers],
            },
        )
        stream.write(',"admin_events":[')
        await self.admin_log(stream)
        stream.write('],"messages":[')
        first = True
        for peer in peers:
            first = await self.history_peer(stream, peer, first)
        self.history_finished = True
        summary = self.summary()
        stream.write('],"export":')
        _dump(stream, summary)
        stream.write("}\n")
        stream.flush()
        os.fsync(stream.fileno())
        return summary


async def export_group(dialog_id: int | str, output: Path) -> Payload:
    """Write atomically without clobbering, returning the final export summary."""
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"Export destination already exists: {output}")
    export = _Export(dialog_id)
    descriptor, name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
    temporary = Path(name)
    print(f"Export temporary file (remove after a crash): {temporary}", file=sys.stderr)
    task = asyncio.create_task(export.ticker())
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            summary = await export.write(stream)
        os.link(temporary, output)
        export.progress()
        return summary
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        temporary.unlink(missing_ok=True)
