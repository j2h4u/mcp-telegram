"""Canonical local entity names remain resolvable until changed or cleared."""

from __future__ import annotations

from typing import cast

import pytest

from tests.test_daemon_api import _insert_entity, _make_db_with_entities, _response_data, make_server


@pytest.mark.asyncio
@pytest.mark.parametrize("entity_type", ["User", "Bot", "user", "bot", "Channel", "group"])
async def test_resolve_entity_uses_old_name_for_every_entity_type(entity_type: str) -> None:
    conn = _make_db_with_entities()
    _insert_entity(
        conn,
        101,
        entity_type=entity_type,
        name="Canonical Name",
        name_normalized="canonical name",
        updated_at=1,
    )

    result = await make_server(conn)._dispatch({"method": "resolve_entity", "query": "Canonical Name"})

    assert _response_data(result)["result"] == "resolved"
    assert _response_data(result)["entity_id"] == 101


@pytest.mark.asyncio
async def test_resolve_entity_stops_using_cleared_username_and_renamed_name() -> None:
    conn = _make_db_with_entities()
    _insert_entity(
        conn,
        101,
        entity_type="user",
        name="Old Name",
        name_normalized="old name",
        username="oldname",
        updated_at=1,
    )
    conn.execute(
        "UPDATE entities SET name = ?, name_normalized = ?, username = NULL WHERE id = ?",
        ("Quantum Blue", "quantum blue", 101),
    )
    conn.commit()
    server = make_server(conn)

    old_name = await server._dispatch({"method": "resolve_entity", "query": "Old Name"})
    old_username = await server._dispatch({"method": "resolve_entity", "query": "@oldname"})
    new_name = await server._dispatch({"method": "resolve_entity", "query": "Quantum Blue"})

    assert _response_data(old_name)["result"] == "not_found"
    assert _response_data(old_username)["result"] == "not_found"
    assert _response_data(new_name)["result"] == "resolved"
    assert cast(dict[str, object], new_name["data"])["entity_id"] == 101
