"""Safe delivery projection for dialog-resolution failures."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .structured import telegram_content

_DIALOG_RESOLUTION_ERRORS = {"ambiguous_dialog", "dialog_not_found"}


@dataclass(frozen=True, slots=True)
class DialogResolutionErrorProjection:
    """One rendered dialog-resolution failure and its structured payload."""

    text: str
    structured_content: dict[str, object]


def _dialog_candidate_payload(match: Mapping[str, object]) -> dict[str, object]:
    """Project Telegram-controlled candidate text with explicit trust metadata."""
    candidate: dict[str, object] = {
        "entity_id": match.get("entity_id"),
        "score": match.get("score"),
        "entity_type": match.get("entity_type"),
        "untrusted_content": True,
        "trust": {
            "source": "telegram",
            "is_untrusted": True,
        },
    }
    if display_name := match.get("display_name"):
        candidate["display_name_content"] = telegram_content(str(display_name), "message_text")
    if username := match.get("username"):
        candidate["username_content"] = telegram_content(str(username), "message_text")
    if hint := match.get("disambiguation_hint"):
        candidate["disambiguation_hint_content"] = telegram_content(str(hint), "message_text")
    return candidate


def project_dialog_resolution_error(
    response: Mapping[str, object],
    *,
    fallback_action: str,
) -> DialogResolutionErrorProjection | None:
    """Return a safe projection for candidate-bearing dialog failures."""
    error = response.get("error")
    if not isinstance(error, str) or error not in _DIALOG_RESOLUTION_ERRORS:
        return None

    raw_candidates = response.get("candidates")
    raw_suggestion = response.get("suggestion")
    if not isinstance(raw_candidates, list) and not isinstance(raw_suggestion, Mapping):
        return None

    message = response.get("message")
    message_text = message if isinstance(message, str) else "Dialog resolution failed."
    required_action = response.get("required_action")
    action_text = required_action if isinstance(required_action, str) and required_action else fallback_action
    structured_content: dict[str, object] = {
        "error": error,
        "message": message_text,
        "required_action": action_text,
    }
    if isinstance(raw_candidates, list):
        structured_content["candidates"] = [
            _dialog_candidate_payload(candidate) for candidate in raw_candidates if isinstance(candidate, Mapping)
        ]
    if isinstance(raw_suggestion, Mapping):
        structured_content["suggestion"] = _dialog_candidate_payload(raw_suggestion)
    return DialogResolutionErrorProjection(
        text=f"Error: {error}: {message_text}\nAction: {action_text}",
        structured_content=structured_content,
    )


__all__ = ["DialogResolutionErrorProjection", "project_dialog_resolution_error"]
