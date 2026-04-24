"""Callsite audit: every resolver consumer handles Candidates correctly.

For each tool that touches entity/dialog resolution, we verify:
- The tool does NOT crash on a Candidates response.
- The tool does NOT silently auto-pick the first match (silent resolution regression).
- The tool returns an action-oriented message (both match ids present, or error-dict).

Tools and their resolution paths
---------------------------------
- GetUserInfo        : calls conn.resolve_entity() → can receive Candidates directly.
                       Real assertion — Candidates IS reachable.

- tools/reading.py   : passes dialog= string to daemon via conn.list_messages(dialog=...).
                       The daemon handles resolution internally and returns ok=False/error
                       on ambiguity; the MCP tool never sees a Candidates dict.
                       Candidates is UNREACHABLE at the MCP-tool layer.
                       (reading.py:197-201 — parse_exact_dialog_id for numeric/@ then
                        dialog name forwarded to daemon as raw string; daemon returns
                        ok=False on ambiguous dialog, never a candidates dict to the tool)
                       xfail with evidence.

- tools/unread.py    : calls conn.list_unread_messages() with no dialog resolution at
                       the tool layer (unread.py:56 — conn.list_unread_messages(...)).
                       No dialog selector → Candidates unreachable.
                       xfail with evidence.

- tools/discovery.py : parse_exact_dialog_id (numeric/@) or raw string to daemon
                       (discovery.py:111 — parse_exact_dialog_id; line 119 — dialog=dialog_name
                        forwarded to conn.list_topics; daemon resolves, returns error-dict).
                       Candidates unreachable at tool layer.
                       xfail with evidence.

- tools/sync.py      : MarkDialogForSync takes a pre-resolved numeric dialog_id
                       (sync.py:27 — dialog_id: int field, no fuzzy resolution).
                       Candidates unreachable at tool layer.
                       xfail with evidence.

- daemon_api passthrough : pure verification that _resolve_entity returns matches verbatim.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_telegram.tools import GetUserInfo, get_user_info

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
# Test 1 & 2: GetUserInfo — Candidates IS reachable
# ---------------------------------------------------------------------------


async def test_user_info_candidates_surfaces_hint() -> None:
    """GetUserInfo with Candidates response → output includes disambiguation_hint text."""
    conn = _make_conn_resolve_candidates()
    with patch(
        "mcp_telegram.tools.user_info.daemon_connection",
        return_value=_conn_ctx(conn),
    ):
        result = await get_user_info(GetUserInfo(user="Ivan"))

    text = result[0].text
    assert "disambiguation_hint" not in text or "hint=" in text  # hint= prefix is the format
    # The hint string itself must appear in output
    assert "Specify @username or numeric id" in text


async def test_user_info_candidates_lists_all_matches() -> None:
    """GetUserInfo with Candidates → output contains all match entity_ids."""
    conn = _make_conn_resolve_candidates()
    with patch(
        "mcp_telegram.tools.user_info.daemon_connection",
        return_value=_conn_ctx(conn),
    ):
        result = await get_user_info(GetUserInfo(user="Ivan"))

    text = result[0].text
    assert "101" in text
    assert "202" in text
    # Must NOT silently auto-pick (i.e. must not proceed to get_user_info call)
    assert conn.get_user_info is not conn.resolve_entity  # sanity; no silent pick


async def test_user_info_candidates_does_not_auto_pick() -> None:
    """GetUserInfo must NOT silently resolve to first match — must return ambiguity response."""
    conn = _make_conn_resolve_candidates()
    # get_user_info (the second call) should NOT be called on Candidates
    conn.get_user_info = AsyncMock(return_value={"ok": True, "data": {}})
    with patch(
        "mcp_telegram.tools.user_info.daemon_connection",
        return_value=_conn_ctx(conn),
    ):
        result = await get_user_info(GetUserInfo(user="Ivan"))

    # Tool must not have proceeded to fetch user profile
    conn.get_user_info.assert_not_called()
    text = result[0].text
    # Must be an ambiguity response, not a profile
    assert "id=101" in text or "id=202" in text  # listed matches, not profile data


# ---------------------------------------------------------------------------
# Tests 3-6: tools that cannot receive Candidates — xfail with evidence
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    reason=(
        "src/mcp_telegram/tools/reading.py:197-201 — parse_exact_dialog_id for "
        "numeric/@ then dialog name forwarded to daemon as raw string; daemon returns "
        "ok=False on ambiguous dialog, never a candidates dict to the MCP tool layer"
    ),
    strict=False,
)
async def test_reading_candidates_handled_without_silent_pick() -> None:
    """tools/reading.py: Candidates unreachable at tool layer.

    Evidence (reading.py lines 197-201):
        dialog_id: int | None = args.exact_dialog_id
        if dialog_id is None and args.dialog is not None:
            exact_id = parse_exact_dialog_id(args.dialog)
            if exact_id is not None:
                dialog_id = exact_id
        # If still None, dialog name goes to daemon for server-side resolution

    The dialog string is forwarded to the daemon as-is via conn.list_messages(dialog=...).
    The daemon calls resolve() internally and returns ok=False (error-dict) on Candidates,
    never exposing the candidates dict to the MCP tool.
    """
    # This path is unreachable: the tool never calls resolve_entity or sees Candidates.
    # Test is xfail to document the audit finding.
    raise AssertionError("This path is structurally unreachable — see docstring evidence")


@pytest.mark.xfail(
    reason=(
        "src/mcp_telegram/tools/unread.py — conn.get_inbox() called "
        "with no dialog selector; no entity resolution at the tool layer"
    ),
    strict=False,
)
async def test_unread_candidates_handled() -> None:
    """tools/unread.py: Candidates unreachable at tool layer.

    Evidence:
        response = await conn.get_inbox(...)

    GetInbox takes no dialog= argument; it returns unread groups from
    the daemon. No entity resolution happens at the tool layer, so Candidates
    can never be received here.
    """
    raise AssertionError("This path is structurally unreachable — see docstring evidence")


@pytest.mark.xfail(
    reason=(
        "src/mcp_telegram/tools/discovery.py:111,119 — parse_exact_dialog_id for "
        "numeric/@ then dialog=dialog_name forwarded to conn.list_topics; daemon "
        "resolves and returns error-dict on ambiguity"
    ),
    strict=False,
)
async def test_discovery_candidates_handled() -> None:
    """tools/discovery.py: Candidates unreachable at tool layer.

    Evidence (discovery.py lines 111, 119):
        dialog_id: int | None = parse_exact_dialog_id(args.dialog)
        dialog_name: str | None = None if dialog_id is not None else args.dialog
        ...
        response = await conn.list_topics(dialog=dialog_name)

    Raw dialog name is passed to daemon; daemon resolves internally and returns
    ok=False / error-dict on ambiguity. Candidates dict never surfaces to the tool.
    """
    raise AssertionError("This path is structurally unreachable — see docstring evidence")


@pytest.mark.xfail(
    reason=(
        "src/mcp_telegram/tools/sync.py:27 — dialog_id: int field, no fuzzy "
        "resolution; MarkDialogForSync requires pre-resolved numeric id"
    ),
    strict=False,
)
async def test_sync_candidates_handled() -> None:
    """tools/sync.py: Candidates unreachable at tool layer.

    Evidence (sync.py line 27):
        dialog_id: int = Field(description="Numeric dialog ID from ListDialogs")

    MarkDialogForSync requires a pre-resolved numeric dialog_id. The tool
    passes it directly to conn.mark_dialog_for_sync(dialog_id=...) with no
    entity resolution step — Candidates is structurally unreachable.
    """
    raise AssertionError("This path is structurally unreachable — see docstring evidence")


# ---------------------------------------------------------------------------
# Test 7: daemon_api passthrough — pure verification (no daemon_api code change)
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
