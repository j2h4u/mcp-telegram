import asyncio

import pytest

from mcp_telegram.telegram_rpc import TelegramRpcAdmissionDeferred, _TransportBoundaryState
from tests.test_telegram_rpc import (
    _gate,
    _reset_process_policy,  # noqa: F401 - local autouse fixture
    _scripted_authenticator,  # noqa: F401 - local autouse fixture
)


@pytest.mark.asyncio
async def test_connect_cannot_supersede_pending_public_disconnect() -> None:
    gate = _gate(initially_ready=False)
    closing = gate.disconnect()
    owner = gate._owned_disconnect
    try:
        with pytest.raises(TelegramRpcAdmissionDeferred):
            gate._begin_transport_connect()
        assert gate._owned_disconnect is owner
        assert gate._reconnect_quiescing
    finally:
        await asyncio.gather(closing, return_exceptions=True)
        gate._transport_state = _TransportBoundaryState.DISCONNECTED
        await gate.close_rpc_scheduler()
