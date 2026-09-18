"""Focused contracts for the bounded Telegram reading gateways."""

from __future__ import annotations

import ast
import sqlite3
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Protocol, cast
from unittest.mock import AsyncMock

import pytest
from telethon.tl import types

from mcp_telegram.flood import TelegramRpcThrottled
from mcp_telegram.models import ReadMessage
from mcp_telegram.reactions.telegram_adapter import TelethonTelegramReactionGateway
from mcp_telegram.telegram_demand import AcquisitionKind, RpcAttemptBudgetExhaustedError
from mcp_telegram.telegram_fact_queries import enrich_read_at, persist_read_at, stale_read_at_ids
from mcp_telegram.telegram_fragments import FragmentContextService, TelethonTelegramFragmentGateway
from mcp_telegram.telegram_history import TelethonTelegramHistoryGateway
from mcp_telegram.telegram_read_receipts import TelethonTelegramReadReceiptGateway
from mcp_telegram.telegram_reading import (
    GatewayFailure,
    GatewayFailureKind,
    ReadDateFetchResult,
)
from mcp_telegram.telegram_rpc_consumers import DemandKind
from mcp_telegram.telegram_rpc_scheduler import (
    RpcAdmissionClosedError,
    TelegramRpcSource,
    current_rpc_scope,
    rpc_scope,
)
from tests.history_enrollment_helpers import seed_full_history_enrollment


class _RequestWithPeer(Protocol):
    peer: object


def _message(message_id: int, *, reaction: bool = True) -> SimpleNamespace:
    reactions = None
    if reaction:
        reactions = SimpleNamespace(
            results=[SimpleNamespace(reaction=SimpleNamespace(emoticon="👍"), count=2)],
        )
    return SimpleNamespace(
        id=message_id,
        date=datetime.fromtimestamp(1_700_000_000 + message_id, tz=UTC),
        message=f"message {message_id}",
        sender_id=101,
        sender=SimpleNamespace(first_name="Alice"),
        media=None,
        reply_to=None,
        reactions=reactions,
        edit_date=None,
        grouped_id=None,
        out=False,
        post_author=None,
    )


class _HistoryClient:
    def __init__(self, messages: list[object], *, error: Exception | None = None) -> None:
        self.messages = messages
        self.error = error
        self.calls: list[tuple[int, dict[str, object]]] = []

    async def iter_messages(self, dialog_id: int, **kwargs: object) -> AsyncIterator[object]:
        self.calls.append((dialog_id, kwargs))
        for message in self.messages:
            yield message
        if self.error is not None:
            raise self.error


def _seed_synced(conn: sqlite3.Connection, dialog_id: int) -> None:
    conn.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, read_inbox_max_id) VALUES (?, 'synced', 0)",
        (dialog_id,),
    )
    seed_full_history_enrollment(conn, dialog_id, enabled=True)
    conn.commit()


def _seed_enrollment(conn: sqlite3.Connection, dialog_id: int, *, enabled: bool = True) -> None:
    """Seed the v34 durable authorization for direct fact-persistence tests."""
    seed_full_history_enrollment(conn, dialog_id, enabled=enabled)
    conn.commit()


@pytest.mark.asyncio
async def test_history_gateway_projects_ordered_messages_and_preserves_query_options() -> None:
    client = _HistoryClient([_message(10), _message(12)])

    result = await TelethonTelegramHistoryGateway(client).fetch_history(
        42,
        {"limit": 2, "reverse": True},
        self_id=101,
    )

    assert client.calls == [(42, {"limit": 2, "reverse": True})]
    assert result.failure is None
    assert [message["message_id"] for message in result.messages] == [10, 12]
    assert [message["dialog_id"] for message in result.messages] == [42, 42]
    assert [message["effective_sender_id"] for message in result.messages] == [101, 101]


@pytest.mark.asyncio
async def test_history_gateway_returns_structured_failure_without_partial_messages() -> None:
    client = _HistoryClient([_message(10)], error=ValueError("dialog not available"))

    result = await TelethonTelegramHistoryGateway(client).fetch_history(42, {"limit": 2}, self_id=101)

    assert result.messages == ()
    assert result.failure == GatewayFailure(
        kind=GatewayFailureKind.INVALID_TARGET,
        error_type="ValueError",
        error_message="dialog not available",
        retryable=False,
    )


@pytest.mark.parametrize(
    ("peer", "expected"),
    [
        (types.PeerUser(user_id=11), 11),
        (types.PeerChat(chat_id=22), -22),
        (types.PeerChannel(channel_id=33), -1000000000033),
        (SimpleNamespace(user_id=44), 44),
        (SimpleNamespace(chat_id=55), -55),
        (SimpleNamespace(channel_id=66), -1000000000066),
        (SimpleNamespace(), None),
        (None, None),
    ],
)
def test_reaction_gateway_peer_id_normalizes_telethon_and_narrow_doubles(peer: object, expected: int | None) -> None:
    assert TelethonTelegramReactionGateway._peer_id(peer) == expected


@pytest.mark.asyncio
async def test_fragment_gateway_preserves_fixed_window_and_normalized_persistence(
    make_synced_db: Callable[[], sqlite3.Connection],
) -> None:
    conn = make_synced_db()
    scopes: list[tuple[TelegramRpcSource, DemandKind, AcquisitionKind | None]] = []

    async def get_input_entity(_dialog_id: int) -> str:
        scope = current_rpc_scope()
        assert scope.demand_kind is not None
        scopes.append((scope.source, scope.demand_kind, scope.acquisition_kind))
        return "entity"

    async def get_messages(_entity: object, *, ids: list[int]) -> list[object | None]:
        scope = current_rpc_scope()
        assert scope.demand_kind is not None
        scopes.append((scope.source, scope.demand_kind, scope.acquisition_kind))
        return [_message(10), None, _message(12)]

    client = SimpleNamespace(
        get_input_entity=AsyncMock(side_effect=get_input_entity),
        get_messages=AsyncMock(side_effect=get_messages),
    )

    result = await FragmentContextService(conn, TelethonTelegramFragmentGateway(client)).fetch(42, 10, 6)

    assert result.ok is True
    cast(AsyncMock, client.get_messages).assert_awaited_once_with("entity", ids=[7, 8, 9, 10, 11, 12])
    assert conn.execute("SELECT status FROM synced_dialogs WHERE dialog_id=42").fetchone() == ("fragment",)
    assert conn.execute("SELECT message_id, text FROM messages ORDER BY message_id").fetchall() == [
        (10, "message 10"),
        (12, "message 12"),
    ]
    assert conn.execute("SELECT message_id, emoji, count FROM message_reactions ORDER BY message_id").fetchall() == [
        (10, "👍", 2),
        (12, "👍", 2),
    ]
    assert conn.execute("SELECT dialog_id, message_id FROM messages_fts ORDER BY message_id").fetchall() == [
        (42, 10),
        (42, 12),
    ]
    assert scopes == [
        (
            TelegramRpcSource.MESSAGE_READ_FALLBACK,
            DemandKind.MESSAGE_READ_FALLBACK,
            AcquisitionKind.ENTITY_LOOKUP,
        ),
        (
            TelegramRpcSource.MESSAGE_READ_FALLBACK,
            DemandKind.MESSAGE_READ_FALLBACK,
            AcquisitionKind.MESSAGE_LOOKUP,
        ),
    ]


@pytest.mark.asyncio
async def test_fragment_gateway_translates_floodwait_without_partial_persistence(
    make_synced_db: Callable[[], sqlite3.Connection],
) -> None:
    conn = make_synced_db()
    flood = TelegramRpcThrottled(retry_after_seconds=17)
    client = SimpleNamespace(
        get_input_entity=AsyncMock(side_effect=flood),
        get_messages=AsyncMock(),
    )

    result = await FragmentContextService(conn, TelethonTelegramFragmentGateway(client)).fetch(42, 10, 6)

    assert result.ok is False
    assert result.failure == GatewayFailure(
        kind=GatewayFailureKind.FLOOD_WAIT,
        error_type="TelegramRpcThrottled",
        error_message=str(flood),
        retryable=True,
        retry_after=17,
    )
    assert conn.execute("SELECT status FROM synced_dialogs WHERE dialog_id=42").fetchone() == ("fragment",)
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone() == (0,)


@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_read_receipt_gateway_keeps_telegram_date_nullable() -> None:
    class Client:
        def __init__(self) -> None:
            self.resolved: list[object] = []

        async def get_input_entity(self, entity: object) -> object:
            self.resolved.append(entity)
            return SimpleNamespace(peer_id=entity)

        async def __call__(self, request: object) -> object:
            assert request.__class__.__name__ == "GetOutboxReadDateRequest"
            assert cast(_RequestWithPeer, request).peer == SimpleNamespace(peer_id=42)
            return SimpleNamespace(date=datetime.fromtimestamp(1_700_000_200, tz=UTC))

    client = Client()
    result = await TelethonTelegramReadReceiptGateway(client).fetch_outbox_read_date(42, 10)
    assert result == ReadDateFetchResult(read_at=1_700_000_200, status="complete")
    assert client.resolved == [42]


@pytest.mark.asyncio
async def test_read_receipt_gateway_propagates_scheduler_close() -> None:
    with rpc_scope(TelegramRpcSource.READ_RECEIPT_PROBE) as scope:
        closed = RpcAdmissionClosedError(scope, "scheduler closed")

    class Client:
        async def get_input_entity(self, _entity: object) -> object:
            raise closed

    with pytest.raises(RpcAdmissionClosedError, match="scheduler closed"):
        await TelethonTelegramReadReceiptGateway(Client()).fetch_outbox_read_date(42, 10)


@pytest.mark.asyncio
async def test_read_receipt_gateway_propagates_attempt_budget_exhaustion() -> None:
    exhausted = RpcAttemptBudgetExhaustedError("slice budget exhausted")

    class Client:
        async def get_input_entity(self, _entity: object) -> object:
            return object()

        async def __call__(self, _request: object) -> object:
            raise exhausted

    with pytest.raises(RpcAttemptBudgetExhaustedError, match="slice budget exhausted"):
        await TelethonTelegramReadReceiptGateway(Client()).fetch_outbox_read_date(42, 10)


@pytest.mark.asyncio
async def test_read_at_enrichment_only_probes_outgoing_user_dm_and_never_falls_back(
    make_synced_db: Callable[[], sqlite3.Connection],
) -> None:
    conn = make_synced_db()
    _seed_enrollment(conn, 42)
    calls: list[int] = []

    class Gateway:
        async def fetch_outbox_read_date(self, entity: object, message_id: int) -> ReadDateFetchResult:
            _ = entity
            calls.append(message_id)
            return ReadDateFetchResult(status="missing")

    messages = [
        ReadMessage(message_id=1, sent_at=1_000, dialog_id=42, out=1),
        ReadMessage(message_id=2, sent_at=1_001, dialog_id=42, out=0),
        ReadMessage(message_id=3, sent_at=1_002, dialog_id=99, out=1),
    ]
    enriched = await enrich_read_at(
        conn, Gateway(), 42, messages, dialog_type="user", read_at_ttl_seconds=600, checked_at=2_000
    )
    assert [message.read_at for message in enriched] == [None, None, None]
    assert calls == [1]
    assert conn.execute(
        "SELECT read_at, checked_at, status FROM message_read_facts WHERE dialog_id=42 AND message_id=1"
    ).fetchone() == (None, 2_000, "missing")

    # Group-shaped dialogs are outside the private User-DM contract and must
    # not trigger a Telegram RPC even when they contain an outgoing message.
    await enrich_read_at(
        conn, Gateway(), 42, messages, dialog_type="supergroup", read_at_ttl_seconds=600, checked_at=2_001
    )
    assert calls == [1]


@pytest.mark.asyncio
async def test_read_at_suppresses_terminal_duplicate_ids_in_source_order(
    make_synced_db: Callable[[], sqlite3.Connection],
) -> None:
    """A terminal fact is reused for later duplicate source rows."""
    conn = make_synced_db()
    _seed_enrollment(conn, 42)
    calls: list[int] = []

    class Gateway:
        async def fetch_outbox_read_date(
            self,
            entity: object,
            message_id: int,
        ) -> ReadDateFetchResult:
            _ = entity
            calls.append(message_id)
            return ReadDateFetchResult(
                read_at=1_700_000_000 + message_id,
                status="complete",
            )

    messages = [
        ReadMessage(message_id=3, sent_at=1_000, dialog_id=42, out=1),
        ReadMessage(message_id=1, sent_at=1_001, dialog_id=42, out=1),
        ReadMessage(message_id=3, sent_at=1_002, dialog_id=42, out=1),
    ]
    enriched = await enrich_read_at(
        conn,
        Gateway(),
        42,
        messages,
        dialog_type="user",
        read_at_ttl_seconds=600,
        checked_at=3_000,
    )

    assert calls == [3, 1]
    assert [message.message_id for message in enriched] == [3, 1, 3]
    assert [message.read_at for message in enriched] == [
        1_700_000_003,
        1_700_000_001,
        1_700_000_003,
    ]


@pytest.mark.asyncio
async def test_read_at_rejects_boolean_ttl_before_shortcut(make_synced_db: Callable[[], sqlite3.Connection]) -> None:
    """TTL validation applies even when no gateway or eligible dialog exists."""
    with pytest.raises(ValueError, match="read_at_ttl_seconds"):
        await enrich_read_at(
            make_synced_db(),
            None,
            42,
            [],
            dialog_type="supergroup",
            read_at_ttl_seconds=True,
        )


def test_read_receipt_at_exact_ttl_age_is_stale(make_synced_db: Callable[[], sqlite3.Connection]) -> None:
    """A terminal read receipt remains suppressed at every TTL age."""
    conn = make_synced_db()
    dialog_id, message_id, now, ttl = 42, 1, 2_000, 600
    conn.execute(
        "INSERT INTO message_read_facts (dialog_id, message_id, read_at, checked_at, status) VALUES (?, ?, ?, ?, ?)",
        (dialog_id, message_id, 1_700_000_000, now - ttl, "complete"),
    )
    conn.commit()

    assert stale_read_at_ids(conn, dialog_id, [message_id], now - ttl) == []


def test_read_at_persistence_is_terminal_and_monotonic(make_synced_db: Callable[[], sqlite3.Connection]) -> None:
    conn = make_synced_db()
    seed_full_history_enrollment(conn, 42, enabled=True)

    persist_read_at(conn, 42, 1, read_at=None, checked_at=200, status="missing")
    persist_read_at(conn, 42, 1, read_at=None, checked_at=199, status="unavailable")
    assert conn.execute(
        "SELECT read_at, checked_at, status FROM message_read_facts WHERE dialog_id=42 AND message_id=1"
    ).fetchone() == (None, 200, "missing")

    persist_read_at(conn, 42, 1, read_at=1_700_000_001, checked_at=100, status="complete")
    persist_read_at(conn, 42, 1, read_at=None, checked_at=300, status="unavailable")
    assert conn.execute(
        "SELECT read_at, checked_at, status FROM message_read_facts WHERE dialog_id=42 AND message_id=1"
    ).fetchone() == (1_700_000_001, 100, "complete")

    with pytest.raises(ValueError, match="non-null read_at"):
        persist_read_at(conn, 42, 2, read_at=None, checked_at=1, status="complete")


@pytest.mark.asyncio
async def test_read_at_projects_telegram_date_for_own_dm_only(
    make_synced_db: Callable[[], sqlite3.Connection],
) -> None:
    conn = make_synced_db()
    _seed_enrollment(conn, 42)
    calls: list[int] = []

    class Gateway:
        async def fetch_outbox_read_date(self, entity: object, message_id: int) -> ReadDateFetchResult:
            _ = entity
            calls.append(message_id)
            return ReadDateFetchResult(read_at=1_700_000_200, status="complete")

    messages = [
        ReadMessage(message_id=8, sent_at=1_000, dialog_id=42, out=1),
        ReadMessage(message_id=9, sent_at=1_001, dialog_id=42, out=0),
    ]
    enriched = await enrich_read_at(
        conn, Gateway(), 42, messages, dialog_type="user", read_at_ttl_seconds=600, checked_at=3_100
    )

    assert calls == [8]
    assert [message.read_at for message in enriched] == [1_700_000_200, None]
    assert conn.execute(
        "SELECT read_at, status FROM message_read_facts WHERE dialog_id=42 AND message_id=8"
    ).fetchone() == (1_700_000_200, "complete")


@pytest.mark.asyncio
async def test_read_at_unavailable_is_nullable_but_probe_status_is_persisted(
    make_synced_db: Callable[[], sqlite3.Connection],
) -> None:
    conn = make_synced_db()
    _seed_enrollment(conn, 42)
    calls: list[int] = []

    class Gateway:
        async def fetch_outbox_read_date(self, entity: object, message_id: int) -> ReadDateFetchResult:
            _ = entity
            calls.append(message_id)
            return ReadDateFetchResult(status="unavailable")

    messages = [ReadMessage(message_id=7, sent_at=1_000, dialog_id=42, out=1)]
    enriched = await enrich_read_at(
        conn, Gateway(), 42, messages, dialog_type="user", read_at_ttl_seconds=600, checked_at=3_000
    )

    assert calls == [7]
    assert enriched[0].read_at is None
    # checked_at/status are local probe metadata, never an event timestamp.
    assert conn.execute(
        "SELECT read_at, checked_at, status FROM message_read_facts WHERE dialog_id=42 AND message_id=7"
    ).fetchone() == (None, 3_000, "unavailable")


@pytest.mark.asyncio
async def test_read_at_stops_after_persistence_operational_error_with_prior_commits(
    make_synced_db: Callable[[], sqlite3.Connection], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Earlier probe facts remain committed when a later local write cannot persist."""
    import mcp_telegram.telegram_fact_queries as fact_queries

    conn = make_synced_db()
    _seed_enrollment(conn, 42)
    calls: list[int] = []

    class Gateway:
        async def fetch_outbox_read_date(self, entity: object, message_id: int) -> ReadDateFetchResult:
            _ = entity
            calls.append(message_id)
            return ReadDateFetchResult(read_at=1_700_000_000 + message_id, status="complete")

    original_persist = fact_queries.persist_read_at

    def fail_second_persist(  # noqa: PLR0913
        connection: sqlite3.Connection,
        current_dialog_id: int,
        message_id: int,
        *,
        read_at: int | None,
        checked_at: int,
        status: str,
    ) -> None:
        if message_id == 2:
            raise sqlite3.OperationalError("simulated missing table")
        original_persist(
            connection,
            current_dialog_id,
            message_id,
            read_at=read_at,
            checked_at=checked_at,
            status=status,
        )

    monkeypatch.setattr(fact_queries, "persist_read_at", fail_second_persist)
    messages = [
        ReadMessage(message_id=1, sent_at=1_000, dialog_id=42, out=1),
        ReadMessage(message_id=2, sent_at=1_001, dialog_id=42, out=1),
        ReadMessage(message_id=3, sent_at=1_002, dialog_id=42, out=1),
    ]

    enriched = await enrich_read_at(
        conn, Gateway(), 42, messages, dialog_type="user", read_at_ttl_seconds=600, checked_at=3_000
    )

    assert calls == [1, 2]
    assert [message.read_at for message in enriched] == [1_700_000_001, None, None]
    assert conn.execute(
        "SELECT read_at, checked_at, status FROM message_read_facts WHERE dialog_id=42 AND message_id=1"
    ).fetchone() == (1_700_000_001, 3_000, "complete")
    assert conn.execute(
        "SELECT COUNT(*) FROM message_read_facts WHERE dialog_id=42 AND message_id IN (2, 3)"
    ).fetchone() == (0,)


def test_reading_query_modules_have_no_telethon_or_client_calls() -> None:
    paths = [
        Path("src/mcp_telegram/reading/service.py"),
        Path("src/mcp_telegram/reading/sqlite_projection.py"),
        Path("src/mcp_telegram/daemon_dialog_queries.py"),
        Path("src/mcp_telegram/reading/sqlite_projection.py"),
        Path("src/mcp_telegram/telegram_reading.py"),
    ]
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports = [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
        imports.extend(node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom))
        assert all("telethon" not in name.lower() and "floodwait" not in name.lower() for name in imports), path
        direct_calls = [
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        ]
        assert not {"get_messages", "iter_messages", "get_input_entity"} & set(direct_calls), path
