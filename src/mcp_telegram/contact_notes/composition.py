"""Validate a note-only native mutation before reserving a wire attempt."""

from dataclasses import dataclass
from typing import Literal

from telethon.tl import types


@dataclass(frozen=True, slots=True)
class NoteIntent:
    action: Literal["create", "update", "clear"]
    text: str | None
    drop_formatting: bool


def compose_note(intent: NoteIntent, state: str, retained: object, limit: int) -> types.TextWithEntities | None:
    """Return a plain replacement or no-op; never reconstruct retained entities."""
    _validate_action(intent.action, state)
    if intent.action == "clear":
        return None if state == "absent" else types.TextWithEntities("", [])
    text = _valid_text(intent.text, limit)
    if state == "retained" and not _text_changed(intent, retained, text):
        return None
    return types.TextWithEntities(text, [])


def _validate_action(action: str, state: str) -> None:
    if state not in {"absent", "retained"}:
        raise ValueError("note_state_unknown")
    if action == "create" and state != "absent":
        raise ValueError("note_already_exists")
    if action == "update" and state != "retained":
        raise ValueError("note_absent")


def _valid_text(text: str | None, limit: int) -> str:
    if not isinstance(text, str) or not text or len(text) > limit:
        raise ValueError("invalid_note_text")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError("invalid_note_text") from error
    return text


def _text_changed(intent: NoteIntent, retained: object, text: str) -> bool:
    if not isinstance(retained, dict) or not isinstance(retained.get("text"), str):
        raise ValueError("note_state_unknown")
    if retained["text"] == text:
        return False
    if retained.get("entities") and not intent.drop_formatting:
        raise ValueError("note_formatting_conflict")
    return True
