from __future__ import annotations

import time
from typing import cast
from unittest.mock import AsyncMock

import pytest

from mcp_telegram.account_trace_sqlite import apply_trace_dialog_identities
from mcp_telegram.dialog_selector import required_dialog_selector
from test_account_trace_sqlite import _conn as trace_conn
from test_daemon_api import (
    _activity_data,
    _make_db_with_activity,
    _make_db_with_dialogs,
    _seed_dialog_row,
    _TestClient,
    make_server,
)


@pytest.mark.asyncio
async def test_dialog_username_selector_uses_canonical_bundle_and_never_resolves_remote() -> None:
    conn = _make_db_with_dialogs()
    _seed_dialog_row(conn, 7101, name="Current Name", type_="supergroup")
    conn.execute(
        "UPDATE dialogs SET username='current_name',identity_complete=1,identity_observed_at=1700000000 "
        "WHERE dialog_id=7101"
    )
    conn.execute(
        "INSERT INTO entities (id,type,name,username,name_normalized,updated_at) "
        "VALUES (7101,'Channel','Stale Name','deleted_name','stale name',1700000000)"
    )
    conn.commit()
    client = _TestClient()
    client.get_entity = AsyncMock(side_effect=AssertionError("dialog identity selectors are local"))
    server = make_server(conn, client)

    found = await server._resolve_dialog_id(required_dialog_selector(dialog="@current_name"))
    stale = await server._resolve_dialog_id(required_dialog_selector(dialog="@deleted_name"))

    assert isinstance(found, int)
    assert found == 7101
    assert isinstance(stale, dict) and stale["error"] == "dialog_directory_incomplete"
    client.get_entity.assert_not_awaited()


@pytest.mark.asyncio
async def test_activity_uses_canonical_dialog_name_and_type_without_entity_row() -> None:
    server = make_server(_make_db_with_activity())
    now = int(time.time())
    with server._conn:
        server._conn.execute(
            "INSERT INTO dialogs (dialog_id,name,type,username,identity_complete,identity_observed_at) "
            "VALUES (7102,'Canonical Forum','forum','canonical_forum',1,?)",
            (now,),
        )
        server._conn.execute(
            "INSERT INTO messages (dialog_id,message_id,sent_at,text,out,is_service,is_deleted) "
            "VALUES (7102,1,?,'post',1,0,0)",
            (now - 10,),
        )

    response = await server._dispatch({"method": "get_my_recent_activity", "dialog_kinds": ["forum"]})
    comments = cast(list[dict[str, object]], _activity_data(response)["comments"])

    assert comments[0]["dialog_name"] == "Canonical Forum"
    assert comments[0]["dialog_type"] == "forum"
    assert comments[0]["dialog_category"] == "forum"


def test_trace_labels_and_types_use_one_owner_bundle() -> None:
    conn = trace_conn()
    try:
        conn.execute(
            "INSERT INTO dialogs "
            "(dialog_id,name,type,username,identity_complete,identity_observed_at) "
            "VALUES (7103,'Canonical Channel','channel','canonical_channel',1,1700000000)"
        )
        rows = apply_trace_dialog_identities(conn, [{"dialog_id": 7103, "dialog_title": "old", "dialog_type": "old"}])

        assert rows == [{"dialog_id": 7103, "dialog_title": "Canonical Channel", "dialog_type": "channel"}]
    finally:
        conn.close()
