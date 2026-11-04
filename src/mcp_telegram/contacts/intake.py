"""Synchronous fences for contact writes awaiting physical admission."""

import threading
from collections.abc import Iterator
from contextlib import contextmanager

_scope_generation = 0
_catalogue_generation = 0
_peers: dict[int, tuple[int, int]] = {}
_delivered: dict[int, int] = {}
_lock = threading.Lock()


@contextmanager
def track_contact(entity_id: int) -> Iterator[tuple[int, int]]:
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


def invalidate_contact(entity_id: int) -> None:
    """Invalidate a tracked peer after accepted canonical change or update intake."""
    with _lock:
        current = _peers.get(entity_id)
        if current is not None:
            _peers[entity_id] = (current[0], current[1] + 1)


def intake_marker(entity_id: int) -> tuple[int, int] | None:
    with _lock:
        delivered = _delivered.get(entity_id)
        return None if delivered is None else (_scope_generation, delivered)


def current_contact_marker(entity_id: int) -> tuple[int, int] | None:
    with _lock:
        current = _peers.get(entity_id)
        return None if current is None else (_scope_generation, current[1])


def capture_contact_intake(updates: list[object]) -> None:
    global _catalogue_generation, _scope_generation
    with _lock:
        if any(type(update).__name__ == "UpdateContactsReset" for update in updates):
            _scope_generation += 1
        if any(
            type(update).__name__ in {"UpdateUser", "UpdateUserName", "UpdateUserPhone", "UpdateContactsReset"}
            for update in updates
        ):
            _catalogue_generation += 1
        for update in updates:
            if type(update).__name__ not in {"UpdateUser", "UpdateUserName", "UpdateUserPhone"}:
                continue
            entity_id = getattr(update, "user_id", None)
            if type(entity_id) is int and entity_id in _peers:
                count, generation = _peers[entity_id]
                _peers[entity_id] = (count, generation + 1)
                _delivered[entity_id] += 1


def invalidate_contact_scope() -> None:
    global _scope_generation, _catalogue_generation
    with _lock:
        _scope_generation += 1
        _catalogue_generation += 1


def catalogue_marker() -> int:
    with _lock:
        return _catalogue_generation
