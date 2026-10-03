"""Stream a current Telegram group export through finite daemon requests."""

import asyncio
import fcntl
import json
import logging
import math
import os
import re
import signal
import sys
import tempfile
import time
from collections import OrderedDict
from contextlib import suppress
from pathlib import Path
from typing import TextIO, cast

from .chat_export_checkpoint import ORDER, Checkpoint, CheckpointError, census, read_checkpoint_options
from .chat_export_checkpoint import fingerprint as _fingerprint
from .chat_export_projection import (
    clean_facts as _facts,
)
from .chat_export_projection import (
    project_admin_event,
    project_group,
    project_message,
    project_reactor,
)
from .daemon_client import DaemonNotRunningError, daemon_connection

MIN_RETRY_SECONDS = 0.1
ROLE_CACHE_SIZE = 512
TOPIC_CACHE_SIZE = 64
IPC_RETRIES = 3
_LOGGER = logging.getLogger(__name__)
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
    stream.write(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False))


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
        self.started_messages = 0
        self.stage = "open"
        self.wait_reason: str | None = None
        self.retry_at = 0.0

    def progress(self) -> None:
        elapsed = time.monotonic() - self.clock
        rate = (self.counts["messages"] - self.started_messages) / elapsed if elapsed else 0
        detail = (
            f"; stage {self.stage}; admin events {self.counts['admin_events']}"
            f"; enrichments {self.counts['enrichments']}"
        )
        if self.total_hint is not None:
            detail += f"; estimated history total {self.total_hint}"
        if self.wait_reason is not None:
            detail += f"; waiting: {self.wait_reason}; retry in {max(0, self.retry_at - time.monotonic()):.0f}s"
        if not self.history_finished and self.total_hint is not None and rate > 0:
            seconds = round(max(0, self.total_hint - self.counts["messages"]) / rate)
            hours, remainder = divmod(seconds, 3600)
            detail += f"; approximate history ETA {hours}h{remainder // 60:02d}m ({seconds}s)"
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
            response: Payload = {}
            for attempt in range(IPC_RETRIES + 1):
                started = time.monotonic()
                try:
                    async with daemon_connection(timeout_seconds=75) as conn:
                        response = _object(
                            await conn.request(
                                {"method": "export_chat", "operation": operation, "dialog_id": peer_id, **kwargs}
                            )
                        )
                    _LOGGER.info(
                        "Export RPC operation=%s attempt=%s elapsed=%.3fs result=%s",
                        operation,
                        attempt + 1,
                        time.monotonic() - started,
                        response.get("error", "ok"),
                    )
                    break
                except DaemonNotRunningError as exc:
                    _LOGGER.warning(
                        "Export RPC operation=%s attempt=%s elapsed=%.3fs error=%s",
                        operation,
                        attempt + 1,
                        time.monotonic() - started,
                        exc.kind,
                    )
                    if (
                        exc.kind not in {"response_timeout", "connect_timeout", "send_timeout", "connection_broken"}
                        or attempt == IPC_RETRIES
                    ):
                        raise
                    await self.defer({"retry_after": 2**attempt, "reason": f"IPC {exc.kind}"})
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
        status = _status(reactions, {"known_empty", "unknown", "pending", "unavailable", "complete"})
        items = reactions.pop("items", None)
        _field(stream, "reactions", _facts(reactions))
        stream.write(',"reactors":[')
        if status == "complete":
            await self.reactor_items(stream, {"items": items}, peer_id, 0)
        elif status == "pending" and reactions.get("can_view_list") is True:
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

    async def history_peer(self, checkpoint: Checkpoint, peer: Payload, minimum: int) -> None:
        peer_id = cast(int, peer["dialog_id"])
        key = f"history:{peer_id}"
        if checkpoint.state(key + ":done"):
            return
        before = cast(int, checkpoint.state(key) or 0)
        upper = _integer(peer["upper_id"])
        while True:
            page = await self.request("history", peer_id, upper_id=upper, before_id=before, min_id=minimum)
            last = before
            for item in _items(page):
                last = _ordered_id(item, last, upper)
                if last <= minimum:
                    raise ChatExportError("History escaped incremental boundary")
                with tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+", encoding="utf-8") as spool:
                    await self.write_message(cast(TextIO, spool), item, peer_id)
                    spool.seek(0)
                    checkpoint.save("message", peer_id, last, spool.read(), key)
                    self.counts["messages"] += 1
            if page["done"]:
                checkpoint.mark(key + ":done", True)
                return
            before = _advance(page, before, last, upper)
            checkpoint.mark(key, before)

    async def admin_log(self, checkpoint: Checkpoint, minimum: int) -> None:
        if checkpoint.state("admin:done"):
            return
        before = cast(int, checkpoint.state("admin") or 0)
        while True:
            page = await self.request("admin_log", self.dialog_id, before_id=before, min_id=minimum)
            status = _status(page, {"complete", "unavailable"})
            last = before
            if status != "unavailable":
                for raw in _items(page):
                    event = _facts(raw)
                    last = _ordered_id(event, last)
                    if last <= minimum:
                        raise ChatExportError("Admin log escaped incremental boundary")
                    event["actor"] = await self.identity(event.get("actor"), self.dialog_id)
                    await self.related_users(event, self.dialog_id)
                    payload = json.dumps(
                        project_admin_event(event, self.dialog_id), ensure_ascii=False, allow_nan=False
                    )
                    checkpoint.save("admin", self.dialog_id, last, payload, "admin")
                    self.counts["admin_events"] += 1
            if status == "unavailable" or page["done"]:
                checkpoint.mark("admin:done", True)
                return
            before = _advance(page, before, last)
            checkpoint.mark("admin", before)


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_records(stream: TextIO, checkpoint: Checkpoint, peers: list[Payload]) -> None:
    for kind, field in (("admin", "admin_events"), ("message", "messages")):
        stream.write(f',"{field}":[')
        first = True
        for peer in peers[:1] if kind == "admin" else peers:
            for payload in checkpoint.records(kind, cast(int, peer["dialog_id"])):
                if not first:
                    stream.write(",")
                stream.write(payload)
                first = False
        stream.write("]")


def _publish(checkpoint: Checkpoint, peers: list[Payload], output: Path) -> Payload:
    if _fingerprint(output) != checkpoint.state("published"):
        raise FileExistsError(f"Export destination changed independently: {output}")
    descriptor, name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
    temporary = Path(name)
    summary = checkpoint.summary()
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write('{"format_version":1,"group":')
            _dump(stream, project_group(_object(peers[0]["group"])))
            stream.write(',"metadata":')
            _dump(stream, {"order": ORDER, "peers": [project_group(_object(peer["group"])) for peer in peers]})
            _write_records(stream, checkpoint, peers)
            stream.write(',"export":')
            _dump(stream, summary)
            stream.write("}\n")
            stream.flush()
            os.fsync(stream.fileno())
        if _fingerprint(output) != checkpoint.state("published"):
            raise FileExistsError(f"Export destination changed independently: {output}")
        checkpoint.mark("pending_publish", _fingerprint(temporary))
        if output.exists():
            temporary.replace(output)
        else:
            os.link(temporary, output)
        _sync_directory(output.parent)
        checkpoint.mark("published", _fingerprint(output))
        checkpoint.mark("pending_publish", None)
        return summary
    finally:
        temporary.unlink(missing_ok=True)


def _base_info(checkpoint: Checkpoint, base: Path | None, refresh: int) -> Payload:
    if base is None:
        return {"boundaries": {}, "admin_max": 0}
    if base.with_name(f".{base.name}.resume.sqlite3").exists():
        raise ChatExportError("Incremental base is incomplete; resume its original export first")
    fingerprint = _fingerprint(base)
    saved = checkpoint.state("base_fingerprint")
    if saved is not None and fingerprint != saved:
        raise ChatExportError("Incremental base changed since checkpoint creation")
    info = checkpoint.state("base_info")
    if info is not None and saved is None:
        raise ChatExportError("Incomplete base fingerprint checkpoint")
    if info is None:
        info = census(base, refresh)
        if _fingerprint(base) != fingerprint:
            raise ChatExportError("Incremental base changed while reading")
        checkpoint.mark_many({"base_info": info, "base_fingerprint": fingerprint})
    return _object(info)


async def _prepare(
    export: _Export, checkpoint: Checkpoint, base: Path | None, refresh: int
) -> tuple[list[Payload], Payload]:
    info = _base_info(checkpoint, base, refresh)
    saved_peers = checkpoint.state("peers")
    peers = cast(list[Payload], saved_peers) if saved_peers else await export.open_peers()
    if base is not None:
        if _object(info["group"]).get("dialog_id") != str(peers[0]["dialog_id"]):
            raise ChatExportError("Incremental base belongs to another group")
        if not set(cast(list[int], info["peers"])).issubset({cast(int, p["dialog_id"]) for p in peers}):
            raise ChatExportError("Incremental base migration chain does not match")
    checkpoint.mark("peers", peers)
    export.dialog_id = cast(int, peers[0]["dialog_id"])
    export.total_hint = _hint(peers) if base is None else None
    if base is not None:
        checkpoint.import_base(base, info)
        if _fingerprint(base) != checkpoint.state("base_fingerprint"):
            raise ChatExportError("Incremental base changed while importing")
    export.counts.update(cast(dict[str, int], checkpoint.summary()))
    export.started_messages = export.counts["messages"]
    return peers, info


async def _run_export(
    export: _Export, checkpoint: Checkpoint, output: Path, base: Path | None, refresh: int
) -> Payload:
    task = asyncio.create_task(export.ticker())
    current = asyncio.current_task()
    loop = asyncio.get_running_loop()
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    if current is not None:
        loop.add_signal_handler(signal.SIGTERM, current.cancel)
    try:
        peers, info = await _prepare(export, checkpoint, base, refresh)
        await export.admin_log(checkpoint, cast(int, info["admin_max"]))
        boundaries = _object(info["boundaries"])
        for peer in peers:
            await export.history_peer(checkpoint, peer, cast(int, boundaries.get(str(peer["dialog_id"]), 0)))
        export.history_finished = True
        summary = _publish(checkpoint, peers, output)
        export.progress()
        return summary
    except BaseException:
        saved_peers = checkpoint.state("peers")
        if saved_peers is not None:
            try:
                _publish(checkpoint, cast(list[Payload], saved_peers), output)
            except (OSError, ValueError, CheckpointError) as exc:
                print(f"Could not publish partial JSON: {exc}; durable records remain saved", file=sys.stderr)
        print(
            f"Export interrupted; saved progress retained. Repeat the same command to resume: {checkpoint.path}",
            file=sys.stderr,
        )
        raise
    finally:
        loop.remove_signal_handler(signal.SIGTERM)
        signal.signal(signal.SIGTERM, previous_sigterm)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def _resume_options(checkpoint: Checkpoint, options: Payload) -> None:
    saved = checkpoint.state("options")
    if saved is None and not checkpoint.is_empty():
        raise ChatExportError("Populated checkpoint has no export identity")
    if saved is not None and saved != options:
        raise ChatExportError("Resume options differ from the saved export")
    checkpoint.mark("options", options)
    pending = checkpoint.state("pending_publish")
    output = Path(cast(str, options["output"]))
    if pending is not None and _fingerprint(output) == pending:
        checkpoint.mark("published", pending)


async def export_group(
    dialog_id: int | str, output: Path, *, update_from: Path | None = None, refresh_messages: int = 100
) -> Payload:
    """Commit every received record locally, resume automatically, and publish valid v1 JSON."""
    if refresh_messages < 0:
        raise ValueError("refresh_messages must be nonnegative")
    if update_from is not None and update_from.resolve() == output.resolve():
        raise ValueError("Incremental output must differ from its immutable base")
    path = output.with_name(f".{output.name}.resume.sqlite3")
    if path.is_symlink() or (not path.exists() and _fingerprint(output) is not None):
        raise FileExistsError(f"Export destination already exists: {output}")
    options: Payload = {
        "selector": dialog_id,
        "output": str(output.resolve()),
        "update_from": str(update_from.resolve()) if update_from else None,
        "refresh_messages": refresh_messages,
    }
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path.chmod(0o600)
        checkpoint = Checkpoint(path)
        _sync_directory(path.parent)
        try:
            _resume_options(checkpoint, options)
            summary = await _run_export(_Export(dialog_id), checkpoint, output, update_from, refresh_messages)
        finally:
            checkpoint.close()
        path.unlink()
        _sync_directory(path.parent)
        return summary
    finally:
        os.close(descriptor)


def _checkpoint_options(path: Path) -> Payload:
    descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise FileExistsError("An export in this directory is already running") from exc
        try:
            return read_checkpoint_options(path)
        except CheckpointError, ValueError, ChatExportError:
            return {}
    finally:
        os.close(descriptor)


def _matching_checkpoint(path: Path, selector: int | str, base: Path | None, refresh: int) -> bool:
    if path.is_symlink():
        return False
    options = _checkpoint_options(path)
    output = path.with_name(path.name[1:].removesuffix(".resume.sqlite3"))
    return (
        options.get("output") == str(output.resolve())
        and options.get("selector") == selector
        and options.get("update_from") == (str(base.resolve()) if base else None)
        and options.get("refresh_messages") == refresh
    )


async def choose_output_directory(
    selector: int | str, directory: Path, *, update_from: Path | None = None, refresh_messages: int = 100
) -> Path:
    """Name exports by canonical group identity and preserve every completed file."""
    directory = directory.resolve()
    opened = await _Export(selector).request("open", selector)
    identifier = _group_id(_object(opened.get("group")).get("dialog_id"))
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    stem = f"telegram-group-{identifier}"
    for checkpoint in sorted(directory.glob(f".{stem}*.json.resume.sqlite3")):
        name = checkpoint.name[1:].removesuffix(".resume.sqlite3")
        if re.fullmatch(re.escape(stem) + r"(?:\.\d+)?\.json", name) and _matching_checkpoint(
            checkpoint, selector, update_from, refresh_messages
        ):
            return checkpoint.with_name(checkpoint.name[1:].removesuffix(".resume.sqlite3"))
    number = 1
    while True:
        suffix = "" if number == 1 else f".{number}"
        output = directory / f"{stem}{suffix}.json"
        checkpoint = output.with_name(f".{output.name}.resume.sqlite3")
        if not output.exists() and not output.is_symlink() and not checkpoint.exists() and not checkpoint.is_symlink():
            return output
        number += 1
