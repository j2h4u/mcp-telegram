"""Tests for tools/reading.py helpers."""
from __future__ import annotations

from mcp_telegram.tools.reading import _format_search_results


def _row(
    sender_id,
    sender_first_name,
    *,
    message_id: int = 1,
    sent_at: int = 1_700_000_000,
    text: str = "hello world",
    dialog_name: str | None = None,
) -> dict:
    r: dict = {
        "message_id": message_id,
        "sent_at": sent_at,
        "text": text,
        "sender_id": sender_id,
        "sender_first_name": sender_first_name,
    }
    if dialog_name is not None:
        r["dialog_name"] = dialog_name
    return r


def test_search_snippet_uses_sender_first_name_when_present():
    out = _format_search_results([_row(sender_id=42, sender_first_name="Alice")], "hello")
    assert " Alice (msg_id:1)" in out


def test_search_snippet_renders_unknown_user_with_id_when_name_missing():
    out = _format_search_results([_row(sender_id=42, sender_first_name=None)], "hello")
    assert " (unknown user 42) (msg_id:1)" in out


def test_search_snippet_renders_system_when_sender_id_is_none():
    out = _format_search_results([_row(sender_id=None, sender_first_name=None)], "hello")
    assert " System (msg_id:1)" in out


def test_search_snippet_no_raw_question_mark_sender_fallback_in_source():
    import pathlib
    src = pathlib.Path("src/mcp_telegram/tools/reading.py").read_text()
    assert 'row.get("sender_first_name") or "?"' not in src
