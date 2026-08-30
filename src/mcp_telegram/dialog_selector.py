"""Canonical validation and normalization for dialog selectors."""

from __future__ import annotations

import re
from dataclasses import dataclass

SQLITE_INT64_MIN = -(2**63)
SQLITE_INT64_MAX = 2**63 - 1

_SIGNED_ASCII_DECIMAL_RE = re.compile(r"^[+-]?[0-9]+$")


@dataclass(frozen=True, slots=True)
class DialogSelector:
    """One normalized exact-id or natural-name dialog selector."""

    exact_id: int | None = None
    query: str | None = None

    def __post_init__(self) -> None:
        if (self.exact_id is None) == (self.query is None):
            raise ValueError("DialogSelector requires exactly one selector")

    @property
    def label(self) -> str:
        """Stable human-readable representation for errors and telemetry."""
        return str(self.exact_id) if self.exact_id is not None else self.query or ""


class DialogSelectorError(ValueError):
    """Stable validation failure returned at MCP and raw IPC boundaries."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _validated_exact_id(value: object) -> int:
    if type(value) is not int:
        raise DialogSelectorError(
            "invalid_dialog_selector",
            "Exact dialog id must be a nonzero signed SQLite int64 integer.",
        )
    if value == 0 or not SQLITE_INT64_MIN <= value <= SQLITE_INT64_MAX:
        raise DialogSelectorError(
            "invalid_dialog_selector",
            "Exact dialog id must be a nonzero signed SQLite int64 integer.",
        )
    return value


def _selector_from_natural_value(value: object) -> DialogSelector:
    if not isinstance(value, str):
        raise DialogSelectorError(
            "invalid_dialog_selector",
            "Dialog must be a nonempty string or an exact dialog id must be provided.",
        )
    query = value.strip()
    if not query:
        raise DialogSelectorError(
            "invalid_dialog_selector",
            "Dialog must be a nonempty string or an exact dialog id must be provided.",
        )
    if _SIGNED_ASCII_DECIMAL_RE.fullmatch(query):
        return DialogSelector(exact_id=_validated_exact_id(int(query)))
    return DialogSelector(query=query)


def optional_dialog_selector(
    *,
    exact_id: object | None = None,
    dialog: object | None = None,
) -> DialogSelector | None:
    """Return zero or one selector, rejecting conflicting or malformed values."""
    if exact_id is not None and dialog is not None:
        raise DialogSelectorError(
            "invalid_dialog_selector",
            "dialog and exact_dialog_id are mutually exclusive.",
        )
    if exact_id is not None:
        return DialogSelector(exact_id=_validated_exact_id(exact_id))
    if dialog is not None:
        return _selector_from_natural_value(dialog)
    return None


def required_dialog_selector(
    *,
    exact_id: object | None = None,
    dialog: object | None = None,
) -> DialogSelector:
    """Return exactly one selector, preserving the stable missing error code."""
    selector = optional_dialog_selector(exact_id=exact_id, dialog=dialog)
    if selector is None:
        raise DialogSelectorError("missing_dialog", "Provide either dialog or exact_dialog_id.")
    return selector
