"""Local-only activity linked-chat fact resolution tests."""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from typing import cast

import pytest

from mcp_telegram.activity_peer_resolve import linked_chat_retry_at, resolve_input_peer, resolve_linked_chat_id
from mcp_telegram.linked_chat_fact import LinkedChatFact, LinkedChatState, linked_chat_fact_owner
from mcp_telegram.sync_db import _apply_migrations


@contextlib.contextmanager
def _make_db() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(":memory:")
    try:
        _apply_migrations(conn)
        yield conn
    finally:
        conn.close()


class _Client:
    def __init__(self, input_peer: object = object()) -> None:
        self.input_peer = input_peer
        self.input_calls: list[int] = []
        self.rpc_calls: list[object] = []

    async def get_input_entity(self, dialog_id: int) -> object:
        self.input_calls.append(dialog_id)
        return self.input_peer

    async def __call__(self, request: object) -> object:
        self.rpc_calls.append(request)
        raise AssertionError("activity linked-chat resolution must not send RPC")


@pytest.mark.asyncio
async def test_resolve_input_peer_uses_client_session_lookup() -> None:
    peer = object()
    client = _Client(peer)
    assert await resolve_input_peer(client, -10042) is peer
    assert client.input_calls == [-10042]


@pytest.mark.asyncio
async def test_unknown_fact_is_pending_and_cold_demand_is_idempotent() -> None:
    with _make_db() as conn:
        client = _Client()
        fact = await resolve_linked_chat_id(client, conn, -10042)  # type: ignore[arg-type]
        assert fact.state is LinkedChatState.UNKNOWN
        assert fact.refresh_pending
        generation = cast(
            tuple[int, int | None] | None,
            conn.execute(
                "SELECT generation, pending_generation FROM linked_chat_fact_state WHERE channel_id=?", (-10042,)
            ).fetchone(),
        )
        assert generation is not None and generation[0] == generation[1]

        repeated = await resolve_linked_chat_id(client, conn, -10042)  # type: ignore[arg-type]
        assert repeated.state is LinkedChatState.UNKNOWN and repeated.refresh_pending
        assert (
            cast(
                tuple[int, int | None] | None,
                conn.execute(
                    "SELECT generation, pending_generation FROM linked_chat_fact_state WHERE channel_id=?", (-10042,)
                ).fetchone(),
            )
            == generation
        )
        assert client.input_calls == []
        assert client.rpc_calls == []


@pytest.mark.asyncio
async def test_confirmed_no_link_and_stale_known_link_remain_distinct_from_unknown() -> None:
    with _make_db() as conn:
        with conn:
            linked_chat_fact_owner.capture_generation(conn, -10043)
            assert linked_chat_fact_owner.publish(conn, -10043, 0, None, 100)
            conn.execute("INSERT INTO linked_chat_fact_state(channel_id,generation) VALUES(-10044,0)")
            conn.execute(
                "INSERT INTO dialogs(dialog_id,linked_chat_id,linked_chat_resolved_at) VALUES(-10044,-10099,90)"
            )
            conn.execute("UPDATE linked_chat_fact_state SET pending_generation=0,retry_at=200 WHERE channel_id=-10044")

        no_link = await resolve_linked_chat_id(_Client(), conn, -10043)  # type: ignore[arg-type]
        stale_link = await resolve_linked_chat_id(_Client(), conn, -10044)  # type: ignore[arg-type]
        assert no_link.state is LinkedChatState.KNOWN_NONE and no_link.linked_chat_id is None
        assert stale_link.state is LinkedChatState.KNOWN_LINK and stale_link.linked_chat_id == -10099
        assert stale_link.refresh_pending and stale_link.retry_at == 200


def test_missing_pending_retry_uses_one_bounded_fallback() -> None:
    fact = LinkedChatFact(LinkedChatState.UNKNOWN, None, None, True, 50, None)
    assert linked_chat_retry_at(fact, now=100) == linked_chat_fact_owner.retry_policy.effective_retry_at(None, 50, 100)


def test_missing_retry_and_request_use_owner_bounded_fallback() -> None:
    fact = LinkedChatFact(LinkedChatState.UNKNOWN, None, None, True, None, None)
    assert linked_chat_retry_at(fact, now=100) == linked_chat_fact_owner.retry_policy.effective_retry_at(
        None, None, 100
    )
