"""An evidenced monoforum contact limit ends one attempt without erasing facts."""

import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from typing import cast

import pytest
from jsonschema import Draft202012Validator
from telethon.errors import RPCError
from telethon.errors.rpcbaseerrors import BadRequestError
from telethon.tl import types

from mcp_telegram import server
from mcp_telegram.auth_scope import AUTH_SCOPE_VERSION, TelegramAuthScope
from mcp_telegram.entity_profile.contracts import CHANNEL_CONTACT_UNSUPPORTED_REASON, ChannelReference
from mcp_telegram.entity_profile.refresh import scope_changed_retry_at
from mcp_telegram.flood import TelegramRpcThrottled
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.telegram_gateway import TelethonChannelProfileGateway
from mcp_telegram.telegram_rpc_scheduler import current_rpc_scope
from mcp_telegram.tools import entity_info
from tests.test_channel_contact_failure_identity import _PEER, Scenario, _PrivateRequest, _QueuedWriter, _run, _Session
from tests.test_channel_contact_failure_identity import scenario as scenario
from tests.test_channel_profile_telegram_gateway import _Client as _GatewayClient
from tests.test_profile_read_integration import registered_validator as registered_validator


def _seed(scenario: Scenario, *, retained: bool = True, owner: int = 7) -> None:
    conn, service, client = scenario
    detail: dict[str, object] = {"schema": 1, "id": _PEER, "type": "supergroup", "name": "Retained", "about": "Known"}
    contacts = [{"id": 5, "name": "Retained contact", "username": None}]
    if retained:
        detail.update(contacts_subscribed=contacts, contacts_subscribed_partial=True)
    with write_transaction(conn):
        conn.execute(
            "INSERT INTO entity_details(entity_id,detail_json,fetched_at,profile_owner_account_id) VALUES(?,?,10,?)",
            (_PEER, json.dumps(detail), owner),
        )
        if retained:
            conn.execute(
                "INSERT INTO entity_detail_sections(entity_id,section,status,observed_at,payload_json) "
                "VALUES(?,'contact_overlap','stale',10,?)",
                (_PEER, json.dumps(contacts)),
            )
    client.failure = BadRequestError(_PrivateRequest(), CHANNEL_CONTACT_UNSUPPORTED_REASON)
    gateway = TelethonChannelProfileGateway(client, now_provider=iter((100.25, 101.5)).__next__)
    service._deps = replace(service._deps, channel_profile_port=gateway, channel_reference_provider=gateway)


@pytest.mark.parametrize("retained", [True, False])
async def test_terminal_negative_keeps_facts_age_and_registered_local_projection(
    scenario: Scenario,
    retained: bool,
    monkeypatch: pytest.MonkeyPatch,
    registered_validator: Draft202012Validator,
) -> None:
    conn, service, client = scenario
    _seed(scenario, retained=retained)
    before = conn.execute("SELECT * FROM entity_details").fetchall()

    @asynccontextmanager
    async def connection():
        async def get_entity_info(**kwargs: object):
            return await service.get_entity_info(kwargs)

        yield SimpleNamespace(get_entity_info=get_entity_info)

    monkeypatch.setattr(entity_info, "_bounded_daemon_connection", connection)
    try:
        await _run(service)
        assert conn.execute("SELECT * FROM entity_details").fetchall() == before
        assert conn.execute(
            "SELECT status,next_section,generation,profile_revision,retry_at FROM entity_profile_refresh_state"
        ).fetchone() == ("pending", "avatar_history", 3, 0, None)
        row = cast(
            tuple[str, str, int | None, str | None, int | None, int, str, int, int, str] | None,
            conn.execute(
                "SELECT status,reason,observed_at,payload_json,retry_at,acquisition_generation,acquisition_outcome,"
                "observation_started_at,observation_completed_at,provenance_json FROM entity_detail_sections"
            ).fetchone(),
        )
        assert row is not None
        assert row[:3] == ("unavailable", CHANNEL_CONTACT_UNSUPPORTED_REASON, 10 if retained else None)
        assert (row[3] is not None) is retained
        assert row[4:9] == (None, 3, "unavailable", 100, 101)
        assert json.loads(row[9]) == {
            "endpoint": "channels.GetParticipants",
            "rpc_code": 400,
            "rpc_symbol": CHANNEL_CONTACT_UNSUPPORTED_REASON,
        }
        result = await entity_info.get_entity_info(entity_info.GetEntityInfo(exact_entity_id=_PEER))
        wire_result = server._project_tool_result("get_entity_info", result, server._CallTelemetry())
        wire = cast(dict[str, object], wire_result.structured_content)
        registered_validator.validate(wire)
        assert not wire_result.is_error and wire_result.content == []
        sections = cast(dict[str, dict[str, object]], wire["sections"])
        assert sections["contact_overlap"]["status"] == "unavailable"
        assert sections["contact_overlap"]["reason"] == CHANNEL_CONTACT_UNSUPPORTED_REASON
        assert sections["contact_overlap"].get("observed_at") == ("1970-01-01T00:00:10+00:00" if retained else None)
        specific = cast(dict[str, object], wire["type_specific"])
        contacts = cast(dict[str, object], specific["contacts_subscribed"])
        assert contacts["available"] is retained
        if retained:
            items = cast(list[dict[str, object]], contacts["items"])
            assert items[0]["id"] == 5 and items[0]["name"] == "Retained contact"
        else:
            assert contacts.get("items") is None
        assert len(client.requests) == 1
    finally:
        await service.shutdown()


@pytest.mark.parametrize("owner", [None, 8])
async def test_terminal_cannot_rebind_missing_or_other_owner(scenario: Scenario, owner: int | None) -> None:
    conn, service, _client = scenario
    if owner is None:
        _seed(scenario)
        with write_transaction(conn):
            conn.execute("DELETE FROM entity_details")
    else:
        _seed(scenario, owner=owner)
    before = conn.execute("SELECT * FROM entity_detail_sections").fetchall()
    details = conn.execute("SELECT * FROM entity_details").fetchall()
    try:
        await _run(service)
        # Pair mode capture is an existing local queue action, not a negative receipt.
        assert conn.execute("SELECT next_section,reason FROM entity_profile_refresh_state").fetchone() == (
            "contact_overlap",
            None,
        )
        assert "CHANNEL_MONOFORUM_UNSUPPORTED" not in "\n".join(conn.iterdump())
        assert conn.execute("SELECT * FROM entity_detail_sections").fetchall() == before
        assert conn.execute("SELECT * FROM entity_details").fetchall() == details
    finally:
        await service.shutdown()


async def test_terminal_writer_rechecks_queued_account(scenario: Scenario) -> None:
    conn, service, client = scenario
    _seed(scenario)
    before = conn.execute("SELECT * FROM entity_details").fetchall()
    writer = _QueuedWriter(conn)
    service._deps = replace(service._deps, writer=cast(SQLiteWriter, writer))
    client.before_failure = lambda: setattr(writer, "pause", True)
    task = asyncio.create_task(_run(service))
    try:
        await asyncio.wait_for(writer.waiting.wait(), timeout=2)
        service._deps = replace(
            service._deps, full_user_auth_scope=lambda: TelegramAuthScope(AUTH_SCOPE_VERSION, 8, 2, 99)
        )
        writer.release.set()
        await task
        assert conn.execute("SELECT * FROM entity_details").fetchall() == before
        assert conn.execute("SELECT reason,retry_at,next_section FROM entity_profile_refresh_state").fetchone() == (
            "auth_scope_changed",
            scope_changed_retry_at(100),
            "contact_overlap",
        )
        assert "CHANNEL_MONOFORUM_UNSUPPORTED" not in "\n".join(conn.iterdump())
    finally:
        writer.release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await service.shutdown()


@pytest.mark.parametrize("fence", ["generation", "page_cursor"])
async def test_terminal_stale_cursor_cannot_publish(scenario: Scenario, fence: str) -> None:
    conn, service, client = scenario
    _seed(scenario)
    before = conn.execute("SELECT * FROM entity_detail_sections").fetchall()

    def invalidate() -> None:
        with write_transaction(conn):
            if fence == "generation":
                conn.execute("UPDATE entity_profile_refresh_state SET generation=4")
            else:
                conn.execute("UPDATE entity_profile_refresh_state SET page_cursor_json='{}'")

    client.before_failure = invalidate
    try:
        await _run(service)
        assert conn.execute("SELECT * FROM entity_detail_sections").fetchall() == before
        assert conn.execute("SELECT next_section FROM entity_profile_refresh_state").fetchone() == ("contact_overlap",)
    finally:
        await service.shutdown()


async def test_terminal_cursor_failure_rolls_back_negative_receipt(scenario: Scenario) -> None:
    conn, service, _client = scenario
    _seed(scenario)
    before = conn.execute("SELECT * FROM entity_detail_sections").fetchall()
    with write_transaction(conn):
        conn.execute(
            "CREATE TRIGGER reject_advance BEFORE UPDATE OF next_section ON entity_profile_refresh_state "
            "BEGIN SELECT RAISE(ABORT,'test cursor rejection'); END"
        )
    try:
        with pytest.raises(sqlite3.IntegrityError, match="test cursor rejection"):
            await _run(service)
        assert conn.execute("SELECT * FROM entity_detail_sections").fetchall() == before
    finally:
        await service.shutdown()


class _SuccessfulClient:
    session = _Session()

    async def __call__(self, _request: object) -> object:
        budget = current_rpc_scope().attempt_budget
        assert budget is not None
        budget.debit()
        return types.channels.ChannelParticipants(count=1, participants=[], chats=[], users=[types.User(5)])


async def test_explicit_invalidation_can_reacquire_after_unsupported_attempt(scenario: Scenario) -> None:
    conn, service, _client = scenario
    _seed(scenario)
    try:
        await _run(service)
        assert conn.execute("SELECT next_section FROM entity_profile_refresh_state").fetchone() == ("avatar_history",)
        service._profiles.request_section_refresh(_PEER, ("contact_overlap",), now=200, pair_mode="disabled")
        gateway = TelethonChannelProfileGateway(_SuccessfulClient(), now_provider=lambda: 200.0)
        service._deps = replace(
            service._deps,
            channel_profile_port=gateway,
            channel_reference_provider=gateway,
            now_provider=lambda: 200.0,
            dm_peer_ids=lambda: {5},
        )
        await _run(service)
        row = cast(
            tuple[str, str | None, int, str, int | None] | None,
            conn.execute(
                "SELECT status,reason,observed_at,payload_json,retry_at FROM entity_detail_sections"
            ).fetchone(),
        )
        assert row is not None and row[:3] == ("fresh", None, 200)
        assert json.loads(row[3]) == [{"id": 5, "name": None, "username": None}]
        assert row[4] is None
        assert conn.execute("SELECT next_section FROM entity_profile_refresh_state").fetchone() == ("avatar_history",)
    finally:
        await service.shutdown()


@pytest.mark.parametrize(
    ("message", "code"),
    [("OTHER_ERROR", 400), (CHANNEL_CONTACT_UNSUPPORTED_REASON, 500), ("BAD\nprivate", 400)],
)
async def test_gateway_other_rpc_errors_still_propagate(message: str, code: int) -> None:
    error = RPCError(_PrivateRequest(), message, code=code)
    gateway = TelethonChannelProfileGateway(_GatewayClient(error))
    with pytest.raises(RPCError) as caught:
        await gateway.fetch_channel_contact_overlap(ChannelReference(_PEER, 456))
    assert caught.value is error


async def test_gateway_flood_wait_still_propagates() -> None:
    error = TelegramRpcThrottled(retry_after_seconds=7)
    gateway = TelethonChannelProfileGateway(_GatewayClient(error))
    with pytest.raises(TelegramRpcThrottled) as caught:
        await gateway.fetch_channel_contact_overlap(ChannelReference(_PEER, 456))
    assert caught.value is error
