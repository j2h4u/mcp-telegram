"""Compose one contact mutation from accepted canonical facts."""

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

from telethon.tl import functions, types
from telethon.tl.tlobject import TLRequest

from .contracts import ContactCommandState


@dataclass(frozen=True, slots=True)
class ContactIntent:
    action: Literal["create", "update", "delete"]
    first_name: str | None
    last_name: str | None


def compose_contact(intent: ContactIntent, state: ContactCommandState, peer: types.InputUser) -> TLRequest | None:
    if state["membership"] is None:
        raise ValueError("contact_state_unknown")
    if intent.action == "create":
        if state["membership"]:
            raise ValueError("contact_already_exists")
        if not intent.first_name:
            raise ValueError("contact_name_required")
        return functions.contacts.AddContactRequest(
            peer, intent.first_name, intent.last_name or "", "", add_phone_privacy_exception=False
        )
    if not state["membership"]:
        raise ValueError("contact_required")
    if intent.action == "delete":
        return functions.contacts.DeleteContactsRequest([peer])
    return _compose_update(intent, state, peer)


def _compose_update(intent: ContactIntent, state: ContactCommandState, peer: types.InputUser) -> TLRequest | None:
    first, last = state["first_name"], state["last_name"]
    if first is None or last is None:
        raise ValueError("contact_name_unavailable")
    first = first if intent.first_name is None else intent.first_name
    last = last if intent.last_name is None else intent.last_name
    if not first:
        raise ValueError("contact_name_required")
    if (first, last) == (state["first_name"], state["last_name"]):
        return None
    phone = _preserved_phone(state)
    return functions.contacts.AddContactRequest(peer, first, last, phone, add_phone_privacy_exception=False)


def _preserved_phone(state: ContactCommandState) -> str:
    return state["phone"] if state["phone_state"] == "available" and state["phone"] else ""


def contact_fingerprint(action: str, target: int, first: str, last: str, phone: str) -> str:
    """Bind capability-free request semantics without retaining request data."""
    data = json.dumps([action, target, first, last, phone], ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(data.encode()).hexdigest()
