"""Contract and import-boundary tests for neutral message DTOs."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import FrozenInstanceError, fields
from types import ModuleType

import pytest

from mcp_telegram.message_contracts import (
    EntityRecord,
    ExtractedMessage,
    ForwardRecord,
    ReactionRecord,
    StoredMessage,
)


def _stored_message() -> StoredMessage:
    return StoredMessage(
        dialog_id=1,
        message_id=2,
        sent_at=3,
        text="message",
        sender_id=4,
        sender_first_name="Sender",
        media_description=None,
        reply_to_msg_id=None,
        forum_topic_id=None,
        edit_date=None,
        grouped_id=None,
        reply_to_peer_id=None,
        out=0,
        is_service=0,
        post_author=None,
    )


@pytest.mark.parametrize(
    ("contract", "field_names"),
    [
        (
            StoredMessage,
            (
                "dialog_id",
                "message_id",
                "sent_at",
                "text",
                "sender_id",
                "sender_first_name",
                "media_description",
                "reply_to_msg_id",
                "forum_topic_id",
                "edit_date",
                "grouped_id",
                "reply_to_peer_id",
                "out",
                "is_service",
                "post_author",
            ),
        ),
        (ReactionRecord, ("dialog_id", "message_id", "emoji", "count")),
        (EntityRecord, ("dialog_id", "message_id", "offset", "length", "type", "value")),
        (
            ForwardRecord,
            (
                "dialog_id",
                "message_id",
                "fwd_from_peer_id",
                "fwd_from_name",
                "fwd_date",
                "fwd_channel_post",
            ),
        ),
    ],
)
def test_row_contracts_preserve_field_order_and_dataclass_policy(
    contract: type[StoredMessage] | type[ReactionRecord] | type[EntityRecord] | type[ForwardRecord],
    field_names: tuple[str, ...],
) -> None:
    assert tuple(item.name for item in fields(contract)) == field_names
    assert all(item.kw_only for item in fields(contract))
    assert hasattr(contract, "__slots__")

    if contract is StoredMessage:
        field_name = "text"
        with pytest.raises(FrozenInstanceError):
            setattr(_stored_message(), field_name, "changed")


def test_extracted_message_is_mutable_with_fresh_list_factories() -> None:
    first = ExtractedMessage(message=_stored_message(), reply_count=0)
    second = ExtractedMessage(message=_stored_message(), reply_count=0)

    assert first.reactions == []
    assert first.entities == []
    assert first.reactions is not second.reactions
    assert first.entities is not second.entities

    first.reply_count = 1
    first.reactions.append(ReactionRecord(dialog_id=1, message_id=2, emoji="ok", count=1))
    assert first.reply_count == 1
    assert second.reactions == []


def test_neutral_modules_do_not_load_sync_worker_or_telethon() -> None:
    script = """
import sys
import mcp_telegram.message_contracts
import mcp_telegram.telegram_reading

assert "mcp_telegram.sync_worker" not in sys.modules
assert not any(name == "telethon" or name.startswith("telethon.") for name in sys.modules)
"""
    subprocess.run([sys.executable, "-c", script], check=True, capture_output=True, text=True)


@pytest.mark.parametrize(
    "contract_name",
    ["StoredMessage", "ReactionRecord", "EntityRecord", "ForwardRecord", "ExtractedMessage"],
)
def test_sync_worker_does_not_implicitly_reexport_message_contracts(contract_name: str) -> None:
    import mcp_telegram.sync_worker as sync_worker

    assert isinstance(sync_worker, ModuleType)
    assert not hasattr(sync_worker, contract_name)
