"""A durable receipt cannot prove ownership of another method or scope."""

from dataclasses import asdict, replace
from typing import cast

import pytest

from mcp_telegram.native_write_fence import NativeWriteDomain, NativeWriteReceiptBinding
from mcp_telegram.telegram_rpc import _valid_safety_intent


@pytest.mark.parametrize(
    "domain,method",
    [
        ("folder", "telethon.tl.functions.messages.UpdateDialogFilterRequest:1ad4a04a"),
        ("contact_note", "telethon.tl.functions.contacts.UpdateContactNoteRequest:139f63fb"),
    ],
)
def test_native_intent_binds_domain_method_and_auth_scope(domain: str, method: str) -> None:
    binding = NativeWriteReceiptBinding(
        cast(NativeWriteDomain, domain), 42, 2, 123, "operation", "update", 8, "fingerprint", 1
    )
    intent = {
        "method": method,
        "run": "run",
        "phase": "unknown",
        "scope": {"account_id": 42, "dc_id": 2, "key_id": 123},
        "native_write_receipt_owner": {"binding": asdict(binding), "contained": True},
    }
    assert _valid_safety_intent("attempt", intent)
    other_domain = "contact_note" if domain == "folder" else "folder"
    intent["native_write_receipt_owner"] = {
        "binding": asdict(replace(binding, domain=cast(NativeWriteDomain, other_domain))),
        "contained": True,
    }
    assert not _valid_safety_intent("attempt", intent)
    intent["native_write_receipt_owner"] = {"binding": asdict(replace(binding, key_id=124)), "contained": True}
    assert not _valid_safety_intent("attempt", intent)
