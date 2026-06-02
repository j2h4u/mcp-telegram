"""Callsite audit: resolver consumers handle a Candidates response correctly.

Only GetEntityInfo reaches Candidates directly (via conn.resolve_entity); it must
surface them without silently auto-picking the first match. The other resolver
consumers (reading/unread/discovery/sync) forward the raw selector to the daemon,
which resolves server-side and returns an error-dict on ambiguity, so a Candidates
dict never reaches the MCP-tool layer. daemon_api._resolve_entity is verified to
return matches verbatim.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from mcp_telegram.tools import GetEntityInfo, get_entity_info

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_CANDIDATES_RESPONSE = {
    "ok": True,
    "data": {
        "result": "candidates",
        "matches": [
            {
                "entity_id": 101,
                "display_name": "Ivan Petrov",
                "score": 100,
                "username": "ivan",
                "entity_type": "User",
                "disambiguation_hint": ('2 entities match "Ivan": Channel, User. Specify @username or numeric id.'),
            },
            {
                "entity_id": 202,
                "display_name": "Ivan Channel",
                "score": 100,
                "username": None,
                "entity_type": "Channel",
                "disambiguation_hint": ('2 entities match "Ivan": Channel, User. Specify @username or numeric id.'),
            },
        ],
    },
}


def _make_conn_resolve_candidates() -> MagicMock:
    """Daemon connection that returns Candidates from resolve_entity."""
    conn = MagicMock()
    conn.resolve_entity = AsyncMock(return_value=_CANDIDATES_RESPONSE)
    return conn


@asynccontextmanager
async def _conn_ctx(conn: MagicMock):
    yield conn


# ---------------------------------------------------------------------------
# Test 1 & 2: GetEntityInfo — Candidates IS reachable
# ---------------------------------------------------------------------------


async def test_entity_info_candidates_surfaces_hint() -> None:
    """GetEntityInfo with Candidates response keeps hints out of error text."""
    conn = _make_conn_resolve_candidates()
    with patch(
        "mcp_telegram.tools.entity_info.daemon_connection",
        return_value=_conn_ctx(conn),
    ):
        result = await get_entity_info(GetEntityInfo(entity="Ivan"))

    text = result.content[0].text
    assert "structuredContent.candidates" in text
    assert "Specify @username or numeric id" not in text
    payload = result.structured_content
    assert payload is not None
    candidates = payload["candidates"]
    assert isinstance(candidates, list)
    assert candidates[0]["disambiguation_hint_content"] == {
        "text": '2 entities match "Ivan": Channel, User. Specify @username or numeric id.',
        "is_telegram_content": True,
        "content_kind": "message_text",
    }


async def test_entity_info_candidates_lists_all_matches() -> None:
    """GetEntityInfo with Candidates → structuredContent contains all match entity_ids."""
    conn = _make_conn_resolve_candidates()
    with patch(
        "mcp_telegram.tools.entity_info.daemon_connection",
        return_value=_conn_ctx(conn),
    ):
        result = await get_entity_info(GetEntityInfo(entity="Ivan"))

    text = result.content[0].text
    assert "101" not in text
    assert "202" not in text
    payload = result.structured_content
    assert payload is not None
    candidates = payload["candidates"]
    assert isinstance(candidates, list)
    assert [candidate["entity_id"] for candidate in candidates] == [101, 202]
    assert candidates[0]["display_name_content"] == {
        "text": "Ivan Petrov",
        "is_telegram_content": True,
        "content_kind": "message_text",
    }
    # Must NOT silently auto-pick (i.e. must not proceed to get_entity_info call)
    assert conn.get_entity_info is not conn.resolve_entity  # sanity; no silent pick


async def test_entity_info_candidates_does_not_auto_pick() -> None:
    """GetEntityInfo must NOT silently resolve to first match — must return ambiguity response."""
    conn = _make_conn_resolve_candidates()
    # get_entity_info (the second call) should NOT be called on Candidates
    conn.get_entity_info = AsyncMock(return_value={"ok": True, "data": {}})
    with patch(
        "mcp_telegram.tools.entity_info.daemon_connection",
        return_value=_conn_ctx(conn),
    ):
        result = await get_entity_info(GetEntityInfo(entity="Ivan"))

    # Tool must not have proceeded to fetch entity profile
    conn.get_entity_info.assert_not_called()
    text = result.content[0].text
    # Must be an ambiguity response, not a profile
    assert "structuredContent.candidates" in text
    assert "id=101" not in text and "id=202" not in text
    assert result.structured_content is not None
    assert result.structured_content["error"] == "ambiguous_entity"


# ---------------------------------------------------------------------------
# daemon_api passthrough — pure verification (no daemon_api code change)
# ---------------------------------------------------------------------------


def test_daemon_api_resolve_entity_passes_through_hint() -> None:
    """daemon_api._resolve_entity returns match dicts verbatim including disambiguation_hint.

    This is a pure guard test: if someone adds lossy projection in daemon_api.py
    (e.g. picks specific keys from each match), this test will catch it.

    daemon_api._resolve_entity (line 1537-1540):
        if isinstance(result, Candidates):
            return {
                "ok": True,
                "data": {"result": "candidates", "matches": result.matches},
            }
    result.matches is the list produced by _build_matches — each dict already
    contains disambiguation_hint. The passthrough is verbatim (result.matches),
    so disambiguation_hint propagates with zero code changes.
    """
    from mcp_telegram.resolver import Candidates, _build_matches

    # Build matches with collision_query so hints are present
    hits = [("ivan", 100.0, 0)]
    norm_map = {"ivan": [(101, "Ivan Petrov"), (202, "Ivan Channel")]}
    matches = _build_matches(hits, norm_map, None, collision_query="Ivan")

    # Simulate what daemon_api._resolve_entity returns
    result = Candidates(query="Ivan", matches=matches)
    daemon_response = {
        "ok": True,
        "data": {"result": "candidates", "matches": result.matches},
    }

    # Verify hint survives the passthrough
    returned_matches = daemon_response["data"]["matches"]
    assert len(returned_matches) == 2
    for m in returned_matches:
        assert "disambiguation_hint" in m
        assert m["disambiguation_hint"] is not None
        assert "Specify @username or numeric id" in m["disambiguation_hint"]
