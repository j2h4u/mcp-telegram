"""Streaming export publication, paging and cancellation checks."""

import asyncio
import json
import resource
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TypedDict, cast

import pytest

from mcp_telegram import chat_export_cli as cli

type Payload = dict[str, object]


class Identity(TypedDict):
    role: str
    is_admin: bool


class Reactor(TypedDict):
    actor_role: str


class Message(TypedDict):
    message_id: str
    kind: str
    reactors: list[Reactor]
    related_users: list[Identity]


class AdminEvent(TypedDict):
    actor_is_admin: bool


class Metadata(TypedDict):
    peers: list[Payload]


class ExportDocument(TypedDict):
    messages: list[Message]
    admin_events: list[AdminEvent]
    group: Payload
    metadata: Metadata


def install_daemon(monkeypatch: pytest.MonkeyPatch, handler: Callable[[Payload], Awaitable[Payload]]) -> None:
    class Connection:
        async def request(self, payload: Payload) -> Payload:
            return await handler(payload)

    @asynccontextmanager
    async def connection(timeout_seconds: float) -> AsyncIterator[Connection]:
        assert timeout_seconds == 75
        yield Connection()

    monkeypatch.setattr(cli, "daemon_connection", connection)


@pytest.mark.asyncio
@pytest.mark.parametrize("inline_reactors", [False, True])
async def test_pages_roles_reactions_service_admin_and_legacy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], inline_reactors: bool
) -> None:
    calls: list[Payload] = []

    async def handler(p: Payload) -> Payload:
        data: Payload
        calls.append(p)
        op = p["operation"]
        peer = p["dialog_id"]
        if op == "open":
            data = {
                "group": {"dialog_id": peer, "title": "Group", "kind": "group"},
                "upper_id": 2,
                "total_messages": 2,
                "total_kind": "estimated",
                "migrated_from_dialog_id": -2 if peer == -1 else None,
                "observed_at": "now",
            }
        elif op == "history":
            mid = 2 if not p["before_id"] else 1
            data = {
                "items": [
                    {
                        "id": mid,
                        "dialog_id": peer,
                        "kind": "service" if mid == 1 else "message",
                        "raw": {"_": "Message", "message": "Юникод"},
                        "date": "2026-10-03T10:00:00+00:00",
                        "author": {"id": 5, "kind": "user"},
                        "topic_id": 7,
                        "reactions": {
                            "status": "complete",
                            "can_view_list": True,
                            "items": [
                                {"peer": {"id": 5, "kind": "user"}, "reaction": {"emoji": "👍"}},
                                {"peer": {"id": 5, "kind": "user"}, "reaction": {"emoji": "❤️"}},
                            ],
                        }
                        if inline_reactors
                        else {"status": "pending", "can_view_list": True},
                    }
                ],
                "next_before_id": mid,
                "done": mid == 1,
            }
        elif op == "participant":
            data = {
                "status": "complete",
                "participant": {"role": "admin", "is_admin": True, "rank": "Moderator", "status": "complete"},
            }
        elif op == "topic":
            data = {"status": "unavailable", "topic": None}
        elif op == "reactions":
            data = {
                "items": [{"peer": {"id": 5, "kind": "user"}, "reaction": {"emoji": "👍"}}],
                "next_offset": "next" if not p["offset"] else None,
                "status": "complete",
            }
        else:
            data = {
                "items": [
                    {
                        "id": 9 if not p["before_id"] else 8,
                        "actor": {"id": 5, "kind": "user"},
                        "action": {},
                        "source": "telegram_admin_log",
                    }
                ],
                "next_before_id": 9 if not p["before_id"] else 8,
                "done": bool(p["before_id"]),
                "status": "complete",
            }
        return {"ok": True, "data": data}

    install_daemon(monkeypatch, handler)
    output = tmp_path / "export.json"
    summary = await cli.export_group(-1, output)
    doc = cast(ExportDocument, json.loads(output.read_text()))
    assert set(summary) == {"messages", "admin_events", "reactors"}
    assert summary["messages"] == 4
    assert summary["reactors"] == 8
    progress = capsys.readouterr().err
    assert "admin events 2" in progress
    assert "estimated history total 4" in progress
    if inline_reactors:
        assert not any(call["operation"] == "reactions" for call in calls)
        assert "items" not in cast(Payload, cast(Payload, doc["messages"][0])["reactions"])
    assert [m["message_id"] for m in doc["messages"]] == ["2", "1", "2", "1"]
    assert doc["messages"][1]["kind"] == "service"
    message = cast(Payload, doc["messages"][0])
    assert message["date"] == "2026-10-03T10:00:00+00:00"
    assert message["text"] == "Юникод"
    assert "message" not in cast(Payload, message["metadata"])
    assert message["author_rank"] == "Moderator"
    assert message["author_id"] == "5"
    assert "status" not in cast(Payload, message["reactions"])
    assert "source" not in cast(Payload, doc["admin_events"][0])
    assert doc["messages"][0]["reactors"][0]["actor_role"] == "admin"
    assert doc["admin_events"][0]["actor_is_admin"] is True
    assert doc["messages"][0].get("topic") is None
    assert [c["dialog_id"] for c in calls if c["operation"] == "participant"] == [-1]
    assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.asyncio
async def test_deferral_repeats_request_and_cancel_cleans(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[Payload] = []
    waiting = asyncio.Event()

    async def handler(payload: Payload) -> Payload:
        calls.append(payload)
        if len(calls) == 1:
            return {"ok": False, "error": "export_deferred", "retry_after": 0.1}
        waiting.set()
        await asyncio.Event().wait()
        return {}

    install_daemon(monkeypatch, handler)
    task = asyncio.create_task(cli.export_group(-1, tmp_path / "out"))
    await waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls[0] == calls[1]
    assert not list(tmp_path.glob(".*.tmp"))
    assert not (tmp_path / "out").exists()


@pytest.mark.asyncio
async def test_no_clobber_dangling_symlink(tmp_path: Path) -> None:
    output = tmp_path / "out"
    output.symlink_to(tmp_path / "missing")
    with pytest.raises(FileExistsError):
        await cli.export_group(-1, output)
    assert output.is_symlink()


@pytest.mark.asyncio
async def test_history_failure_removes_temporary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def handler(p: Payload) -> Payload:
        if p["operation"] == "open":
            return {"ok": True, "data": {"group": {"dialog_id": -1}, "upper_id": 1, "migrated_from_dialog_id": None}}
        if p["operation"] == "admin_log":
            return {"ok": True, "data": {"items": [], "next_before_id": 0, "done": True, "status": "complete"}}
        return {"ok": False, "error": "history_unavailable", "message": "Access lost"}

    install_daemon(monkeypatch, handler)
    with pytest.raises(cli.ChatExportError, match="Access lost"):
        await cli.export_group(-1, tmp_path / "out")
    assert not list(tmp_path.glob(".*.tmp"))
    assert not (tmp_path / "out").exists()


@pytest.mark.asyncio
async def test_million_messages_remain_bounded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A million records stay paged; inspect output in chunks, never json.load."""
    million = 1_000_000
    max_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    async def handler(p: Payload) -> Payload:
        data: Payload
        if p["operation"] == "open":
            data = {
                "group": {"dialog_id": -1},
                "upper_id": million,
                "total_messages": None,
                "total_kind": "unknown",
                "migrated_from_dialog_id": None,
            }
        elif p["operation"] == "history":
            upper = cast(int, p["before_id"]) - 1 if p["before_id"] else million
            lower = max(1, upper - 99)
            data = {
                "items": [
                    {
                        "id": mid,
                        "dialog_id": -1,
                        "kind": "message",
                        "raw": {},
                        "author": None,
                        "reactions": {"status": "known_empty", "can_view_list": False},
                    }
                    for mid in range(upper, lower - 1, -1)
                ],
                "next_before_id": lower,
                "done": lower == 1,
            }
        else:
            data = {"items": [], "next_before_id": 0, "done": True, "status": "complete"}
        return {"ok": True, "data": data}

    install_daemon(monkeypatch, handler)
    output = tmp_path / "million.json"
    summary = await cli.export_group(-1, output)
    assert summary["messages"] == million
    assert resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - max_rss < 32 * 1024
    total = 0
    tail = b""
    needle = b'"reactors":[]'
    with output.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            combined = tail + chunk
            total += combined.count(needle)
            # Retain only an incomplete marker across chunk boundaries.
            tail = combined[-len(needle) + 1 :]
            total -= tail.count(needle)
    assert total == million
    output.unlink()


@pytest.mark.asyncio
async def test_tombstones_partial_reactors_and_related_roles(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    history_cursors: list[object] = []

    async def handler(p: Payload) -> Payload:
        data: Payload
        op = p["operation"]
        if op == "open":
            data = {
                "group": {"dialog_id": -1},
                "upper_id": 10,
                "total_kind": "unknown",
                "migrated_from_dialog_id": None,
            }
        elif op == "history":
            before = p["before_id"]
            history_cursors.append(before)
            if before == 0:
                data = {"items": [], "next_before_id": 8, "done": False}
            elif before == 8:
                data = {
                    "items": [
                        {
                            "id": 7,
                            "dialog_id": -1,
                            "kind": "service",
                            "raw": {},
                            "author": None,
                            "related_users": [{"id": 5, "kind": "user"}],
                            "reactions": {"status": "pending", "can_view_list": True},
                        }
                    ],
                    "next_before_id": 6,
                    "done": False,
                }
            else:
                data = {"items": [], "next_before_id": 0, "done": True}
        elif op == "participant":
            data = {"status": "complete", "participant": {"role": "member", "is_admin": False}}
        elif op == "reactions":
            data = {
                "items": [{"peer": {"id": 5, "kind": "user"}}],
                "total": 3,
                "next_offset": None,
                "status": "partial",
            }
        else:
            data = {"items": [], "next_before_id": 0, "done": True, "status": "complete"}
        return {"ok": True, "data": data}

    install_daemon(monkeypatch, handler)
    output = tmp_path / "export.json"
    summary = await cli.export_group(-1, output)
    doc = cast(ExportDocument, json.loads(output.read_text()))
    assert history_cursors == [0, 8, 6]
    assert doc["messages"][0]["related_users"][0]["role"] == "member"
    assert len(doc["messages"][0]["reactors"]) == 1
    assert summary == {"messages": 1, "admin_events": 0, "reactors": 1}


@pytest.mark.asyncio
async def test_deferred_progress_reports_wait_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = 0

    async def handler(p: Payload) -> Payload:
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"ok": False, "error": "export_deferred", "retry_after": 0.1, "reason": "FloodWait"}
        return {"ok": False, "error": "account_protected"}

    install_daemon(monkeypatch, handler)
    with pytest.raises(cli.ChatExportError, match="account_protected"):
        await cli.export_group(-1, tmp_path / "out")
    text = capsys.readouterr().err
    assert "waiting: FloodWait; retry in" in text
    assert "enrichments 0" in text


@pytest.mark.parametrize(
    ("incoming", "last", "upper", "cursor"),
    [(8, 7, 10, 8), (8, 7, 10, 0), (8, 6, 10, 7), (0, 0, 10, 11)],
)
def test_export_rejects_stalled_or_escaping_cursors(incoming: int, last: int, upper: int, cursor: int) -> None:
    with pytest.raises(cli.ChatExportError):
        cli._advance({"next_before_id": cursor}, incoming, last, upper)


@pytest.mark.parametrize("delay", [True, "0.1", float("inf"), 0.0])
def test_export_rejects_invalid_deferral_at_trust_boundary(delay: object) -> None:
    with pytest.raises(cli.ChatExportError):
        cli._retry_delay({"retry_after": delay})


@pytest.mark.asyncio
async def test_exact_url_resolves_once_then_uses_canonical_numeric_peer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    selector = "https://t.me/ai_engineers_guild"
    canonical = -1001234567890
    calls: list[Payload] = []

    async def handler(p: Payload) -> Payload:
        data: Payload
        calls.append(p)
        if p["operation"] == "open":
            assert p["dialog_id"] == selector
            data = {
                "group": {"dialog_id": canonical, "title": "Guild", "kind": "supergroup"},
                "upper_id": 0,
                "total_kind": "unknown",
                "migrated_from_dialog_id": None,
            }
        else:
            assert p["dialog_id"] == canonical
            data = {"items": [], "next_before_id": 0, "done": True, "status": "complete"}
        return {"ok": True, "data": data}

    install_daemon(monkeypatch, handler)
    output = tmp_path / "guild.json"
    await cli.export_group(selector, output)
    doc = cast(ExportDocument, json.loads(output.read_text()))
    assert doc["group"]["dialog_id"] == str(canonical)
    assert doc["metadata"]["peers"][0]["dialog_id"] == str(canonical)
    assert [p["operation"] for p in calls] == ["open", "admin_log", "history"]


@pytest.mark.asyncio
async def test_unavailable_current_role_keeps_identity_with_null_role(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handler(payload: Payload) -> Payload:
        assert payload["operation"] == "participant"
        return {"ok": True, "data": {"status": "unavailable", "participant": None}}

    install_daemon(monkeypatch, handler)
    identity = await cli._Export(-1).identity({"id": 5, "kind": "user", "first_name": "Alice"}, -1)
    assert identity == {"id": 5, "kind": "user", "first_name": "Alice", "role": None, "is_admin": None}
