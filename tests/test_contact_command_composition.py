"""Native contact intent must preserve observed fields and omit notes."""

import pytest
from telethon.tl import functions, types

from mcp_telegram.contacts.composition import ContactIntent, compose_contact
from mcp_telegram.contacts.contracts import ContactCommandState


def _state(member: bool, phone: str | None = None) -> ContactCommandState:
    return {
        "membership": member,
        "first_name": "Before" if member else None,
        "last_name": "Surname" if member else None,
        "phone_state": "available" if phone else "unknown",
        "phone": phone,
        "expected_version": "a" * 64,
        "observation_order": 1,
    }


@pytest.mark.parametrize("phone", [None, "123456"])
def test_update_preserves_omitted_name_and_sends_no_note(phone: str | None) -> None:
    peer = types.InputUser(42, 99)
    request = compose_contact(ContactIntent("update", "After", None), _state(True, phone), peer)
    assert isinstance(request, functions.contacts.AddContactRequest)
    assert (request.first_name, request.last_name, request.phone) == ("After", "Surname", phone or "")
    assert request.note is None and not request.add_phone_privacy_exception
    assert int.from_bytes(bytes(request)[4:8], "little") == 0


def test_create_and_singleton_delete() -> None:
    peer = types.InputUser(42, 99)
    create = compose_contact(ContactIntent("create", "Name", None), _state(False), peer)
    assert isinstance(create, functions.contacts.AddContactRequest)
    assert create.note is None and create.phone == "" and int.from_bytes(bytes(create)[4:8], "little") == 0
    delete = compose_contact(ContactIntent("delete", None, None), _state(True), peer)
    assert isinstance(delete, functions.contacts.DeleteContactsRequest) and delete.id == [peer]
