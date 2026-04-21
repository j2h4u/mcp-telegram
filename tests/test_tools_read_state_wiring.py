"""Tool-layer IPC wiring tests for Phase 39.3 read-state (Task 3).

These tests verify that tools/reading.py and tools/unread.py correctly extract
``read_state`` / ``dialog_type`` / ``read_state_per_dialog`` from daemon response
dicts and thread them into the formatters. Rendered-output assertions where
possible (HIGH-1 + HIGH-3 resolution).
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_telegram.tools.reading import (
    ListMessages,
    SearchMessages,
    _format_daemon_messages,
    _format_search_results,
    list_messages,
    search_messages,
)
from mcp_telegram.tools.unread import ListUnreadMessages, list_unread_messages


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_conn(method_name: str, response: dict) -> MagicMock:
    conn = MagicMock()
    setattr(conn, method_name, AsyncMock(return_value=response))
    return conn


@asynccontextmanager
async def _patched_connection(conn: MagicMock) -> AsyncIterator[MagicMock]:
    yield conn


def _patch_daemon(module_path: str, conn: MagicMock):
    return patch(module_path, return_value=_patched_connection(conn))


def _unix(h: int, m: int = 0) -> int:
    # 2026-04-21 fixed day anchor
    base = 1_776_000_000  # arbitrary stable unix ts
    return base + h * 3600 + m * 60


def _collapsed_rs() -> dict:
    return {
        "inbox_unread_count": 0,
        "inbox_cursor_state": "populated",
        "outbox_unread_count": 0,
        "outbox_cursor_state": "populated",
    }


def _split_rs_inbox_unread() -> dict:
    return {
        "inbox_unread_count": 2,
        "inbox_cursor_state": "populated",
        "inbox_oldest_unread_date": _unix(10, 0),
        "inbox_max_id_anchor": 5,
        "outbox_unread_count": 0,
        "outbox_cursor_state": "populated",
    }


def _dm_row(
    *,
    message_id: int = 1,
    out: int = 0,
    sent_at: int = 1_776_000_000,
    text: str = "hello",
    dialog_id: int = 111,
    dialog_name: str | None = None,
    sender_id: int | None = 111,
    sender_first_name: str | None = "Alice",
) -> dict:
    r: dict = {
        "message_id": message_id,
        "out": out,
        "sent_at": sent_at,
        "text": text,
        "sender_id": sender_id,
        "sender_first_name": sender_first_name,
        "is_service": 0,
        "dialog_id": dialog_id,
        "effective_sender_id": sender_id,
    }
    if dialog_name is not None:
        r["dialog_name"] = dialog_name
    return r


# ---------------------------------------------------------------------------
# _format_daemon_messages kwarg-capture & pass-through (reading.py)
# ---------------------------------------------------------------------------


def test_format_daemon_messages_passes_read_state_and_dialog_type_to_format_messages():
    """_format_daemon_messages threads kwargs into format_messages."""
    with patch("mcp_telegram.tools.reading.format_messages") as mock_fmt:
        mock_fmt.return_value = "rendered"
        rs = _split_rs_inbox_unread()
        _format_daemon_messages(
            [_dm_row(message_id=1)],
            read_state=rs,
            dialog_type="User",
        )
        assert mock_fmt.called
        kwargs = mock_fmt.call_args.kwargs
        assert kwargs.get("read_state") == rs
        assert kwargs.get("dialog_type") == "User"


# ---------------------------------------------------------------------------
# list_messages MCP tool (end-to-end via mocked daemon)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_messages_tool_renders_header_in_output() -> None:
    """Daemon response with read_state + dialog_type='User' → header appears in output."""
    response = {
        "ok": True,
        "data": {
            "messages": [_dm_row(message_id=1, text="hey")],
            "source": "sync",
            "read_state": _split_rs_inbox_unread(),
            "dialog_type": "User",
        },
    }
    conn = _make_conn("list_messages", response)
    with _patch_daemon("mcp_telegram.tools.reading.daemon_connection", conn):
        result = await list_messages(ListMessages(exact_dialog_id=111))
    text = result[0].text  # type: ignore[index]
    assert "[inbox: 2 unread from peer" in text


@pytest.mark.asyncio
async def test_list_messages_tool_renders_inline_marker_in_output() -> None:
    """When read_state + page rows set up a boundary/tail, at least one marker appears."""
    # Two incoming messages: id=3 (seen, <=cursor 5) and id=10 (unseen, >cursor 5)
    rs = {
        "inbox_unread_count": 1,
        "inbox_cursor_state": "populated",
        "inbox_oldest_unread_date": _unix(11, 0),
        "inbox_max_id_anchor": 5,
        "outbox_unread_count": 0,
        "outbox_cursor_state": "populated",
    }
    response = {
        "ok": True,
        "data": {
            "messages": [
                _dm_row(message_id=3, out=0, text="seen"),
                _dm_row(message_id=10, out=0, text="unseen"),
            ],
            "source": "sync",
            "read_state": rs,
            "dialog_type": "User",
        },
    }
    conn = _make_conn("list_messages", response)
    with _patch_daemon("mcp_telegram.tools.reading.daemon_connection", conn):
        result = await list_messages(ListMessages(exact_dialog_id=111))
    text = result[0].text  # type: ignore[index]
    assert any(
        marker in text
        for marker in [
            "[I read up to here]",
            "[unread by me]",
            "[peer read up to here]",
            "[unread by peer]",
        ]
    )


@pytest.mark.asyncio
async def test_list_messages_tool_non_dm_omits_header() -> None:
    """dialog_type='Channel' → no read-state header lines in output."""
    response = {
        "ok": True,
        "data": {
            "messages": [_dm_row(message_id=1)],
            "source": "sync",
            "read_state": None,
            "dialog_type": "Channel",
        },
    }
    conn = _make_conn("list_messages", response)
    with _patch_daemon("mcp_telegram.tools.reading.daemon_connection", conn):
        result = await list_messages(ListMessages(exact_dialog_id=-100500))
    text = result[0].text  # type: ignore[index]
    assert "[read-state:" not in text
    assert "[inbox:" not in text
    assert "[outbox:" not in text


@pytest.mark.asyncio
async def test_list_messages_tool_backward_compat_no_read_state_in_response() -> None:
    """Pre-39.3 daemon response (no read_state/dialog_type) → renders without header."""
    response = {
        "ok": True,
        "data": {
            "messages": [_dm_row(message_id=1)],
            "source": "sync",
        },
    }
    conn = _make_conn("list_messages", response)
    with _patch_daemon("mcp_telegram.tools.reading.daemon_connection", conn):
        result = await list_messages(ListMessages(exact_dialog_id=111))
    text = result[0].text  # type: ignore[index]
    assert "[read-state:" not in text
    assert "[inbox:" not in text
    assert "[outbox:" not in text


@pytest.mark.asyncio
async def test_list_messages_passes_dialog_type_through() -> None:
    """Kwarg-capture: format_messages receives dialog_type from daemon response."""
    response = {
        "ok": True,
        "data": {
            "messages": [_dm_row(message_id=1)],
            "source": "sync",
            "read_state": _collapsed_rs(),
            "dialog_type": "User",
        },
    }
    conn = _make_conn("list_messages", response)
    with patch("mcp_telegram.tools.reading.format_messages") as mock_fmt:
        mock_fmt.return_value = "rendered"
        with _patch_daemon("mcp_telegram.tools.reading.daemon_connection", conn):
            await list_messages(ListMessages(exact_dialog_id=111))
    assert mock_fmt.called
    kwargs = mock_fmt.call_args.kwargs
    assert kwargs.get("dialog_type") == "User"
    assert kwargs.get("read_state") == _collapsed_rs()


# ---------------------------------------------------------------------------
# _format_search_results per-dialog header block
# ---------------------------------------------------------------------------


def test_format_search_results_prepends_per_dialog_header_block() -> None:
    """read_state_per_dialog covering 2 DMs → header block with 2 entries precedes snippets."""
    rows = [
        _dm_row(message_id=1, dialog_id=111, dialog_name="Alice", text="hi A"),
        _dm_row(message_id=2, dialog_id=222, dialog_name="Bob", text="hi B"),
    ]
    rs_map = {
        111: _split_rs_inbox_unread(),
        222: _collapsed_rs(),
    }
    out = _format_search_results(
        rows, "hi", global_mode=True, read_state_per_dialog=rs_map
    )
    # Both dialogs' headers must appear
    assert "[inbox: 2 unread from peer" in out
    assert "[read-state: all caught up]" in out
    # Header block precedes snippet lines
    idx_header = min(out.find("[inbox:"), out.find("[read-state:"))
    idx_snippet = out.find("(msg_id:")
    assert idx_header >= 0
    assert idx_snippet >= 0
    assert idx_header < idx_snippet


def test_format_search_results_no_inline_markers_on_snippet_lines() -> None:
    """Documented trade-off: snippet lines do NOT get inline markers."""
    rows = [
        _dm_row(message_id=1, dialog_id=111, dialog_name="Alice", text="hit one"),
        _dm_row(message_id=10, dialog_id=111, dialog_name="Alice", text="hit two"),
    ]
    rs_map = {111: _split_rs_inbox_unread()}
    out = _format_search_results(
        rows, "hit", global_mode=True, read_state_per_dialog=rs_map
    )
    # Locate the snippet region (after the header block). Then scan for markers.
    snippet_start = out.find("(msg_id:")
    assert snippet_start >= 0
    snippet_region = out[snippet_start:]
    for marker in [
        "[I read up to here]",
        "[unread by me]",
        "[peer read up to here]",
        "[unread by peer]",
    ]:
        assert marker not in snippet_region


def test_format_search_results_skips_non_dm_in_header_block() -> None:
    """Only DMs in read_state_per_dialog get header lines (non-DM dialog_ids absent)."""
    rows = [
        _dm_row(message_id=1, dialog_id=111, dialog_name="Alice"),
        _dm_row(message_id=2, dialog_id=-100500, dialog_name="Channel X"),
    ]
    # Only DM 111 is in the map — non-DM absent per daemon contract.
    rs_map = {111: _collapsed_rs()}
    out = _format_search_results(
        rows, "hello", global_mode=True, read_state_per_dialog=rs_map
    )
    # Exactly one header line (for DM 111)
    assert out.count("[read-state: all caught up]") == 1
    # Channel X has no header
    assert "[inbox:" not in out
    assert "[outbox:" not in out


def test_format_search_results_backward_compat_without_read_state_per_dialog() -> None:
    """Missing kwarg → renders identically to pre-39.3 behaviour."""
    rows = [_dm_row(message_id=1, dialog_id=111, dialog_name="Alice")]
    baseline = _format_search_results(rows, "hello", global_mode=True)
    assert "[read-state:" not in baseline
    assert "[inbox:" not in baseline


# ---------------------------------------------------------------------------
# search_messages MCP tool (end-to-end)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_tool_renders_per_dialog_header_block() -> None:
    """Daemon response carries read_state_per_dialog → tool renders header block."""
    rows = [
        _dm_row(message_id=1, dialog_id=111, dialog_name="Alice", text="ping"),
        _dm_row(message_id=2, dialog_id=222, dialog_name="Bob", text="ping"),
    ]
    rs_map = {
        111: _split_rs_inbox_unread(),
        222: _collapsed_rs(),
    }
    response = {
        "ok": True,
        "data": {
            "messages": rows,
            "read_state_per_dialog": rs_map,
        },
    }
    conn = _make_conn("search_messages", response)
    with _patch_daemon("mcp_telegram.tools.reading.daemon_connection", conn):
        result = await search_messages(SearchMessages(query="ping"))
    text = result[0].text  # type: ignore[index]
    assert "[inbox: 2 unread from peer" in text
    assert "[read-state: all caught up]" in text


@pytest.mark.asyncio
async def test_search_tool_backward_compat_no_read_state_per_dialog() -> None:
    """Daemon response without read_state_per_dialog → no header block."""
    rows = [_dm_row(message_id=1, dialog_id=111, dialog_name="Alice", text="ping")]
    response = {"ok": True, "data": {"messages": rows}}
    conn = _make_conn("search_messages", response)
    with _patch_daemon("mcp_telegram.tools.reading.daemon_connection", conn):
        result = await search_messages(SearchMessages(query="ping"))
    text = result[0].text  # type: ignore[index]
    assert "[read-state:" not in text
    assert "[inbox:" not in text


# ---------------------------------------------------------------------------
# list_unread_messages MCP tool (end-to-end)
# ---------------------------------------------------------------------------


def _unread_group(
    *,
    dialog_id: int,
    display_name: str,
    read_state: dict | None,
    dialog_type: str | None,
    category: str = "human",
    msg_text: str = "hey",
    msg_id: int = 1,
) -> dict:
    g: dict = {
        "dialog_id": dialog_id,
        "display_name": display_name,
        "unread_count": 1,
        "unread_mentions_count": 0,
        "category": category,
        "messages": [
            {
                "message_id": msg_id,
                "out": 0,
                "sent_at": 1_776_000_000,
                "text": msg_text,
                "sender_id": dialog_id,
                "sender_first_name": display_name,
                "is_service": 0,
                "dialog_id": dialog_id,
                "effective_sender_id": dialog_id,
            }
        ],
    }
    if read_state is not None:
        g["read_state"] = read_state
    if dialog_type is not None:
        g["dialog_type"] = dialog_type
    return g


@pytest.mark.asyncio
async def test_unread_tool_renders_per_chat_header() -> None:
    """Each DM chat block in rendered output has exactly one header preceding its messages."""
    groups = [
        _unread_group(
            dialog_id=111,
            display_name="Alice",
            read_state=_split_rs_inbox_unread(),
            dialog_type="User",
            msg_text="hey A",
        ),
        _unread_group(
            dialog_id=222,
            display_name="Bob",
            read_state=_collapsed_rs(),
            dialog_type="User",
            msg_text="hey B",
            msg_id=2,
        ),
    ]
    response = {"ok": True, "data": {"groups": groups, "bootstrap_pending": 0}}
    conn = _make_conn("list_unread_messages", response)
    with _patch_daemon("mcp_telegram.tools.unread.daemon_connection", conn):
        result = await list_unread_messages(ListUnreadMessages())
    text = result[0].text  # type: ignore[index]
    assert "[inbox: 2 unread from peer" in text
    assert "[read-state: all caught up]" in text
    # Order: Alice block first → header precedes "hey A"; Bob block → header precedes "hey B".
    assert text.index("[inbox: 2 unread from peer") < text.index("hey A")
    assert text.index("[read-state: all caught up]") < text.index("hey B")


@pytest.mark.asyncio
async def test_unread_tool_renders_collapsed_header_when_chat_caught_up() -> None:
    """DM group with collapsed read_state → '[read-state: all caught up]'."""
    groups = [
        _unread_group(
            dialog_id=111,
            display_name="Alice",
            read_state=_collapsed_rs(),
            dialog_type="User",
        ),
    ]
    response = {"ok": True, "data": {"groups": groups, "bootstrap_pending": 0}}
    conn = _make_conn("list_unread_messages", response)
    with _patch_daemon("mcp_telegram.tools.unread.daemon_connection", conn):
        result = await list_unread_messages(ListUnreadMessages())
    text = result[0].text  # type: ignore[index]
    assert "[read-state: all caught up]" in text


@pytest.mark.asyncio
async def test_unread_tool_renders_split_header_when_inbox_unread() -> None:
    """Split form for DM with inbox unread."""
    groups = [
        _unread_group(
            dialog_id=111,
            display_name="Alice",
            read_state=_split_rs_inbox_unread(),
            dialog_type="User",
        ),
    ]
    response = {"ok": True, "data": {"groups": groups, "bootstrap_pending": 0}}
    conn = _make_conn("list_unread_messages", response)
    with _patch_daemon("mcp_telegram.tools.unread.daemon_connection", conn):
        result = await list_unread_messages(ListUnreadMessages())
    text = result[0].text  # type: ignore[index]
    assert "[inbox: 2 unread from peer" in text
    assert "[outbox: all read by peer]" in text


@pytest.mark.asyncio
async def test_unread_tool_no_header_for_non_dm_group() -> None:
    """Non-DM group (dialog_type != 'User') → no header for that block."""
    groups = [
        _unread_group(
            dialog_id=-100500,
            display_name="Some Channel",
            read_state=None,
            dialog_type="Channel",
            category="channel",
        ),
    ]
    response = {"ok": True, "data": {"groups": groups, "bootstrap_pending": 0}}
    conn = _make_conn("list_unread_messages", response)
    with _patch_daemon("mcp_telegram.tools.unread.daemon_connection", conn):
        result = await list_unread_messages(ListUnreadMessages())
    text = result[0].text  # type: ignore[index]
    assert "[read-state:" not in text
    assert "[inbox:" not in text
    assert "[outbox:" not in text


@pytest.mark.asyncio
async def test_unread_tool_backward_compat_no_read_state() -> None:
    """Groups without read_state/dialog_type → no header lines (legacy daemon)."""
    groups = [
        _unread_group(
            dialog_id=111,
            display_name="Alice",
            read_state=None,
            dialog_type=None,
        ),
    ]
    response = {"ok": True, "data": {"groups": groups, "bootstrap_pending": 0}}
    conn = _make_conn("list_unread_messages", response)
    with _patch_daemon("mcp_telegram.tools.unread.daemon_connection", conn):
        result = await list_unread_messages(ListUnreadMessages())
    text = result[0].text  # type: ignore[index]
    assert "[read-state:" not in text
    assert "[inbox:" not in text
    assert "[outbox:" not in text
