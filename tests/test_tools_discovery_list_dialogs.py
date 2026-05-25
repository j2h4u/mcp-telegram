"""Renderer tests for ListDialogs DIFF-04 tokens + snapshot_age annotation.

Phase 44 Plan 02 — covers:
- DIFF-04: inline mentions=/reactions=/draft= tokens on rows
- LISTDIALOGS-04: trailing [snapshot_age=Xh] line when stale
- bootstrap_pending banner when dialogs snapshot is empty (Plan 01 contract)
- bootstrap_pending=False + empty dialogs -> no_dialogs_text() fallthrough
- draft_text with embedded double quotes (cosmetic acceptance T-44-07)
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_telegram.errors import no_dialogs_text
from mcp_telegram.tools.discovery import ListDialogs, list_dialogs


def _make_dialog_dict(
    *,
    dialog_id: int = 100,
    name: str = "Alice",
    type_: str = "User",
    unread_mentions_count: int = 0,
    unread_reactions_count: int = 0,
    draft_text: str | None = None,
    sync_status: str = "synced",
) -> dict:
    return {
        "id": dialog_id,
        "name": name,
        "type": type_,
        "last_message_at": 1700000000,
        "unread_count": 0,
        "members": None,
        "created": None,
        "sync_status": sync_status,
        "sync_coverage_pct": None,
        "access_lost_at": None,
        "unread_mentions_count": unread_mentions_count,
        "unread_reactions_count": unread_reactions_count,
        "draft_text": draft_text,
    }


def _patched_daemon(response: dict):
    conn = MagicMock()
    conn.list_dialogs = AsyncMock(return_value=response)
    conn.upsert_entities = AsyncMock(return_value={"ok": True, "upserted": 0})

    @asynccontextmanager
    async def _cm():
        yield conn

    return patch("mcp_telegram.tools.discovery.daemon_connection", side_effect=_cm)


@pytest.mark.asyncio
async def test_list_dialogs_renders_mentions_token() -> None:
    response = {
        "ok": True,
        "data": {
            "dialogs": [_make_dialog_dict(unread_mentions_count=3)],
            "snapshot_age_h": None,
            "bootstrap_pending": False,
        },
    }
    with _patched_daemon(response):
        result = await list_dialogs(ListDialogs())
    text = result.content[0].text
    assert " mentions=3" in text
    assert " reactions=" not in text
    assert result.structured_content is not None
    assert result.structured_content["dialogs"][0]["unread_mentions_count"] == 3


@pytest.mark.asyncio
async def test_list_dialogs_renders_reactions_token() -> None:
    response = {
        "ok": True,
        "data": {
            "dialogs": [_make_dialog_dict(unread_reactions_count=2)],
            "snapshot_age_h": None,
            "bootstrap_pending": False,
        },
    }
    with _patched_daemon(response):
        result = await list_dialogs(ListDialogs())
    text = result.content[0].text
    assert " reactions=2" in text
    assert " mentions=" not in text
    assert result.structured_content is not None
    assert result.structured_content["dialogs"][0]["unread_reactions_count"] == 2


@pytest.mark.asyncio
async def test_list_dialogs_renders_draft_token() -> None:
    response = {
        "ok": True,
        "data": {
            "dialogs": [_make_dialog_dict(draft_text="Hi all")],
            "snapshot_age_h": None,
            "bootstrap_pending": False,
        },
    }
    with _patched_daemon(response):
        result = await list_dialogs(ListDialogs())
    text = result.content[0].text
    assert 'draft="Hi all"' in text
    assert result.structured_content is not None
    dialog = result.structured_content["dialogs"][0]
    assert dialog["draft_text"] == "Hi all"
    assert dialog["draft_content"] == {
        "text": "Hi all",
        "is_telegram_content": True,
        "content_kind": "message_text",
    }


@pytest.mark.asyncio
async def test_list_dialogs_omits_zero_diff_tokens() -> None:
    response = {
        "ok": True,
        "data": {
            "dialogs": [
                _make_dialog_dict(
                    unread_mentions_count=0,
                    unread_reactions_count=0,
                    draft_text=None,
                )
            ],
            "snapshot_age_h": None,
            "bootstrap_pending": False,
        },
    }
    with _patched_daemon(response):
        result = await list_dialogs(ListDialogs())
    text = result.content[0].text
    assert "id=" in text  # row is present
    assert "mentions=" not in text
    assert "reactions=" not in text
    assert "draft=" not in text
    assert result.structured_content is not None
    assert result.structured_content["dialogs"][0]["draft_content"] is None


@pytest.mark.asyncio
async def test_list_dialogs_renders_all_three_diff_tokens_together() -> None:
    response = {
        "ok": True,
        "data": {
            "dialogs": [
                _make_dialog_dict(
                    unread_mentions_count=1,
                    unread_reactions_count=2,
                    draft_text="WIP",
                )
            ],
            "snapshot_age_h": None,
            "bootstrap_pending": False,
        },
    }
    with _patched_daemon(response):
        result = await list_dialogs(ListDialogs())
    text = result.content[0].text
    lines = text.splitlines()
    # Single row only
    assert len(lines) == 1
    row = lines[0]
    assert "id=" in row
    assert " mentions=1" in row
    assert " reactions=2" in row
    assert 'draft="WIP"' in row


@pytest.mark.asyncio
async def test_list_dialogs_renders_snapshot_age_trailing_line_when_stale() -> None:
    response = {
        "ok": True,
        "data": {
            "dialogs": [_make_dialog_dict()],
            "snapshot_age_h": 18,
            "bootstrap_pending": False,
        },
    }
    with _patched_daemon(response):
        result = await list_dialogs(ListDialogs())
    text = result.content[0].text
    assert "[snapshot_age=18h" in text
    # Must appear as the last line
    last_line = text.splitlines()[-1]
    assert last_line.startswith("[snapshot_age=")


@pytest.mark.asyncio
async def test_list_dialogs_omits_snapshot_age_line_when_fresh() -> None:
    response = {
        "ok": True,
        "data": {
            "dialogs": [_make_dialog_dict()],
            "snapshot_age_h": None,
            "bootstrap_pending": False,
        },
    }
    with _patched_daemon(response):
        result = await list_dialogs(ListDialogs())
    text = result.content[0].text
    assert "snapshot_age=" not in text


@pytest.mark.asyncio
async def test_list_dialogs_renders_bootstrap_pending_line_when_true() -> None:
    response = {
        "ok": True,
        "data": {
            "dialogs": [],
            "snapshot_age_h": None,
            "bootstrap_pending": True,
        },
    }
    with _patched_daemon(response):
        result = await list_dialogs(ListDialogs())
    text = result.content[0].text
    assert "sync in progress" in text
    assert result.structured_content is not None
    assert result.structured_content["bootstrap_pending"] is True
    # result_count=0 is set on the ToolResult internally; the MCP wrapper
    # returns .content (a list), so result_count is not accessible here.
    # The implementation passes result_count=0 explicitly — verified by code review.


@pytest.mark.asyncio
async def test_list_dialogs_renders_no_dialogs_when_empty_and_not_bootstrap() -> None:
    """bootstrap_pending=False + empty dialogs -> existing no_dialogs_text() fallback.

    This is the 'table populated but caller's filter excluded everything' case
    per Plan 01's bootstrap_pending semantics.
    """
    response = {
        "ok": True,
        "data": {
            "dialogs": [],
            "bootstrap_pending": False,
        },
    }
    with _patched_daemon(response):
        result = await list_dialogs(ListDialogs())
    text = result.content[0].text
    assert text == no_dialogs_text()


@pytest.mark.asyncio
async def test_list_dialogs_renders_draft_with_double_quotes() -> None:
    """Draft text with embedded double quotes renders as-is (cosmetic acceptance T-44-07).

    The inner double quotes are NOT escaped — this is accepted cosmetic behavior.
    The renderer output is text-only for an LLM; no parser interprets the format.
    """
    response = {
        "ok": True,
        "data": {
            "dialogs": [_make_dialog_dict(draft_text='Say "hi" to Bob')],
            "snapshot_age_h": None,
            "bootstrap_pending": False,
        },
    }
    with _patched_daemon(response):
        result = await list_dialogs(ListDialogs())
    text = result.content[0].text
    assert 'draft="Say "hi" to Bob"' in text
    # The embedded quotes must not introduce extra lines
    assert text.count("\n") == 0
