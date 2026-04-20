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
    is_service: int = 0,
    out: int = 0,
    dialog_id: int = 0,
    effective_sender_id=None,
) -> dict:
    r: dict = {
        "message_id": message_id,
        "sent_at": sent_at,
        "text": text,
        "sender_id": sender_id,
        "sender_first_name": sender_first_name,
        "is_service": is_service,
        "out": out,
        "dialog_id": dialog_id,
        "effective_sender_id": effective_sender_id,
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


def test_search_snippet_renders_system_when_is_service():
    """Phase 39.1-02: 'System' requires is_service=1 (not just sender_id=None)."""
    out = _format_search_results(
        [_row(sender_id=None, sender_first_name=None, is_service=1)], "hello"
    )
    assert " System (msg_id:1)" in out


def test_search_snippet_dm_outgoing_renders_ya():
    """DM outgoing (out=1, dialog_id>0, is_service=0) renders 'Я'."""
    out = _format_search_results(
        [_row(
            sender_id=None, sender_first_name=None,
            out=1, dialog_id=268071163, is_service=0,
            effective_sender_id=99999,
        )],
        "hello",
    )
    # The Russian letter 'Я' must appear as the sender token
    assert " \u042f (msg_id:1)" in out


def test_search_snippet_dm_incoming_uses_first_name():
    out = _format_search_results(
        [_row(
            sender_id=None, sender_first_name="Alice",
            out=0, dialog_id=268071163, is_service=0,
            effective_sender_id=268071163,
        )],
        "hello",
    )
    assert " Alice (msg_id:1)" in out


def test_search_snippet_dm_incoming_unknown_uses_effective_sender_id():
    out = _format_search_results(
        [_row(
            sender_id=None, sender_first_name=None,
            out=0, dialog_id=268071163, is_service=0,
            effective_sender_id=268071163,
        )],
        "hello",
    )
    assert " (unknown user 268071163) (msg_id:1)" in out


def test_search_snippet_group_unknown_renders_unknown_user():
    """Group unknown sender (no id anywhere) → '(unknown user)' no trailing id."""
    out = _format_search_results(
        [_row(
            sender_id=None, sender_first_name=None,
            out=0, dialog_id=-100123, is_service=0,
            effective_sender_id=None,
        )],
        "hello",
    )
    assert " (unknown user) (msg_id:1)" in out


def test_search_snippet_no_raw_question_mark_sender_fallback_in_source():
    import pathlib
    src = pathlib.Path("src/mcp_telegram/tools/reading.py").read_text()
    assert 'row.get("sender_first_name") or "?"' not in src
