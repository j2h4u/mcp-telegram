import asyncio
from collections.abc import AsyncIterator

import pytest

from mcp_telegram.auth_scope import AUTH_SCOPE_VERSION, TelegramAuthScope
from mcp_telegram.telegram_demand import AcquisitionKind
from mcp_telegram.telegram_rpc_consumers import DemandKind
from mcp_telegram.telegram_rpc_scheduler import TelegramRpcSource, current_rpc_scope
from tests.test_daemon_api import make_server

CHANNEL = -1000000000042


@pytest.mark.asyncio
async def test_operator_request_has_one_attempt_scope_and_aggregate_result():
    server = make_server()
    server.self_id = 42
    await server._publish_auth_scope(TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99))

    async def verify(dialog_id: int, ids: tuple[int, ...], control: int) -> dict[str, object]:
        assert (dialog_id, ids, control) == (CHANNEL, (3, 4), 8)
        scope = current_rpc_scope()
        assert scope.source == TelegramRpcSource.MCP_INTERACTIVE
        assert scope.demand_kind == DemandKind.MCP_REMOTE_ACQUISITION
        assert scope.acquisition_kind == AcquisitionKind.MESSAGE_LOOKUP
        assert scope.deadline is not None
        assert scope.attempt_budget is not None and scope.attempt_budget.limit == 1
        return {"requested": 2, "newly_marked": 1, "not_marked": 1, "secret": "discard"}

    server.bind_channel_deletion_verifier(verify)
    result = await server._dispatch(
        {"method": "verify_channel_deletions", "dialog_id": CHANNEL, "message_ids": [3, 4], "control_id": 8}
    )
    assert result == {"ok": True, "data": {"requested": 2, "newly_marked": 1, "not_marked": 1}}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ids,control", [([], 8), ([3, 3], 8), ([3], 3), ([True], 8), ([2**31], 8), (list(range(1, 101)), 200)]
)
async def test_operator_rejects_invalid_inputs_before_owner(ids: list[int], control: int) -> None:
    server = make_server()
    result = await server._dispatch(
        {"method": "verify_channel_deletions", "dialog_id": CHANNEL, "message_ids": ids, "control_id": control}
    )
    assert result["error"] == "invalid_input"


@pytest.mark.asyncio
async def test_operator_requires_account_and_rejects_invalid_aggregate():
    server = make_server()
    request = {"method": "verify_channel_deletions", "dialog_id": CHANNEL, "message_ids": [3], "control_id": 8}
    assert (await server._dispatch(request))["error"] == "account_unverified"
    server.self_id = 42
    await server._publish_auth_scope(TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99))

    async def verify(*_args: object) -> dict[str, object]:
        return {"requested": 1, "newly_marked": 2, "not_marked": 0}

    server.bind_channel_deletion_verifier(verify)
    assert (await server._dispatch(request))["error"] == "verification_failed"


@pytest.mark.asyncio
async def test_operator_cancellation_propagates():
    server = make_server()
    server.self_id = 42
    await server._publish_auth_scope(TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99))

    async def verify(*_args: object) -> dict[str, object]:
        raise asyncio.CancelledError

    server.bind_channel_deletion_verifier(verify)
    with pytest.raises(asyncio.CancelledError):
        await server._dispatch(
            {"method": "verify_channel_deletions", "dialog_id": CHANNEL, "message_ids": [3], "control_id": 8}
        )


def test_cli_forwards_one_local_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    from contextlib import asynccontextmanager

    from typer.testing import CliRunner

    import mcp_telegram.daemon_client as client
    from mcp_telegram import app

    calls: list[dict[str, object]] = []

    class Connection:
        async def verify_channel_deletions(self, **kwargs: object) -> dict[str, object]:
            calls.append(kwargs)
            return {"ok": True, "data": {"requested": 2, "newly_marked": 1, "not_marked": 1}}

    @asynccontextmanager
    async def connection(**kwargs: object) -> AsyncIterator[Connection]:
        assert kwargs == {}
        yield Connection()

    monkeypatch.setattr(client, "daemon_connection", connection)
    result = CliRunner().invoke(
        app,
        [
            "verify-channel-deletions",
            "--dialog-id",
            str(CHANNEL),
            "--control-id",
            "8",
            "--message-id",
            "3",
            "--message-id",
            "4",
        ],
    )
    assert result.exit_code == 0, result.output
    assert calls == [{"dialog_id": CHANNEL, "message_ids": [3, 4], "control_id": 8}]
    assert '"newly_marked": 1' in result.output
