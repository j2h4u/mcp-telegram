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
    id: str
    rank: str


class Reactor(TypedDict):
    actor: int


class Message(TypedDict):
    text: str
    message_id: str
    kind: str
    reactors: list[Reactor]
    related_users: list[int]
    author: int


class AdminEvent(TypedDict):
    actor: int


class Metadata(TypedDict):
    peers: list[Payload]
    exporter: Payload


class ExportDocument(TypedDict):
    identities: list[Identity]
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
        assert timeout_seconds == 420
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
    assert (summary["messages"], summary["reactors"]) == (4, 8)
    progress = capsys.readouterr().err
    assert "admin events 2" in progress and "estimated history total 4" in progress
    if inline_reactors:
        assert not any(call["operation"] == "reactions" for call in calls)
        assert "items" not in cast(Payload, cast(Payload, doc["messages"][0])["reactions"])
    assert [m["message_id"] for m in doc["messages"]] == ["2", "1", "2", "1"]
    assert doc["messages"][1]["kind"] == "service"
    message = cast(Payload, doc["messages"][0])
    assert message["date"] == "2026-10-03T10:00:00+00:00"
    assert message["text"] == "Юникод"
    assert "message" not in cast(Payload, message["metadata"])
    author = doc["identities"][message["author"]]
    assert (author["rank"], author["id"]) == ("Moderator", "5")
    assert "status" not in cast(Payload, message["reactions"])
    assert "source" not in cast(Payload, doc["admin_events"][0])
    assert doc["identities"][doc["messages"][0]["reactors"][0]["actor"]]["role"] == "admin"
    assert doc["identities"][doc["admin_events"][0]["actor"]]["is_admin"] is True
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
    assert json.loads((tmp_path / "out").read_text())["messages"] == []
    assert (tmp_path / ".out.resume.sqlite3").exists()


@pytest.mark.asyncio
async def test_million_messages_remain_bounded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A million records stay paged; inspect output in chunks, never json.load."""
    million = 1_000_000
    original_init = cli.Checkpoint.__init__

    def fast_init(self: cli.Checkpoint, path: Path) -> None:
        original_init(self, path)
        # Durability uses real FULL commits in the interruption tests; this checks memory only.
        self.db.execute("PRAGMA synchronous=OFF")

    monkeypatch.setattr(cli.Checkpoint, "__init__", fast_init)

    def fast_save(  # noqa: PLR0913 -- monkeypatch must preserve Checkpoint.save signature
        self: cli.Checkpoint, kind: str, peer: int, identifier: int, payload: str, cursor_key: str
    ) -> None:
        # Synthetic memory stress commits at the normal page marker; real crash tests commit each record.
        self.db.execute("INSERT OR REPLACE INTO records VALUES (?,?,?,?)", (kind, peer, identifier, payload))
        self.set_state(cursor_key, identifier)

    monkeypatch.setattr(cli.Checkpoint, "save", fast_save)
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
    related = doc["identities"][doc["messages"][0]["related_users"][0]]
    assert related["role"] == "member"
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
    assert doc["metadata"]["exporter"] == cli.project_exporter(cli.version("mcp-telegram"))
    assert [p["operation"] for p in calls] == ["open", "admin_log", "history"]


@pytest.mark.asyncio
async def test_unavailable_current_role_keeps_identity_with_null_role(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handler(payload: Payload) -> Payload:
        assert payload["operation"] == "participant"
        return {"ok": True, "data": {"status": "unavailable", "participant": None}}

    install_daemon(monkeypatch, handler)
    identity = await cli._Export(-1).identity({"id": 5, "kind": "user", "first_name": "Alice"}, -1)
    assert identity == {"id": 5, "kind": "user", "first_name": "Alice", "role": None, "is_admin": None}


def minimal_message(peer: object, identifier: int, text: str = "") -> Payload:
    return {
        "id": identifier,
        "dialog_id": peer,
        "kind": "message",
        "raw": {"message": text},
        "author": None,
        "reactions": {"status": "known_empty", "can_view_list": False},
    }


@pytest.mark.asyncio
async def test_committed_records_survive_failure_and_resume_frozen_boundary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[Payload] = []
    fail = True

    async def handler(p: Payload) -> Payload:
        calls.append(p)
        if p["operation"] == "open":
            data = {"group": {"dialog_id": -1}, "upper_id": 4, "migrated_from_dialog_id": None}
        elif p["operation"] == "admin_log":
            data = {"items": [], "done": True, "status": "complete"}
        elif fail and p["before_id"]:
            raise cli.ChatExportError("lost connection")
        else:
            ids = [4, 3] if not p["before_id"] else [2, 1]
            data = {
                "items": [minimal_message(-1, i) for i in ids],
                "next_before_id": ids[-1],
                "done": bool(p["before_id"]),
            }
        return {"ok": True, "data": data}

    install_daemon(monkeypatch, handler)
    output = tmp_path / "out.json"
    with pytest.raises(cli.ChatExportError):
        await cli.export_group(-1, output)
    assert [m["message_id"] for m in cast(ExportDocument, json.loads(output.read_text()))["messages"]] == ["4", "3"]
    checkpoint = tmp_path / ".out.json.resume.sqlite3"
    assert checkpoint.exists() and checkpoint.stat().st_mode & 0o777 == 0o600
    calls.clear()
    fail = False
    await cli.export_group(-1, output)
    assert [p["operation"] for p in calls] == ["history"]
    assert calls[0]["before_id"] == 3 and calls[0]["upper_id"] == 4
    assert [m["message_id"] for m in cast(ExportDocument, json.loads(output.read_text()))["messages"]] == [
        "4",
        "3",
        "2",
        "1",
    ]
    assert not checkpoint.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("refresh", [0, 2])
async def test_incremental_keeps_old_history_and_refreshes_recent_deletions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, refresh: int
) -> None:
    phase = "base"
    calls: list[Payload] = []

    async def handler(p: Payload) -> Payload:
        calls.append(p)
        if p["operation"] == "open":
            data: Payload = {
                "group": {"dialog_id": -1},
                "upper_id": 5 if phase == "base" else 7,
                "migrated_from_dialog_id": None,
            }
        elif p["operation"] == "admin_log":
            data = {"items": [], "done": True, "status": "complete"}
        else:
            ids = [5, 4, 3, 2, 1] if phase == "base" else [7, 6, 5, 3, 2, 1]
            ids = [i for i in ids if i > cast(int, p["min_id"])]
            data = {
                "items": [minimal_message(-1, i, "old" if phase == "base" else "edited") for i in ids],
                "next_before_id": ids[-1] if ids else 0,
                "done": True,
            }
        return {"ok": True, "data": data}

    install_daemon(monkeypatch, handler)
    base = tmp_path / "base.json"
    await cli.export_group(-1, base)
    original = base.read_bytes()
    phase = "update"
    calls.clear()
    output = tmp_path / "new.json"
    await cli.export_group(-1, output, update_from=base, refresh_messages=refresh)
    assert base.read_bytes() == original
    messages = cast(ExportDocument, json.loads(output.read_text()))["messages"]
    assert [m["message_id"] for m in messages] == (
        ["7", "6", "5", "4", "3", "2", "1"] if refresh == 0 else ["7", "6", "5", "3", "2", "1"]
    )
    assert messages[-1]["text"] == "old"
    assert messages[2]["text"] == ("old" if refresh == 0 else "edited")
    assert next(p for p in calls if p["operation"] == "history")["min_id"] == (5 if refresh == 0 else 3)


@pytest.mark.asyncio
async def test_cancel_mid_record_keeps_only_complete_records(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    waiting = asyncio.Event()

    async def handler(p: Payload) -> Payload:
        if p["operation"] == "open":
            data = {"group": {"dialog_id": -1}, "upper_id": 2, "migrated_from_dialog_id": None}
        elif p["operation"] == "admin_log":
            data = {"items": [], "done": True, "status": "complete"}
        elif p["operation"] == "participant":
            waiting.set()
            await asyncio.Event().wait()
            return {}
        else:
            second = minimal_message(-1, 1)
            second["author"] = {"id": 5, "kind": "user"}
            data = {"items": [minimal_message(-1, 2), second], "done": True}
        return {"ok": True, "data": data}

    install_daemon(monkeypatch, handler)
    output = tmp_path / "out.json"
    task = asyncio.create_task(cli.export_group(-1, output))
    await waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [m["message_id"] for m in cast(ExportDocument, json.loads(output.read_text()))["messages"]] == ["2"]
    assert (tmp_path / ".out.json.resume.sqlite3").exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("format_version", True),
        ("format_version", "1"),
        ("messages", None),
        ("messages", {}),
        ("messages", [None]),
        ("admin_events", [42]),
    ],
)
def test_malformed_incremental_base_rejected(tmp_path: Path, field: str, value: object) -> None:
    doc: Payload = {
        "format_version": 1,
        "group": {"dialog_id": "-1"},
        "metadata": {"order": cli.ORDER, "peers": [{"dialog_id": "-1"}]},
        "admin_events": [],
        "messages": [],
        "export": {"messages": 0, "admin_events": 0, "reactors": 0},
    }
    doc[field] = value
    source = tmp_path / "base.json"
    source.write_text(json.dumps(doc))
    with pytest.raises(ValueError):
        cli.census(source, 100)


@pytest.mark.asyncio
async def test_sigkill_recovers_real_committed_records(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import subprocess
    import sys

    output = tmp_path / "killed.json"
    script = """
import asyncio, os, signal, sys
from pathlib import Path
from mcp_telegram import chat_export_cli as cli
async def request(self, operation, peer, **kwargs):
    if operation == 'open':
        return {'group': {'dialog_id': -1}, 'upper_id': 3, 'migrated_from_dialog_id': None}
    if operation == 'admin_log':
        return {'items': [], 'done': True, 'status': 'complete'}
    if kwargs['before_id']:
        os.kill(os.getpid(), signal.SIGKILL)
    return {'items': [{'id': i, 'dialog_id': -1, 'kind': 'message', 'raw': {}, 'author': None,
        'reactions': {'status': 'known_empty', 'can_view_list': False}} for i in [3, 2]],
        'next_before_id': 2, 'done': False}
cli._Export.request = request
asyncio.run(cli.export_group(-1, Path(sys.argv[1])))
"""
    result = subprocess.run([sys.executable, "-c", script, str(output)], capture_output=True, check=False)
    assert result.returncode < 0
    assert not output.exists()
    calls: list[Payload] = []

    async def handler(p: Payload) -> Payload:
        calls.append(p)
        assert p["operation"] == "history" and p["before_id"] == 2 and p["upper_id"] == 3
        return {"ok": True, "data": {"items": [minimal_message(-1, 1)], "done": True}}

    install_daemon(monkeypatch, handler)
    await cli.export_group(-1, output)
    assert [m["message_id"] for m in cast(ExportDocument, json.loads(output.read_text()))["messages"]] == [
        "3",
        "2",
        "1",
    ]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_transient_ipc_retries_without_repeating_committed_records(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0

    async def handler(p: Payload) -> Payload:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise cli.DaemonNotRunningError("stalled", kind="response_timeout")
        return {"ok": True, "data": {"items": [], "done": True}}

    async def defer(self: cli._Export, data: Payload) -> None:
        assert data["retry_after"] in {1, 2}

    install_daemon(monkeypatch, handler)
    monkeypatch.setattr(cli._Export, "defer", defer)
    await cli._Export(-1).request("history", -1, before_id=2, upper_id=3)
    assert attempts == 3


@pytest.mark.asyncio
async def test_unfinished_base_is_rejected_without_telegram(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    base = tmp_path / "base.json"
    base.write_text("{}")
    (tmp_path / ".base.json.resume.sqlite3").touch()

    async def handler(p: Payload) -> Payload:
        pytest.fail("Incomplete base must fail before Telegram requests")

    install_daemon(monkeypatch, handler)
    with pytest.raises(cli.ChatExportError, match="incomplete"):
        await cli.export_group(-1, tmp_path / "new.json", update_from=base)
    assert base.read_text() == "{}"


@pytest.mark.asyncio
async def test_export_rejects_concurrent_owner(tmp_path: Path) -> None:
    import fcntl

    output = tmp_path / "locked.json"
    with (tmp_path / ".locked.json.resume.sqlite3").open("wb") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            await cli.export_group(-1, output)
    assert not output.exists()


@pytest.mark.asyncio
async def test_directory_filenames_use_group_id_and_preserve_completed_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def handler(p: Payload) -> Payload:
        identifier = -1001 if p["dialog_id"] == "@one" else -1002
        return {"ok": True, "data": {"group": {"dialog_id": identifier, "title": "Same title"}}}

    install_daemon(monkeypatch, handler)
    one = await cli.choose_output_directory("@one", tmp_path)
    two = await cli.choose_output_directory("@two", tmp_path)
    assert one.name == "telegram-group--1001.json"
    assert two.name == "telegram-group--1002.json"
    one.write_text("valuable export")
    again = await cli.choose_output_directory("@one", tmp_path)
    assert again.name == "telegram-group--1001.2.json"
    assert one.read_text() == "valuable export"


@pytest.mark.asyncio
async def test_directory_selection_resumes_only_matching_owned_checkpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def handler(p: Payload) -> Payload:
        return {"ok": True, "data": {"group": {"dialog_id": -1}}}

    install_daemon(monkeypatch, handler)
    for identifier, number, selector in [(-1, 1, "@different"), (-1, 2, "@one"), (-100, 1, "@one")]:
        suffix = "" if number == 1 else f".{number}"
        output = tmp_path / f"telegram-group-{identifier}{suffix}.json"
        checkpoint = cli.Checkpoint(output.with_name(f".{output.name}.resume.sqlite3"))
        checkpoint.mark(
            "options",
            {"selector": selector, "output": str(output.resolve()), "update_from": None, "refresh_messages": 100},
        )
        checkpoint.db.close()
    chosen = await cli.choose_output_directory("@one", tmp_path)
    assert chosen.name == "telegram-group--1.2.json"
    other = await cli.choose_output_directory("@new", tmp_path)
    assert other.name == "telegram-group--1.3.json"


@pytest.mark.asyncio
async def test_directory_selection_recovers_hot_rollback_journal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import subprocess
    import sys

    output = tmp_path / "telegram-group--1.json"
    sidecar = tmp_path / ".telegram-group--1.json.resume.sqlite3"
    script = """
import os, signal, sys
from pathlib import Path
from mcp_telegram.chat_export_checkpoint import Checkpoint
checkpoint = Checkpoint(Path(sys.argv[1]))
checkpoint.mark('options', {'selector': '@one', 'output': sys.argv[2], 'update_from': None,
                           'refresh_messages': 100})
checkpoint.mark('committed', True)
checkpoint.db.execute('PRAGMA cache_size=1')
checkpoint.db.execute('BEGIN IMMEDIATE')
checkpoint.db.execute("UPDATE state SET value='false' WHERE key='committed'")
for identifier in range(1000):
    checkpoint.db.execute('INSERT INTO records VALUES (?,?,?,?)', ('message', -1, identifier, 'x' * 4096))
os.kill(os.getpid(), signal.SIGKILL)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(sidecar), str(output.resolve())], capture_output=True, check=False
    )
    assert result.returncode < 0
    assert sidecar.with_name(sidecar.name + "-journal").exists()

    async def handler(p: Payload) -> Payload:
        return {"ok": True, "data": {"group": {"dialog_id": -1}}}

    install_daemon(monkeypatch, handler)
    assert await cli.choose_output_directory("@one", tmp_path) == output
    recovered = cli.Checkpoint(sidecar)
    try:
        assert recovered.state("committed") is True
        assert recovered.db.execute("SELECT count(*) FROM records").fetchone()[0] == 0
    finally:
        recovered.db.close()


@pytest.mark.asyncio
async def test_directory_selection_reports_active_export_instead_of_starting_another(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import fcntl

    sidecar = tmp_path / ".telegram-group--1.json.resume.sqlite3"
    checkpoint = cli.Checkpoint(sidecar)
    checkpoint.db.close()

    async def handler(p: Payload) -> Payload:
        return {"ok": True, "data": {"group": {"dialog_id": -1}}}

    install_daemon(monkeypatch, handler)
    with sidecar.open("rb") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(FileExistsError, match="already running"):
            await cli.choose_output_directory("@one", tmp_path)


@pytest.mark.asyncio
async def test_operation_timeout_deferral_keeps_committed_cursor_and_repeats_same_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[Payload] = []
    deferred = 0
    output = tmp_path / "out.json"

    async def handler(payload: Payload) -> Payload:
        nonlocal deferred
        calls.append(payload)
        if payload["operation"] == "open":
            data: Payload = {"group": {"dialog_id": -1}, "upper_id": 2, "migrated_from_dialog_id": None}
        elif payload["operation"] == "admin_log":
            data = {"items": [], "done": True, "status": "complete"}
        elif payload["before_id"] == 0:
            data = {"items": [minimal_message(-1, 2)], "next_before_id": 2, "done": False}
        elif deferred < 2:
            deferred += 1
            return {"ok": False, "error": "export_deferred", "reason": "operation_timeout", "retry_after": 5}
        else:
            data = {"items": [minimal_message(-1, 1)], "done": True}
        return {"ok": True, "data": data}

    async def defer(self: cli._Export, response: Payload) -> None:
        assert response["reason"] == "operation_timeout" and response["retry_after"] == 5
        checkpoint = cli.Checkpoint(output.with_name(f".{output.name}.resume.sqlite3"))
        try:
            assert checkpoint.state("history:-1") == 2
            assert checkpoint.summary()["messages"] == 1
        finally:
            checkpoint.close()

    install_daemon(monkeypatch, handler)
    monkeypatch.setattr(cli._Export, "defer", defer)
    summary = await cli.export_group(-1, output)
    retries = [call for call in calls if call["operation"] == "history" and call["before_id"] == 2]
    assert len(retries) == 3 and retries[0] == retries[1] == retries[2]
    assert summary["messages"] == 2
    assert not output.with_name(f".{output.name}.resume.sqlite3").exists()
