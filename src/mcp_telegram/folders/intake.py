"""Process-local admission fence for supplied folder updates."""

from __future__ import annotations

_generation = 0
_FOLDER_UPDATES = frozenset({"UpdateDialogFilter", "UpdateDialogFilters", "UpdateDialogFilterOrder"})


def marker() -> int:
    return _generation


def capture_folder_intake(updates: list[object]) -> None:
    if has_folder_updates(updates):
        invalidate_folder_intake()


def has_folder_updates(updates: list[object]) -> bool:
    return any(type(update).__name__ in _FOLDER_UPDATES for update in updates)


def invalidate_folder_intake() -> None:
    global _generation
    _generation += 1
