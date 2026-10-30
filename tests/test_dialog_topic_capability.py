"""Topic capability is a retained fact on the local dialog surface."""

from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest
from jsonschema import validate

from mcp_telegram import server
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_read_model import SyncReadModelContractError
from mcp_telegram.tools._base import TOOL_REGISTRY
from mcp_telegram.tools.discovery import _strict_dialog
from tests.test_daemon import shared_writer as shared_writer
from tests.test_daemon_api import _insert_synced_dialog, _make_db_with_dialogs, _seed_dialog_row, make_server
from tests.test_tool_routing import _make_daemon_conn, _patch_daemon, assert_structured_success_payload
from tests.test_tools_discovery_list_dialogs import _make_dialog_dict


async def test_catalog_capability_through_daemon_and_registered_mcp() -> None:
    conn = _make_db_with_dialogs()
    try:
        for dialog_id, kind in ((1, "forum"), (2, "bot"), (3, "forum"), (4, "bot"), (5, "forum")):
            _seed_dialog_row(conn, dialog_id, type_=kind)
        _insert_synced_dialog(conn, 6, status="access_lost")
        conn.executemany(
            "INSERT INTO topic_catalog_state(dialog_id,applicable) VALUES (?,?)",
            [(1, 1), (2, 1), (3, 0), (4, None), (6, 0)],
        )
        conn.execute("INSERT INTO topic_metadata(dialog_id,topic_id,title) VALUES(3,7,'retained')")
        conn.commit()
        telegram_client = MagicMock()
        api = make_server(conn, telegram_client)
        telegram_client.reset_mock()
        api.self_id = 2
        before = conn.total_changes
        response = await api._list_dialogs({})
        rows = response["data"]["dialogs"]
        expected = {1: "supported", 2: "supported", 3: "unsupported", 4: "unknown", 5: "unknown", 6: "unsupported"}
        assert {row["id"]: row["topic_capability"] for row in rows} == expected
        assert conn.total_changes == before
        assert (
            conn.execute("SELECT title FROM topic_metadata WHERE dialog_id=3 AND topic_id=7").fetchone()[0]
            == "retained"
        )
        client = _make_daemon_conn(response)
        with _patch_daemon(client):
            result = await server.call_tool("list_dialogs", {})
        payload = assert_structured_success_payload(result)
        dialogs = cast(list[dict[str, object]], payload["dialogs"])
        assert {row["id"]: row["topic_capability"] for row in dialogs} == expected
        validate(payload, cast(dict[str, object], TOOL_REGISTRY["list_dialogs"].output_schema))
        assert conn.total_changes == before
        telegram_client.assert_not_called()
        assert telegram_client.mock_calls == []
    finally:
        conn.close()


async def test_account_mismatch_preserves_topic_fact_and_blocks_startup(
    tmp_path: Path, shared_writer: SQLiteWriter
) -> None:
    import asyncio

    from mcp_telegram.daemon import _acquire_startup_identity_before_updates
    from mcp_telegram.dialog_directory import CanonicalDialogDirectory
    from mcp_telegram.startup_identity import StartupIdentityUnavailableError
    from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema
    from tests.test_daemon import _ApiStub, _CadenceStub, _StartupIdentityClient, _typed_ctx

    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = _open_sync_db(path)
    conn.execute("UPDATE dialog_directory_state SET account_id=100")
    conn.execute("INSERT INTO topic_catalog_state(dialog_id,applicable) VALUES (7,1)")
    conn.commit()
    conn.close()
    shutdown = asyncio.Event()
    ctx = _typed_ctx(
        db_path=path,
        shutdown_event=shutdown,
        client=_StartupIdentityClient(101),
        api_server=_ApiStub(),
        self_profile_cadence=_CadenceStub(),
    )
    directory = CanonicalDialogDirectory(ctx.client, path, shutdown, writer=shared_writer)
    with pytest.raises(StartupIdentityUnavailableError):
        await _acquire_startup_identity_before_updates(ctx, directory)
    conn = _open_sync_db(path)
    try:
        assert conn.execute("SELECT applicable FROM topic_catalog_state WHERE dialog_id=7").fetchone() == (1,)
        assert conn.execute("SELECT account_id FROM dialog_directory_state").fetchone() == (100,)
    finally:
        conn.close()


@pytest.mark.parametrize("capability", [None, "invalid", 0])
def test_strict_dialog_rejects_missing_or_invalid_capability(capability: object) -> None:
    row = _make_dialog_dict()
    if capability is None:
        del row["topic_capability"]
    else:
        row["topic_capability"] = capability
    with pytest.raises(SyncReadModelContractError):
        _strict_dialog(row, 0)
