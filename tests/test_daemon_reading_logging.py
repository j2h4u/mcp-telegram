"""Logging regressions for daemon reading fallbacks."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Mapping
from typing import cast

import pytest

from mcp_telegram.daemon_reading import (
    DaemonReadingDeps,
    DaemonReadingService,
    _ListMessagesTelegramRequest,
    _next_telegram_offset,
    _object_to_int,
    _object_to_int_or_none,
    _row_sequence,
    _row_value,
    _SearchMessagesRequest,
    _select_telegram_batch,
    _telegram_batch_cap_reached,
    _TelegramBatchCapContext,
    _TelegramBatchRequest,
)
from mcp_telegram.models import ReadMessage
from mcp_telegram.pagination import HistoryDirection, decode_navigation_token
from mcp_telegram.telegram_fragments import FragmentContextService, TelethonTelegramFragmentGateway
from mcp_telegram.telegram_history import TelethonTelegramHistoryGateway
from mcp_telegram.telegram_reactions import ReactionFreshener, TelethonTelegramReactionGateway
from mcp_telegram.telegram_reading import HistoryFetchResult, ReactionFreshness, TelegramHistoryGateway


class _EntityMissingClient:
    async def get_messages(self, entity: object, ids: list[int]) -> object:
        _ = (entity, ids)
        return None

    def iter_messages(self, dialog_id: int, **kwargs: object) -> AsyncIterator[object]:
        _ = (dialog_id, kwargs)

        async def _gen() -> AsyncIterator[object]:
            raise ValueError("Could not find the input entity for PeerUser(user_id=123)")
            yield object()

        return _gen()


class _TestLogger:
    def __init__(self) -> None:
        self.warning_calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
        self.exception_calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def debug(self, msg: str, *args: object, **kwargs: object) -> None:
        _ = (msg, args, kwargs)
        return

    def info(self, msg: str, *args: object, **kwargs: object) -> None:
        _ = (msg, args, kwargs)
        return

    def warning(self, msg: str, *args: object, **kwargs: object) -> None:
        self.warning_calls.append((msg, args, kwargs))

    def exception(self, msg: str, *args: object, **kwargs: object) -> None:
        self.exception_calls.append((msg, args, kwargs))


class _PagedHistoryGateway:
    def __init__(self, pages: list[tuple[dict[str, object], ...]]) -> None:
        self.pages = pages
        self.calls: list[dict[str, object]] = []

    async def fetch_history(
        self, dialog_id: int, kwargs: Mapping[str, object], self_id: int | None
    ) -> HistoryFetchResult:
        _ = (dialog_id, self_id)
        self.calls.append(dict(kwargs))
        page = self.pages.pop(0) if self.pages else ()
        return HistoryFetchResult(messages=page)


class _NoopReactionFreshener:
    async def refresh(self, dialog_id: int, peer_id: int, message_ids: list[int]) -> object:
        _ = (dialog_id, peer_id, message_ids)
        return object()


@pytest.mark.parametrize(
    ("value", "default", "expected"),
    [(17, 0, 17), ("18", 0, 18), (None, 0, 0), (None, 9, 9)],
)
def test_typed_row_conversion_helpers_cover_nullable_and_string_values(
    value: object | None, default: int, expected: int
) -> None:
    assert _object_to_int(value, default) == expected


@pytest.mark.parametrize(("value", "expected"), [(17, 17), ("18", 18), (None, None)])
def test_typed_nullable_row_conversion_helper_covers_all_values(value: object | None, expected: int | None) -> None:
    assert _object_to_int_or_none(value) == expected


def test_row_access_helpers_cover_sequence_lookup_and_fallback() -> None:
    row = {"message_id": 17}
    assert _row_sequence((17, "hello"))[0] == 17
    assert _row_value(row, "message_id") == 17
    assert _row_value(row, "missing", "fallback") == "fallback"
    assert _row_value(object(), "missing", "fallback") == "fallback"


def test_read_state_per_dialog_skips_non_dm_and_zero_dialogs() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE entities (id INTEGER PRIMARY KEY, type TEXT NOT NULL);
        CREATE TABLE synced_dialogs (
            dialog_id INTEGER PRIMARY KEY,
            read_inbox_max_id INTEGER,
            read_outbox_max_id INTEGER
        );
        CREATE TABLE messages (
            dialog_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            sent_at INTEGER NOT NULL,
            out INTEGER NOT NULL,
            is_deleted INTEGER NOT NULL,
            is_service INTEGER NOT NULL
        );
        INSERT INTO entities VALUES (7, 'User'), (8, 'Channel');
        INSERT INTO synced_dialogs VALUES (7, 10, 20);
        INSERT INTO messages VALUES (7, 11, 1700000000, 0, 0, 0), (7, 21, 1700000100, 1, 0, 0);
        """
    )
    service = DaemonReadingService(
        DaemonReadingDeps(
            conn=conn,
            sync_db_path=None,
            self_id=1,
            resolve_dialog_id=lambda _dialog_id, _dialog: asyncio.sleep(0, result=0),
            fragment_context=cast(FragmentContextService, object()),
            reaction_freshener=cast(ReactionFreshener, object()),
            history_gateway=cast(TelegramHistoryGateway, object()),
            logger=_TestLogger(),
            rid=lambda: "",
        )
    )
    try:
        states = service._read_state_per_dialog(
            [
                ReadMessage(message_id=11, sent_at=1700000000, dialog_id=7, text="in"),
                ReadMessage(message_id=21, sent_at=1700000100, dialog_id=7, text="out", out=1),
                ReadMessage(message_id=1, sent_at=1700000000, dialog_id=8, text="channel"),
                ReadMessage(message_id=2, sent_at=1700000000, dialog_id=0, text="none"),
            ]
        )
    finally:
        conn.close()

    assert set(states) == {7}
    assert states[7]["inbox_unread_count"] == 1
    assert states[7]["outbox_unread_count"] == 1


def test_telegram_batch_selection_filters_deduplicates_and_stops_at_limit() -> None:
    batch = (
        {"message_id": 1, "sent_at": 100},
        {"message_id": 1, "sent_at": 100},
        {"message_id": 2},
        {"message_id": 3, "sent_at": 200},
        {"message_id": 4, "sent_at": 300},
    )

    selection = _select_telegram_batch(
        _TelegramBatchRequest(
            batch=batch,
            seen_ids=frozenset({9}),
            current_count=1,
            limit=2,
            since_utc=150,
            until_utc=300,
        ),
    )

    assert selection.messages == (batch[3],)
    assert selection.seen_ids == frozenset({1, 2, 3, 9})
    assert selection.last_message_id == 4
    assert selection.last_raw_message is batch[4]

    empty = _select_telegram_batch(
        _TelegramBatchRequest(
            batch=(),
            seen_ids=frozenset(),
            current_count=0,
            limit=10,
            since_utc=None,
            until_utc=None,
        ),
    )
    assert empty.messages == ()
    assert empty.last_message_id == 0
    assert empty.last_raw_message is None


@pytest.mark.parametrize(
    ("last_message_id", "current_offset", "expected"),
    [(0, None, None), (12, 12, None), (12, None, 12)],
)
def test_telegram_offset_progression_stops_on_invalid_or_repeated_id(
    last_message_id: int,
    current_offset: int | None,
    expected: int | None,
) -> None:
    assert _next_telegram_offset(last_message_id, current_offset) == expected


def test_telegram_batch_cap_predicate_requires_all_boundary_conditions() -> None:
    assert _telegram_batch_cap_reached(
        _TelegramBatchCapContext(
            has_time_bounds=True,
            message_count=1,
            limit=10,
            batch_size=10,
            batch_index=15,
            max_batches=16,
            last_message_id=20,
            previous_offset=10,
        ),
    )
    assert not _telegram_batch_cap_reached(
        _TelegramBatchCapContext(
            has_time_bounds=False,
            message_count=1,
            limit=10,
            batch_size=10,
            batch_index=15,
            max_batches=16,
            last_message_id=20,
            previous_offset=10,
        ),
    )


def _telegram_service(gateway: _PagedHistoryGateway, conn: sqlite3.Connection | None = None) -> DaemonReadingService:
    return DaemonReadingService(
        DaemonReadingDeps(
            conn=conn or sqlite3.connect(":memory:"),
            sync_db_path=None,
            self_id=1,
            resolve_dialog_id=lambda _dialog_id, _dialog: asyncio.sleep(0, result=0),
            fragment_context=cast(FragmentContextService, object()),
            reaction_freshener=cast(ReactionFreshener, object()),
            history_gateway=gateway,
            logger=_TestLogger(),
            rid=lambda: "",
        )
    )


def test_search_next_navigation_retains_scope_and_utc_bounds() -> None:
    service = _telegram_service(_PagedHistoryGateway([]))
    request = _SearchMessagesRequest(
        dialog_id=123,
        dialog=None,
        query="needle",
        limit=2,
        offset=4,
        navigation=None,
        message_state="sent",
        since_utc=100,
        until_utc=200,
    )
    messages = [
        ReadMessage(message_id=1, sent_at=150, dialog_id=123),
        ReadMessage(message_id=2, sent_at=151, dialog_id=123),
    ]
    try:
        scoped = service._search_next_navigation(request, messages, global_mode=False)
        global_result = service._search_next_navigation(request, messages, global_mode=True)
        partial = service._search_next_navigation(request, messages[:1], global_mode=False)
    finally:
        service._conn.close()

    assert scoped is not None
    scoped_token = decode_navigation_token(scoped)
    assert scoped_token.value == 6
    assert scoped_token.dialog_id == 123
    assert scoped_token.query == "needle"
    assert scoped_token.message_state == "sent"
    assert scoped_token.since_utc == 100
    assert scoped_token.until_utc == 200
    assert global_result is not None
    assert decode_navigation_token(global_result).dialog_id == 0
    assert partial is None


@pytest.mark.asyncio
async def test_search_scoped_result_applies_time_bounds_and_keeps_cursor_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE entities (id INTEGER PRIMARY KEY, name TEXT, type TEXT);
        CREATE TABLE messages (
            dialog_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            text TEXT,
            sent_at INTEGER NOT NULL,
            media_description TEXT,
            reply_to_msg_id INTEGER,
            sender_id INTEGER,
            sender_first_name TEXT,
            forum_topic_id INTEGER,
            is_service INTEGER NOT NULL,
            out INTEGER NOT NULL,
            is_deleted INTEGER NOT NULL DEFAULT 0
        );
        CREATE VIRTUAL TABLE messages_fts USING fts5(dialog_id UNINDEXED, message_id UNINDEXED, text);
        CREATE TABLE synced_dialogs (dialog_id INTEGER PRIMARY KEY, status TEXT NOT NULL);
        INSERT INTO synced_dialogs VALUES (123, 'synced');
        INSERT INTO messages (dialog_id, message_id, text, sent_at, is_service, out)
            VALUES (123, 10, 'needle in range', 150, 0, 0),
                   (123, 11, 'needle after range', 250, 0, 0);
        INSERT INTO messages_fts VALUES (123, 10, 'needle in range'), (123, 11, 'needle after range');
        """
    )
    service = _telegram_service(_PagedHistoryGateway([]), conn)

    async def fake_build(
        dialog_id: int,
        rows: list[object],
        *,
        log_rendered: bool,
    ) -> tuple[list[ReadMessage], ReactionFreshness]:
        assert dialog_id == 123
        assert log_rendered is False
        assert len(rows) == 1
        return [ReadMessage(message_id=10, sent_at=150, dialog_id=123, text="needle in range")], ReactionFreshness(
            requested_count=1,
            fresh_count=1,
            stale_count=0,
            refreshed_count=0,
            status="fresh",
        )

    service._build_read_messages_from_rows = fake_build  # type: ignore[method-assign]
    service._read_state_per_dialog = lambda _messages: {123: {"inbox_unread_count": 0}}  # type: ignore[method-assign]
    monkeypatch.setattr("mcp_telegram.daemon_reading._build_access_metadata", lambda *_args: {"dialog_access": "live"})
    request = _SearchMessagesRequest(
        dialog_id=123,
        dialog=None,
        query="needle",
        limit=1,
        offset=0,
        navigation=None,
        message_state="sent",
        since_utc=100,
        until_utc=200,
    )
    try:
        result = await service._search_messages_scoped_result(request, "needle")
    finally:
        conn.close()

    assert result["ok"] is True
    assert [message["message_id"] for message in result["data"]["messages"]] == [10]
    token = decode_navigation_token(result["data"]["next_navigation"])
    assert token.dialog_id == 123
    assert token.value == 1
    assert token.since_utc == 100
    assert token.until_utc == 200
    assert result["data"]["dialog_access"] == "live"


@pytest.mark.asyncio
async def test_list_messages_telegram_entity_miss_logs_structured_warning_without_traceback() -> None:
    logger = _TestLogger()
    conn = sqlite3.connect(":memory:")
    service = DaemonReadingService(
        DaemonReadingDeps(
            conn=conn,
            sync_db_path=None,
            self_id=1,
            resolve_dialog_id=lambda _dialog_id, _dialog: asyncio.sleep(0, result=0),
            fragment_context=FragmentContextService(conn, TelethonTelegramFragmentGateway(_EntityMissingClient())),
            reaction_freshener=ReactionFreshener(conn, TelethonTelegramReactionGateway(_EntityMissingClient())),
            history_gateway=TelethonTelegramHistoryGateway(_EntityMissingClient()),
            logger=logger,
            rid=lambda: " request_id=test-rid",
        )
    )

    try:
        result = await service._list_messages_from_telegram(
            _ListMessagesTelegramRequest(
                dialog_id=123,
                limit=10,
                direction="newest",
                direction_enum=HistoryDirection.NEWEST,
                anchor_msg_id=None,
                sender_id=None,
                topic_id=None,
                unread_after_id=None,
            )
        )
    finally:
        conn.close()

    assert result["ok"] is False
    assert result["error"] == "telegram_error"
    assert result["detail"] == {
        "error_type": "ValueError",
        "error_message": "Could not find the input entity for PeerUser(user_id=123)",
        "retryable": False,
    }
    assert len(logger.warning_calls) == 1
    _, _, kwargs = logger.warning_calls[0]
    assert kwargs.get("exc_info") is None
    assert logger.exception_calls == []


@pytest.mark.asyncio
async def test_build_read_messages_projects_persisted_reaction_events() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE message_reactions (
            dialog_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            emoji TEXT NOT NULL,
            count INTEGER NOT NULL,
            PRIMARY KEY (dialog_id, message_id, emoji)
        );
        CREATE TABLE entities (id INTEGER PRIMARY KEY, type TEXT NOT NULL);
        CREATE TABLE message_reaction_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            dialog_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            reactor_id INTEGER,
            emoji TEXT NOT NULL,
            reacted_at INTEGER,
            fetched_at INTEGER NOT NULL
        );
        CREATE TABLE message_reaction_event_status (
            dialog_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            checked_at INTEGER NOT NULL,
            status TEXT NOT NULL,
            returned_count INTEGER NOT NULL,
            PRIMARY KEY (dialog_id, message_id)
        );
        INSERT INTO message_reactions VALUES (7, 42, '👍', 2);
        INSERT INTO entities VALUES (7, 'User');
        INSERT INTO message_reaction_events
            (dialog_id, message_id, reactor_id, emoji, reacted_at, fetched_at)
        VALUES (7, 42, 99, '👍', 1700000000, 1700000100),
               (7, 42, NULL, '🔥', NULL, 1700000100);
        INSERT INTO message_reaction_event_status VALUES (7, 42, 1700000100, 'partial', 2);
        """
    )
    logger = _TestLogger()
    service = DaemonReadingService(
        DaemonReadingDeps(
            conn=conn,
            sync_db_path=None,
            self_id=1,
            resolve_dialog_id=lambda _dialog_id, _dialog: asyncio.sleep(0, result=0),
            fragment_context=cast(FragmentContextService, object()),
            reaction_freshener=cast(ReactionFreshener, _NoopReactionFreshener()),
            history_gateway=cast(TelegramHistoryGateway, object()),
            logger=logger,
            rid=lambda: "",
        )
    )
    try:
        messages, _freshness = await service._build_read_messages_from_rows(
            7,
            [{"message_id": 42, "sent_at": 1700000001, "dialog_id": 7}],
            log_rendered=False,
        )
    finally:
        conn.close()

    assert [(event.reactor_id, event.emoji, event.reacted_at) for event in messages[0].reaction_events] == [
        (99, "👍", 1700000000),
        (None, "🔥", None),
    ]
    assert messages[0].reaction_events_status == "partial"
    assert messages[0].reactions_display == "[👍×2]"


@pytest.mark.asyncio
async def test_list_messages_telegram_boundary_fetch_fills_page_and_continues() -> None:
    gateway = _PagedHistoryGateway(
        [
            ({"message_id": 1, "sent_at": 100}, {"message_id": 2, "sent_at": 101}),
            ({"message_id": 3, "sent_at": 200}, {"message_id": 4, "sent_at": 201}),
        ]
    )
    service = _telegram_service(gateway)
    try:
        result = await service._list_messages_from_telegram(
            _ListMessagesTelegramRequest(
                dialog_id=123,
                limit=2,
                direction="oldest",
                direction_enum=HistoryDirection.OLDEST,
                anchor_msg_id=None,
                sender_id=None,
                topic_id=None,
                unread_after_id=None,
                since_utc=200,
                until_utc=300,
            )
        )
    finally:
        service._conn.close()

    assert result["ok"] is True
    assert [row["message_id"] for row in result["data"]["messages"]] == [3, 4]
    assert result["data"]["next_navigation"] is not None
    assert len(gateway.calls) == 2
    assert gateway.calls[1]["offset_id"] == 2


@pytest.mark.asyncio
async def test_list_messages_telegram_boundary_cutoff_is_exclusive_and_bounded() -> None:
    gateway = _PagedHistoryGateway(
        [
            ({"message_id": 10, "sent_at": 300}, {"message_id": 9, "sent_at": 299}),
            (),
        ]
    )
    service = _telegram_service(gateway)
    try:
        result = await service._list_messages_from_telegram(
            _ListMessagesTelegramRequest(
                dialog_id=123,
                limit=2,
                direction="newest",
                direction_enum=HistoryDirection.NEWEST,
                anchor_msg_id=None,
                sender_id=None,
                topic_id=None,
                unread_after_id=None,
                since_utc=200,
                until_utc=300,
            )
        )
    finally:
        service._conn.close()

    assert result["ok"] is True
    assert [row["message_id"] for row in result["data"]["messages"]] == [9]
    assert result["data"]["next_navigation"] is None
    assert len(gateway.calls) == 2


@pytest.mark.asyncio
async def test_list_messages_telegram_boundary_cap_exposes_continuation() -> None:
    pages: list[tuple[dict[str, object], ...]] = [
        tuple({"message_id": page * 2 + offset, "sent_at": 100 + page * 2 + offset} for offset in (1, 2))
        for page in range(16)
    ]
    gateway = _PagedHistoryGateway(pages)
    service = _telegram_service(gateway)
    try:
        result = await service._list_messages_from_telegram(
            _ListMessagesTelegramRequest(
                dialog_id=123,
                limit=2,
                direction="oldest",
                direction_enum=HistoryDirection.OLDEST,
                anchor_msg_id=None,
                sender_id=None,
                topic_id=None,
                unread_after_id=None,
                since_utc=1000,
                until_utc=2000,
            )
        )
    finally:
        service._conn.close()

    assert result["ok"] is True
    assert result["data"]["messages"] == []
    continuation = result["data"]["next_navigation"]
    assert continuation is not None
    token = decode_navigation_token(continuation)
    assert token.value == 32
    assert token.since_utc == 1000
    assert token.until_utc == 2000
    assert len(gateway.calls) == 16
