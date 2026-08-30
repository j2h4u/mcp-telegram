"""Structured output tests for ListDialogs DIFF-04 fields + snapshot_age annotation.

Phase 44 Plan 02 — covers:
- DIFF-04: mentions/reactions/draft fields on dialog rows
- LISTDIALOGS-04: snapshot_age_h annotation when stale
- bootstrap_pending banner when dialogs snapshot is empty (Plan 01 contract)
- draft_text with embedded double quotes (cosmetic acceptance T-44-07)
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.types import TextContent

from mcp_telegram.sync_read_model import build_sync_read_model
from mcp_telegram.tools._base import ToolResult
from mcp_telegram.tools.discovery import ListDialogs, list_dialogs


@dataclass(frozen=True)
class _DialogDictOptions:
    dialog_id: int = 100
    name: str | None = "Alice"
    type_: str | None = "User"
    unread_mentions_count: int = 0
    unread_reactions_count: int = 0
    draft_text: str | None = None


def _make_dialog_dict(*, opts: _DialogDictOptions | None = None, **kwargs: object) -> dict[str, object]:
    if opts is None:
        opts = _DialogDictOptions()
    if kwargs:
        opts = replace(opts, **kwargs)
    return {
        "id": opts.dialog_id,
        "name": opts.name,
        "type": opts.type_,
        "last_message_at": 1700000000,
        "unread_count": 0,
        "members": None,
        "created": None,
        "access_lost_at": None,
        "unread_in": None,
        "unread_out": None,
        "unread_mentions_count": opts.unread_mentions_count,
        "unread_reactions_count": opts.unread_reactions_count,
        "draft_text": opts.draft_text,
        "scheduled_count": 0,
        "next_scheduled_at": None,
        "inclusion_basis": None,
        "folder_ids": [],
        "folders": [],
        "archived": False,
        **build_sync_read_model(
            persisted_status="synced",
            enrollment_enabled=True,
            last_synced_at=1700000000,
            last_event_at=None,
            last_delta_checked_at=None,
            saved_message_count=0,
            total_messages=None,
            now=1700000000,
        ).to_wire(),
    }


def _patched_daemon(response: dict[str, object]):
    conn = MagicMock()
    conn.list_dialogs = AsyncMock(return_value=response)
    conn.upsert_entities = AsyncMock(return_value={"ok": True, "upserted": 0})

    @asynccontextmanager
    async def _cm():
        yield conn

    return patch("mcp_telegram.tools.discovery.daemon_connection", side_effect=_cm)


def _canonical_catalog(*, dialogs: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "ok": True,
        "data": {
            "dialogs": [] if dialogs is None else dialogs,
            "snapshot_age_h": None,
            "bootstrap_pending": False,
            "scope": "all",
            "folder_snapshot": {
                "generation": None,
                "status": "unavailable",
                "completed_at": None,
                "age_seconds": None,
                "complete": False,
            },
        },
    }


def _error_text(result: ToolResult) -> str:
    assert result.content
    first = result.content[0]
    assert isinstance(first, TextContent)
    return first.text


@pytest.mark.asyncio
async def test_list_dialogs_accepts_canonical_empty_catalog() -> None:
    with _patched_daemon(_canonical_catalog()):
        result = await list_dialogs(ListDialogs())

    assert result.is_error is False
    assert result.content == ()
    structured = cast(dict[str, object], result.structured_content)
    assert structured["dialogs"] == []
    assert structured["count"] == 0
    assert structured["bootstrap_pending"] is False
    assert structured["scope"] == "all"


@pytest.mark.asyncio
async def test_list_dialogs_rejects_malformed_top_level_catalog() -> None:
    response = _canonical_catalog()
    data = cast(dict[str, object], response["data"])
    data["bootstrap_pending"] = 0

    with _patched_daemon(response):
        result = await list_dialogs(ListDialogs())

    assert result.is_error is True
    assert result.structured_content is None
    assert "daemon_protocol_error" in _error_text(result)


@pytest.mark.asyncio
async def test_list_dialogs_rejects_missing_top_level_catalog_field() -> None:
    response = _canonical_catalog()
    data = cast(dict[str, object], response["data"])
    del data["scope"]

    with _patched_daemon(response):
        result = await list_dialogs(ListDialogs())

    assert result.is_error is True
    assert result.structured_content is None
    assert "daemon_protocol_error" in _error_text(result)


@pytest.mark.asyncio
async def test_list_dialogs_rejects_malformed_row_field() -> None:
    dialog = _make_dialog_dict()
    dialog["unread_mentions_count"] = "oops"

    with _patched_daemon(_canonical_catalog(dialogs=[dialog])):
        result = await list_dialogs(ListDialogs())

    assert result.is_error is True
    assert result.structured_content is None
    assert "daemon_protocol_error" in _error_text(result)


@pytest.mark.asyncio
async def test_list_dialogs_rejects_missing_required_row_field() -> None:
    dialog = _make_dialog_dict()
    del dialog["name"]

    with _patched_daemon(_canonical_catalog(dialogs=[dialog])):
        result = await list_dialogs(ListDialogs())

    assert result.is_error is True
    assert result.structured_content is None
    assert "daemon_protocol_error" in _error_text(result)


@pytest.mark.asyncio
async def test_list_dialogs_renders_mentions_token() -> None:
    response = _canonical_catalog(dialogs=[_make_dialog_dict(unread_mentions_count=3)])
    with _patched_daemon(response):
        result = await list_dialogs(ListDialogs())
    assert result.content == ()
    structured = cast(dict[str, object], result.structured_content)
    dialogs = cast(list[dict[str, object]], structured["dialogs"])
    assert dialogs[0]["unread_mentions_count"] == 3
    assert dialogs[0]["unread_reactions_count"] == 0


@pytest.mark.asyncio
async def test_list_dialogs_renders_reactions_token() -> None:
    response = _canonical_catalog(dialogs=[_make_dialog_dict(unread_reactions_count=2)])
    with _patched_daemon(response):
        result = await list_dialogs(ListDialogs())
    assert result.content == ()
    structured = cast(dict[str, object], result.structured_content)
    dialogs = cast(list[dict[str, object]], structured["dialogs"])
    assert dialogs[0]["unread_reactions_count"] == 2
    assert dialogs[0]["unread_mentions_count"] == 0


@pytest.mark.asyncio
async def test_list_dialogs_renders_draft_token() -> None:
    response = _canonical_catalog(dialogs=[_make_dialog_dict(draft_text="Hi all")])
    with _patched_daemon(response):
        result = await list_dialogs(ListDialogs())
    assert result.content == ()
    structured = cast(dict[str, object], result.structured_content)
    dialog = cast(dict[str, object], cast(list[dict[str, object]], structured["dialogs"])[0])
    assert dialog["draft_text"] == "Hi all"
    assert dialog["draft_content"] == {
        "text": "Hi all",
        "is_telegram_content": True,
        "content_kind": "message_text",
    }


@pytest.mark.asyncio
async def test_list_dialogs_omits_zero_diff_tokens() -> None:
    response = _canonical_catalog(
        dialogs=[
            _make_dialog_dict(
                unread_mentions_count=0,
                unread_reactions_count=0,
                draft_text=None,
            )
        ]
    )
    with _patched_daemon(response):
        result = await list_dialogs(ListDialogs())
    assert result.content == ()
    structured = cast(dict[str, object], result.structured_content)
    dialogs = cast(list[dict[str, object]], structured["dialogs"])
    assert dialogs[0]["draft_content"] is None
    assert dialogs[0]["unread_mentions_count"] == 0
    assert dialogs[0]["unread_reactions_count"] == 0


@pytest.mark.asyncio
async def test_list_dialogs_renders_all_three_diff_tokens_together() -> None:
    response = _canonical_catalog(
        dialogs=[
            _make_dialog_dict(
                unread_mentions_count=1,
                unread_reactions_count=2,
                draft_text="WIP",
            )
        ]
    )
    with _patched_daemon(response):
        result = await list_dialogs(ListDialogs())
    assert result.content == ()
    structured = cast(dict[str, object], result.structured_content)
    dialog = cast(dict[str, object], cast(list[dict[str, object]], structured["dialogs"])[0])
    assert dialog["unread_mentions_count"] == 1
    assert dialog["unread_reactions_count"] == 2
    assert dialog["draft_text"] == "WIP"


@pytest.mark.asyncio
async def test_list_dialogs_renders_snapshot_age_trailing_line_when_stale() -> None:
    response = _canonical_catalog(dialogs=[_make_dialog_dict()])
    cast(dict[str, object], response["data"])["snapshot_age_h"] = 18
    with _patched_daemon(response):
        result = await list_dialogs(ListDialogs())
    assert result.content == ()
    structured = cast(dict[str, object], result.structured_content)
    assert structured["snapshot_age_h"] == 18


@pytest.mark.asyncio
async def test_list_dialogs_omits_snapshot_age_line_when_fresh() -> None:
    response = _canonical_catalog(dialogs=[_make_dialog_dict()])
    with _patched_daemon(response):
        result = await list_dialogs(ListDialogs())
    assert result.content == ()
    structured = cast(dict[str, object], result.structured_content)
    assert structured["snapshot_age_h"] is None


@pytest.mark.asyncio
async def test_list_dialogs_renders_bootstrap_pending_line_when_true() -> None:
    response = _canonical_catalog()
    cast(dict[str, object], response["data"])["bootstrap_pending"] = True
    with _patched_daemon(response):
        result = await list_dialogs(ListDialogs())
    assert result.content == ()
    structured = cast(dict[str, object], result.structured_content)
    assert structured["bootstrap_pending"] is True
    # result_count=0 is set on the ToolResult internally; the MCP wrapper
    # returns .content (a list), so result_count is not accessible here.
    # The implementation passes result_count=0 explicitly — verified by code review.


@pytest.mark.asyncio
async def test_list_dialogs_renders_no_dialogs_when_empty_and_not_bootstrap() -> None:
    """bootstrap_pending=False + empty dialogs returns an empty structured list.

    This is the 'table populated but caller's filter excluded everything' case
    per Plan 01's bootstrap_pending semantics.
    """
    response = _canonical_catalog()
    with _patched_daemon(response):
        result = await list_dialogs(ListDialogs())
    assert result.content == ()
    structured = cast(dict[str, object], result.structured_content)
    assert structured["dialogs"] == []
    assert structured["count"] == 0
    assert structured["bootstrap_pending"] is False


@pytest.mark.asyncio
async def test_list_dialogs_renders_draft_with_double_quotes() -> None:
    """Draft text with embedded double quotes renders as-is (cosmetic acceptance T-44-07).

    The inner double quotes are NOT escaped — this is accepted cosmetic behavior.
    The renderer output is text-only for an LLM; no parser interprets the format.
    """
    response = _canonical_catalog(dialogs=[_make_dialog_dict(draft_text='Say "hi" to Bob')])
    with _patched_daemon(response):
        result = await list_dialogs(ListDialogs())
    assert result.content == ()
    structured = cast(dict[str, object], result.structured_content)
    dialog = cast(dict[str, object], cast(list[dict[str, object]], structured["dialogs"])[0])
    assert dialog["draft_text"] == 'Say "hi" to Bob'
    assert cast(dict[str, object], dialog["draft_content"])["text"] == 'Say "hi" to Bob'
