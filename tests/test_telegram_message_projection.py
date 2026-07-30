"""Tests for telegram_message_projection sender-resolution logic.

Covers _resolve_effective_sender_id — the pure, zero-I/O function that
determines who "sent" a message when raw sender_id is absent.  Four
branches are exercised plus edge cases for negative dialog_id and
missing self_id.
"""

from __future__ import annotations

from mcp_telegram.telegram_message_projection import _resolve_effective_sender_id


class TestResolveEffectiveSenderId:
    def test_raw_sender_id_present_returns_directly(self) -> None:
        assert _resolve_effective_sender_id(raw_sender_id=42, dialog_id=None, self_id=None, is_service_flag=0, out_flag=0) == 42

    def test_raw_sender_id_wins_over_dialog_and_self(self) -> None:
        assert _resolve_effective_sender_id(raw_sender_id=42, dialog_id=100, self_id=200, is_service_flag=0, out_flag=1) == 42

    def test_service_message_returns_none(self) -> None:
        assert _resolve_effective_sender_id(raw_sender_id=None, dialog_id=100, self_id=200, is_service_flag=1, out_flag=1) is None

    def test_outgoing_dm_returns_self_id(self) -> None:
        assert _resolve_effective_sender_id(raw_sender_id=None, dialog_id=100, self_id=200, is_service_flag=0, out_flag=1) == 200

    def test_incoming_dm_returns_dialog_id(self) -> None:
        assert _resolve_effective_sender_id(raw_sender_id=None, dialog_id=100, self_id=200, is_service_flag=0, out_flag=0) == 100

    def test_negative_dialog_id_with_out_flag_returns_self_id(self) -> None:
        assert _resolve_effective_sender_id(raw_sender_id=None, dialog_id=-100, self_id=200, is_service_flag=0, out_flag=1) is None

    def test_negative_dialog_id_with_incoming_flag_returns_none(self) -> None:
        assert _resolve_effective_sender_id(raw_sender_id=None, dialog_id=-100, self_id=200, is_service_flag=0, out_flag=0) is None

    def test_none_dialog_id_returns_none(self) -> None:
        assert _resolve_effective_sender_id(raw_sender_id=None, dialog_id=None, self_id=200, is_service_flag=0, out_flag=1) is None

    def test_none_self_id_with_outgoing_returns_none(self) -> None:
        assert _resolve_effective_sender_id(raw_sender_id=None, dialog_id=100, self_id=None, is_service_flag=0, out_flag=1) is None
