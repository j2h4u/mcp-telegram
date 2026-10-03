from __future__ import annotations

import sqlite3

import pytest

from mcp_telegram import chat_export
from mcp_telegram.daemon_api import DaemonAPIServer
from mcp_telegram.telegram_demand import AcquisitionKind, current_demand_token
from mcp_telegram.telegram_rpc_consumers import DemandKind
from mcp_telegram.telegram_rpc_scheduler import TelegramRpcSource, current_rpc_scope


@pytest.mark.asyncio
async def test_export_chat_uses_its_registered_root_and_source(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = sqlite3.connect(":memory:")
    server = object.__new__(DaemonAPIServer)
    server._conn = conn
    monkeypatch.setattr(server, "_client", object(), raising=False)
    seen: list[tuple[DemandKind, TelegramRpcSource, sqlite3.Connection]] = []

    async def export_operation(_client: object, _req: object, db: sqlite3.Connection) -> dict[str, object]:
        scope = current_rpc_scope()
        seen.append((current_demand_token().kind, scope.source, db))
        return {"ok": True, "data": {}}

    monkeypatch.setattr(chat_export, "export_operation", export_operation)

    try:
        response = await server._dispatch({"method": "export_chat", "operation": "open", "dialog_id": -1})
        assert response == {"ok": True, "data": {}}
        assert seen == [(DemandKind.CHAT_EXPORT_OPERATION, TelegramRpcSource.CHAT_EXPORT, conn)]
        assert AcquisitionKind.ADMIN_LOG_PAGE.value == "admin_log_page"
        assert AcquisitionKind.PARTICIPANT_LOOKUP.value == "participant_lookup"
    finally:
        conn.close()
