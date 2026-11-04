"""Synchronous fences for contact note writes awaiting physical admission."""

import threading
from collections.abc import Iterator
from contextlib import contextmanager

_scope_generation = 0
_peers: dict[int, tuple[int, int]] = {}
_delivered: dict[int, int] = {}
_lock = threading.Lock()


@contextmanager
def track_contact_note(entity_id: int) -> Iterator[tuple[int, int]]:
    with _lock:
        count, generation = _peers.get(entity_id, (0, 0))
        _peers[entity_id] = (count + 1, generation)
        if count == 0:
            _delivered[entity_id] = 0
        marker = (_scope_generation, generation)
    try:
        yield marker
    finally:
        with _lock:
            count, generation = _peers[entity_id]
            if count == 1:
                del _peers[entity_id]
                del _delivered[entity_id]
            else:
                _peers[entity_id] = (count - 1, generation)


def is_current(entity_id: int, marker: tuple[int, int]) -> bool:
    with _lock:
        current = _peers.get(entity_id)
        return current is not None and marker == (_scope_generation, current[1])


def invalidate_contact_note(entity_id: int) -> None:
    """Invalidate a tracked peer after accepted canonical change or update intake."""
    with _lock:
        current = _peers.get(entity_id)
        if current is not None:
            _peers[entity_id] = (current[0], current[1] + 1)


def intake_marker(entity_id: int) -> tuple[int, int] | None:
    with _lock:
        delivered = _delivered.get(entity_id)
        return None if delivered is None else (_scope_generation, delivered)


def current_note_marker(entity_id: int) -> tuple[int, int] | None:
    with _lock:
        current = _peers.get(entity_id)
        return None if current is None else (_scope_generation, current[1])


def capture_contact_note_intake(updates: list[object]) -> None:
    global _scope_generation
    with _lock:
        for update in updates:
            kind = type(update).__name__
            if kind == "UpdateContactsReset":
                _scope_generation += 1
                continue
            if kind not in {"UpdateUser", "UpdateUserName", "UpdateUserPhone"}:
                continue
            entity_id = getattr(update, "user_id", None)
            if type(entity_id) is int and entity_id in _peers:
                count, generation = _peers[entity_id]
                _peers[entity_id] = (count, generation + 1)
                _delivered[entity_id] += 1


def invalidate_contact_note_scope() -> None:
    global _scope_generation
    with _lock:
        _scope_generation += 1


def note_semantic_fingerprint(note: object) -> str:
    """Fingerprint the plain native replacement without capabilities."""
    from telethon.tl import types

    if not isinstance(note, types.TextWithEntities) or note.entities:
        raise ValueError("contact note mutation must be plain text")
    return plain_note_fingerprint(note.text)


def plain_note_fingerprint(text: str) -> str:
    """Fingerprint the same capability-free semantic representation sent to Telegram."""
    import hashlib
    import json

    encoded = json.dumps([text, []], ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def validate_note_wire_request(binding: object, request: object) -> bool:
    """Bind the admitted native request to its intended peer and note body."""
    from telethon.tl import types
    from telethon.tl.functions.contacts import UpdateContactNoteRequest

    from mcp_telegram.native_write_fence import NativeWriteReceiptBinding

    if not isinstance(binding, NativeWriteReceiptBinding) or binding.domain != "contact_note":
        return False
    if not isinstance(request, UpdateContactNoteRequest) or not isinstance(request.id, types.InputUser):
        return False
    if request.id.user_id != binding.target or not isinstance(request.note, types.TextWithEntities):
        return False
    return _note_body_matches(binding.action, binding.semantic_fingerprint, request.note)


def _note_body_matches(action: str, fingerprint: str, note: object) -> bool:
    from telethon.tl import types

    if not isinstance(note, types.TextWithEntities) or note.entities or action not in {"create", "update", "clear"}:
        return False
    if (action == "clear" and (note.text or note.entities)) or (action != "clear" and not note.text):
        return False
    return note_semantic_fingerprint(note) == fingerprint
