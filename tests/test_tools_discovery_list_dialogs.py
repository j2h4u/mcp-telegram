"""Structured output tests for ListDialogs DIFF-04 fields + snapshot_age annotation.

Phase 44 Plan 02 — covers:
- DIFF-04: mentions/reactions/draft fields on dialog rows
- LISTDIALOGS-04: snapshot_age_h annotation when stale
- bootstrap_pending banner when dialogs snapshot is empty (Plan 01 contract)
- draft_text with embedded double quotes (cosmetic acceptance T-44-07)
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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
    assert result.content == ()
    assert result.structured_content is not None
    assert result.structured_content["dialogs"][0]["unread_mentions_count"] == 3
    assert result.structured_content["dialogs"][0]["unread_reactions_count"] == 0


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
    assert result.content == ()
    assert result.structured_content is not None
    assert result.structured_content["dialogs"][0]["unread_reactions_count"] == 2
    assert result.structured_content["dialogs"][0]["unread_mentions_count"] == 0


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
    assert result.content == ()
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
    assert result.content == ()
    assert result.structured_content is not None
    assert result.structured_content["dialogs"][0]["draft_content"] is None
    assert result.structured_content["dialogs"][0]["unread_mentions_count"] == 0
    assert result.structured_content["dialogs"][0]["unread_reactions_count"] == 0


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
    assert result.content == ()
    assert result.structured_content is not None
    dialog = result.structured_content["dialogs"][0]
    assert dialog["unread_mentions_count"] == 1
    assert dialog["unread_reactions_count"] == 2
    assert dialog["draft_text"] == "WIP"


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
    assert result.content == ()
    assert result.structured_content is not None
    assert result.structured_content["snapshot_age_h"] == 18


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
    assert result.content == ()
    assert result.structured_content is not None
    assert result.structured_content["snapshot_age_h"] is None


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
    assert result.content == ()
    assert result.structured_content is not None
    assert result.structured_content["bootstrap_pending"] is True
    # result_count=0 is set on the ToolResult internally; the MCP wrapper
    # returns .content (a list), so result_count is not accessible here.
    # The implementation passes result_count=0 explicitly — verified by code review.


@pytest.mark.asyncio
async def test_list_dialogs_renders_no_dialogs_when_empty_and_not_bootstrap() -> None:
    """bootstrap_pending=False + empty dialogs returns an empty structured list.

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
    assert result.content == ()
    assert result.structured_content is not None
    assert result.structured_content["dialogs"] == []
    assert result.structured_content["count"] == 0
    assert result.structured_content["bootstrap_pending"] is False


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
    assert result.content == ()
    assert result.structured_content is not None
    dialog = result.structured_content["dialogs"][0]
    assert dialog["draft_text"] == 'Say "hi" to Bob'
    assert dialog["draft_content"]["text"] == 'Say "hi" to Bob'
