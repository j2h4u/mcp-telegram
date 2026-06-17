from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TypedDict, cast
from unittest.mock import AsyncMock, patch

import pytest
from account_trace_fixtures import (
    open_trace_db,
    seed_dialog,
    seed_entity,
    seed_message,
    seed_synced_dialog,
)
from telethon.errors import FloodWaitError

from mcp_telegram.daemon_account_trace import (
    DaemonAccountTraceDeps,
    DaemonAccountTraceService,
    _LoggerLike,
    _messages_row_equal,
    _trace_candidate_dialogs,
    _trace_existing_message_bundle,
    _TraceCandidateBuildRequest,
)
from mcp_telegram.daemon_api import DaemonAPIServer
from mcp_telegram.sync_worker import (
    EntityRecord,
    ExtractedMessage,
    ForwardRecord,
    ReactionRecord,
    StoredMessage,
)


class FakeTraceClient:
    def __init__(
        self,
        messages_by_dialog: Mapping[int, Sequence[object]] | None = None,
        exc: Exception | None = None,
    ) -> None:
        self.messages_by_dialog = messages_by_dialog or {}
        self.exc = exc
        self.calls: list[tuple[int, dict[str, object]]] = []

    async def get_entity(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("unexpected get_entity call in trace enrichment tests")

    def iter_dialogs(self, *args: object, **kwargs: object) -> AsyncIterator[object]:
        async def _gen() -> AsyncIterator[object]:
            raise AssertionError("unexpected iter_dialogs call in trace enrichment tests")
            if False:  # pragma: no cover
                yield None

        return _gen()

    async def get_me(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("unexpected iter_dialogs call in trace enrichment tests")

    async def get_input_entity(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("unexpected get_input_entity call in trace enrichment tests")

    async def get_messages(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("unexpected get_messages call in trace enrichment tests")

    def iter_participants(self, *args: object, **kwargs: object) -> AsyncIterator[object]:
        async def _gen() -> AsyncIterator[object]:
            raise AssertionError("unexpected iter_participants call in trace enrichment tests")
            if False:  # pragma: no cover
                yield None

        return _gen()

    async def __call__(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("unexpected iter_participants call in trace enrichment tests")

    def iter_messages(self, dialog_id: int, **kwargs: object) -> AsyncIterator[object]:
        self.calls.append((dialog_id, kwargs))

        async def _gen():
            if self.exc is not None:
                raise self.exc
            for message in self.messages_by_dialog.get(dialog_id, []):
                yield message

        return _gen()


class _CandidateMessageKwargs(TypedDict, total=False):
    text: str
    edit_date: int | None
    reply_count: int
    reactions: list[ReactionRecord] | None
    entities: list[EntityRecord] | None
    forward: ForwardRecord | None


class _SeedExistingMessageBundleKwargs(TypedDict, total=False):
    text: str
    edit_date: int | None


def _dict(value: object) -> dict[str, object]:
    return cast(dict[str, object], value)


@pytest.fixture()
def trace_enrichment_server(tmp_path: Path) -> Iterator[tuple[DaemonAPIServer, sqlite3.Connection, FakeTraceClient]]:
    conn = open_trace_db(tmp_path)
    client = FakeTraceClient()
    server = DaemonAPIServer(conn, client, asyncio.Event())
    server.self_id = 101
    try:
        yield server, conn, client
    finally:
        conn.close()


@pytest.fixture()
def trace_service(
    trace_enrichment_server: tuple[DaemonAPIServer, sqlite3.Connection, FakeTraceClient],
) -> DaemonAccountTraceService:
    server, conn, client = trace_enrichment_server
    return DaemonAccountTraceService(
        DaemonAccountTraceDeps(
            conn=conn,
            client=client,
            resolve_dialog_id=server._resolve_dialog_id,
            self_id=server.self_id,
            logger=cast(_LoggerLike, logging.getLogger("test")),
            rid=lambda: "",
        )
    )


def fake_message(
    *,
    message_id: int,
    text: str = "trace hit",
    sent_at: int = 1_700_000_000,
    sender_id: int = 101,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=message_id,
        date=datetime.fromtimestamp(sent_at, tz=UTC),
        message=text,
        sender_id=sender_id,
        sender=None,
        media=None,
        reply_to=None,
        edit_date=None,
        grouped_id=None,
        out=False,
        post_author=None,
        reactions=None,
        entities=None,
        fwd_from=None,
    )


def candidate_message(  # noqa: PLR0913
    *,
    text: str = "same",
    edit_date: int | None = None,
    reply_count: int = 0,
    reactions: list[ReactionRecord] | None = None,
    entities: list[EntityRecord] | None = None,
    forward: ForwardRecord | None = None,
) -> ExtractedMessage:
    return ExtractedMessage(
        message=StoredMessage(
            dialog_id=222,
            message_id=1,
            sent_at=1_700_000_000,
            text=text,
            sender_id=101,
            sender_first_name=None,
            media_description=None,
            reply_to_msg_id=None,
            forum_topic_id=None,
            edit_date=edit_date,
            grouped_id=None,
            reply_to_peer_id=None,
            out=0,
            is_service=0,
            post_author=None,
        ),
        reply_count=reply_count,
        reactions=reactions or [],
        entities=entities or [],
        forward=forward,
    )


def seed_existing_message_bundle(conn: sqlite3.Connection, *, text: str = "same", edit_date: int | None = None) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO messages (
            dialog_id, message_id, sent_at, text, sender_id, sender_first_name,
            media_description, reply_to_msg_id, forum_topic_id, edit_date,
            grouped_id, reply_to_peer_id, out, is_service, post_author, is_deleted
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        """,
        (222, 1, 1_700_000_000, text, 101, None, None, None, None, edit_date, None, None, 0, 0, None),
    )
    conn.commit()


def test_trace_candidate_dialogs_are_bounded_visible_and_strategy_labeled(
    trace_enrichment_server: tuple[DaemonAPIServer, sqlite3.Connection, FakeTraceClient],
    trace_service: DaemonAccountTraceService,
) -> None:
    _server, conn, _client = trace_enrichment_server
    for offset, (dialog_id, dialog_type) in enumerate(
        [
            (101, "User"),
            (-1001, "Group"),
            (-1002, "Forum"),
            (-1003, "Chat"),
            (-1004, "Channel"),
        ]
    ):
        seed_dialog(conn, dialog_id=dialog_id, name=f"Dialog {offset}", dialog_type=dialog_type)
        seed_synced_dialog(conn, dialog_id=dialog_id)
    conn.commit()

    base_candidates = _trace_candidate_dialogs(
        _TraceCandidateBuildRequest(conn=conn, target_user_id=101, observed_rows=[], max_dialogs=10)
    )
    strategies = {candidate["dialog_type"]: candidate["strategy"] for candidate in base_candidates}
    assert strategies["User"] == "dialog_scan"
    assert strategies["Group"] == "author_search"
    assert strategies["Forum"] == "author_search"
    assert strategies["Chat"] == "author_search"
    assert strategies["Channel"] == "signature_only"

    seed_dialog(conn, dialog_id=-2001, name="Hidden", dialog_type="Group", hidden=1)
    seed_synced_dialog(conn, dialog_id=-2001)
    seed_dialog(conn, dialog_id=-2002, name="Lost", dialog_type="Group")
    seed_synced_dialog(conn, dialog_id=-2002, status="access_lost")
    for idx in range(20):
        dialog_id = -3000 - idx
        seed_dialog(conn, dialog_id=dialog_id, name=f"Extra {idx}", dialog_type="Group")
        seed_synced_dialog(conn, dialog_id=dialog_id)
    conn.commit()

    candidates = _trace_candidate_dialogs(
        _TraceCandidateBuildRequest(conn=conn, target_user_id=101, observed_rows=[], max_dialogs=10)
    )

    ids = [candidate["dialog_id"] for candidate in candidates]
    assert -2001 not in ids
    assert -2002 not in ids
    assert len(candidates) == 10
    assert candidates == _trace_candidate_dialogs(
        _TraceCandidateBuildRequest(conn=conn, target_user_id=101, observed_rows=[], max_dialogs=10)
    )


def test_trace_candidate_dialogs_include_cached_common_chats(
    trace_enrichment_server: tuple[DaemonAPIServer, sqlite3.Connection, FakeTraceClient],
    trace_service: DaemonAccountTraceService,
) -> None:
    _server, conn, _client = trace_enrichment_server
    seed_entity(conn, entity_id=101, name="Alice", username="alice")
    seed_dialog(conn, dialog_id=-5001, name="Common", dialog_type="Group")
    seed_synced_dialog(conn, dialog_id=-5001)
    conn.execute(
        "INSERT OR REPLACE INTO entity_details (entity_id, detail_json, fetched_at) VALUES (?, ?, ?)",
        (101, json.dumps({"common_chats": [{"id": -5001}]}), 1_700_000_000),
    )
    conn.commit()

    candidates = _trace_candidate_dialogs(
        _TraceCandidateBuildRequest(
            conn=conn,
            target_user_id=101,
            observed_rows=[],
            max_dialogs=10,
        )
    )

    assert candidates[0]["dialog_id"] == -5001
    assert candidates[0]["origin"] == "cached_common_chat"


def test_messages_row_equal_covers_base_and_child_tables(
    trace_enrichment_server: tuple[DaemonAPIServer, sqlite3.Connection, FakeTraceClient],
    trace_service: DaemonAccountTraceService,
) -> None:
    _server, conn, _client = trace_enrichment_server
    seed_existing_message_bundle(conn)
    conn.execute(
        "INSERT INTO message_reactions (dialog_id, message_id, emoji, count) VALUES (?, ?, ?, ?)",
        (222, 1, "👍", 2),
    )
    conn.execute(
        """
        INSERT INTO message_entities (dialog_id, message_id, offset, length, type, value)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (222, 1, 0, 4, "hashtag", "#tag"),
    )
    conn.execute(
        """
        INSERT INTO message_forwards (
            dialog_id, message_id, fwd_from_peer_id, fwd_from_name, fwd_date, fwd_channel_post
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (222, 1, 333, "Source", 1_700_000_010, 5),
    )
    conn.commit()
    existing = _trace_existing_message_bundle(conn, dialog_id=222, message_id=1)
    same = candidate_message(
        reply_count=0,
        reactions=[ReactionRecord(dialog_id=222, message_id=1, emoji="👍", count=2)],
        entities=[EntityRecord(dialog_id=222, message_id=1, offset=0, length=4, type="hashtag", value="#tag")],
        forward=ForwardRecord(
            dialog_id=222,
            message_id=1,
            fwd_from_peer_id=333,
            fwd_from_name="Source",
            fwd_date=1_700_000_010,
            fwd_channel_post=5,
        ),
    )

    assert _messages_row_equal(existing, same) is True
    assert _messages_row_equal(existing, candidate_message(text="changed")) is False
    assert _messages_row_equal(existing, candidate_message(edit_date=123)) is False
    assert _messages_row_equal(existing, candidate_message(reply_count=1)) is False
    assert (
        _messages_row_equal(
            existing,
            candidate_message(reactions=[ReactionRecord(dialog_id=222, message_id=1, emoji="👍", count=3)]),
        )
        is False
    )
    assert (
        _messages_row_equal(
            existing,
            candidate_message(
                entities=[EntityRecord(dialog_id=222, message_id=1, offset=0, length=5, type="hashtag", value="#tag")]
            ),
        )
        is False
    )
    assert (
        _messages_row_equal(
            existing,
            candidate_message(
                forward=ForwardRecord(
                    dialog_id=222,
                    message_id=1,
                    fwd_from_peer_id=334,
                    fwd_from_name="Source",
                    fwd_date=1_700_000_010,
                    fwd_channel_post=5,
                )
            ),
        )
        is False
    )
    conn.execute("UPDATE messages SET is_deleted = 1 WHERE dialog_id = 222 AND message_id = 1")
    conn.commit()
    assert _messages_row_equal(_trace_existing_message_bundle(conn, dialog_id=222, message_id=1), same) is False


@pytest.mark.asyncio
async def test_trace_enrichment_uses_canonical_insert_for_new_messages(
    trace_enrichment_server: tuple[DaemonAPIServer, sqlite3.Connection, FakeTraceClient],
    trace_service: DaemonAccountTraceService,
) -> None:
    server, conn, client = trace_enrichment_server
    client.messages_by_dialog = {222: [fake_message(message_id=1)]}
    seed_dialog(conn, dialog_id=222, name="Alice", dialog_type="User")
    seed_synced_dialog(conn, dialog_id=222)
    conn.commit()

    with patch("mcp_telegram.daemon_account_trace.insert_messages_with_fts") as insert_mock:
        result = await trace_service._trace_enrich_visible_dialogs(
            101,
            [{"dialog_id": 222, "strategy": "dialog_scan", "topic_id": None}],
        )

    insert_mock.assert_called_once()
    assert result["messages_seen"] == 1
    assert result["messages_persisted"] == 1
    assert result["duplicates_skipped"] == 0


@pytest.mark.asyncio
async def test_trace_enrichment_skips_unchanged_duplicates(
    trace_enrichment_server: tuple[DaemonAPIServer, sqlite3.Connection, FakeTraceClient],
    trace_service: DaemonAccountTraceService,
) -> None:
    server, conn, client = trace_enrichment_server
    seed_existing_message_bundle(conn)
    client.messages_by_dialog = {222: [fake_message(message_id=1, text="same")]}

    with patch("mcp_telegram.daemon_account_trace.insert_messages_with_fts") as insert_mock:
        result = await trace_service._trace_enrich_visible_dialogs(
            101,
            [{"dialog_id": 222, "strategy": "dialog_scan", "topic_id": None}],
        )

    insert_mock.assert_not_called()
    assert result["messages_seen"] == 1
    assert result["messages_persisted"] == 0
    assert result["duplicates_skipped"] == 1


@pytest.mark.asyncio
async def test_trace_enrichment_changed_existing_uses_canonical_insert(
    trace_enrichment_server: tuple[DaemonAPIServer, sqlite3.Connection, FakeTraceClient],
    trace_service: DaemonAccountTraceService,
) -> None:
    server, conn, client = trace_enrichment_server
    seed_existing_message_bundle(conn, text="old")
    client.messages_by_dialog = {222: [fake_message(message_id=1, text="new")]}

    with patch("mcp_telegram.daemon_account_trace.insert_messages_with_fts") as insert_mock:
        result = await trace_service._trace_enrich_visible_dialogs(
            101,
            [{"dialog_id": 222, "strategy": "dialog_scan", "topic_id": None}],
        )

    insert_mock.assert_called_once()
    assert result["messages_persisted"] == 1


@pytest.mark.asyncio
async def test_trace_enrichment_floodwait_persists_retry_fragment(
    trace_enrichment_server: tuple[DaemonAPIServer, sqlite3.Connection, FakeTraceClient],
    trace_service: DaemonAccountTraceService,
) -> None:
    server, conn, client = trace_enrichment_server
    client.exc = FloodWaitError(None, 120)

    result = await trace_service._trace_enrich_visible_dialogs(
        101,
        [{"dialog_id": 222, "strategy": "dialog_scan", "topic_id": None}],
    )

    fragment = cast(
        tuple[str, str, int] | None,
        conn.execute(
            "SELECT status, last_error, next_retry_at FROM trace_coverage_fragments WHERE target_user_id = 101"
        ).fetchone(),
    )
    assert fragment is not None
    assert result["fragment_status_counts"]["flood_wait"] == 1
    assert fragment[0] == "flood_wait"
    assert fragment[1] == "FloodWaitError:120"
    assert fragment[2] > int(datetime.now(tz=UTC).timestamp())


@pytest.mark.asyncio
async def test_trace_enrichment_deadline_exhaustion_persists_budget_gap(
    trace_enrichment_server: tuple[DaemonAPIServer, sqlite3.Connection, FakeTraceClient],
    trace_service: DaemonAccountTraceService,
) -> None:
    server, conn, _client = trace_enrichment_server

    result = await trace_service._trace_enrich_visible_dialogs(
        101,
        [{"dialog_id": 222, "strategy": "dialog_scan", "topic_id": None}],
        deadline_ms=0,
    )

    fragment = cast(
        tuple[str, str] | None,
        conn.execute("SELECT status, last_error FROM trace_coverage_fragments WHERE target_user_id = 101").fetchone(),
    )
    assert fragment is not None
    assert result["fragment_status_counts"]["budget_exceeded"] == 1
    assert tuple(fragment) == ("budget_exceeded", "BudgetExceeded:0")


@pytest.mark.asyncio
async def test_trace_enrichment_channel_is_unsupported_without_search(
    trace_enrichment_server: tuple[DaemonAPIServer, sqlite3.Connection, FakeTraceClient],
    trace_service: DaemonAccountTraceService,
) -> None:
    server, conn, client = trace_enrichment_server

    result = await trace_service._trace_enrich_visible_dialogs(
        101,
        [{"dialog_id": -100123, "strategy": "signature_only", "topic_id": None}],
    )

    fragment = cast(
        tuple[str] | None,
        conn.execute("SELECT status FROM trace_coverage_fragments WHERE target_user_id = 101").fetchone(),
    )
    assert fragment is not None
    assert result["fragment_status_counts"]["unsupported"] == 1
    assert fragment[0] == "unsupported"
    assert client.calls == []


@pytest.mark.asyncio
async def test_trace_enrichment_generic_error_does_not_escape(
    trace_enrichment_server: tuple[DaemonAPIServer, sqlite3.Connection, FakeTraceClient],
    trace_service: DaemonAccountTraceService,
) -> None:
    server, conn, client = trace_enrichment_server
    client.exc = RuntimeError("boom")

    result = await trace_service._trace_enrich_visible_dialogs(
        101,
        [{"dialog_id": 222, "strategy": "dialog_scan", "topic_id": None}],
    )

    fragment = cast(
        tuple[str, str] | None,
        conn.execute("SELECT status, last_error FROM trace_coverage_fragments WHERE target_user_id = 101").fetchone(),
    )
    assert fragment is not None
    assert result["fragment_status_counts"]["partial"] == 1
    assert tuple(fragment) == ("partial", "RuntimeError")


def test_trace_enrichment_path_has_no_direct_messages_insert_string() -> None:
    source = Path("src/mcp_telegram/daemon_api.py").read_text(encoding="utf-8")
    assert "INSERT INTO messages" not in source


@pytest.mark.asyncio
async def test_trace_account_observed_mode_does_not_call_enrichment(
    trace_enrichment_server: tuple[DaemonAPIServer, sqlite3.Connection, FakeTraceClient],
    trace_service: DaemonAccountTraceService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, conn, _client = trace_enrichment_server
    seed_entity(conn, entity_id=101, name="Me", username="me")
    conn.commit()
    enrich = AsyncMock()
    monkeypatch.setattr(DaemonAccountTraceService, "_trace_enrich_visible_dialogs", enrich)

    result = _dict(await trace_service._trace_account_messages({"exact_account_id": 101, "coverage_goal": "observed"}))

    assert result["ok"] is True
    enrich.assert_not_called()
    provenance = _dict(_dict(result["data"])["provenance"])
    assert provenance["local_cache_writes"] == 0


@pytest.mark.asyncio
async def test_trace_account_best_effort_calls_enrichment_and_reruns_db_query(
    trace_enrichment_server: tuple[DaemonAPIServer, sqlite3.Connection, FakeTraceClient],
    trace_service: DaemonAccountTraceService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, conn, _client = trace_enrichment_server
    seed_entity(conn, entity_id=101, name="Me", username="me")
    seed_dialog(conn, dialog_id=222, name="Alice", dialog_type="User")
    seed_synced_dialog(conn, dialog_id=222)
    conn.commit()

    async def fake_enrich(
        _self: object,
        _target_user_id: int,
        _candidates: list[dict[str, object]],
        **_kwargs: object,
    ) -> dict[str, object]:
        seed_message(conn, dialog_id=222, message_id=1, sent_at=1_700_000_000, text="from enrichment", sender_id=101)
        conn.commit()
        return {
            "dialogs_attempted": 1,
            "dialogs_skipped": 0,
            "messages_seen": 1,
            "messages_persisted": 1,
            "duplicates_skipped": 0,
            "deadline_ms": 15_000,
            "concurrency": 2,
            "coverage_bounds": {"max_dialogs": 10, "max_per_dialog": 100, "deadline_ms": 15_000},
            "fragment_status_counts": {"complete": 1},
        }

    monkeypatch.setattr(DaemonAccountTraceService, "_trace_enrich_visible_dialogs", fake_enrich)

    result = await trace_service._trace_account_messages(
        {"exact_account_id": 101, "coverage_goal": "best_effort_visible", "exact_dialog_id": 222}
    )

    assert result["ok"] is True
    data = _dict(_dict(result)["data"])
    groups = cast(list[dict[str, object]], data["groups"])
    evidence = cast(list[dict[str, object]], groups[0]["evidence"])
    provenance = _dict(data["provenance"])
    enrichment = _dict(provenance["enrichment"])
    coverage_bounds = _dict(enrichment["coverage_bounds"])
    assert evidence[0]["text"] == "from enrichment"
    assert provenance["local_cache_writes"] == 1
    assert coverage_bounds["max_dialogs"] == 10
    assert coverage_bounds["max_per_dialog"] == 100
    assert coverage_bounds["deadline_ms"] == 15_000
