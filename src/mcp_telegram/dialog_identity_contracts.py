"""Typed contracts for the canonical identity of a Telegram dialog."""

from __future__ import annotations

from dataclasses import dataclass

from .models import DialogType


class _IdentityOmitted:
    __slots__ = ()


IDENTITY_OMITTED = _IdentityOmitted()
type ObservedText = str | _IdentityOmitted | None
type ObservedDialogType = DialogType | str | _IdentityOmitted | None


@dataclass(frozen=True, slots=True)
class DialogIdentity:
    dialog_id: int
    name: str | None
    username: str | None
    dialog_type: DialogType
    observed_at: int | None
    complete: bool
    source: str | None
    display_name: str
    display_name_source: str

    @property
    def name_unavailable_reason(self) -> str | None:
        if self.name is not None:
            return None
        return "identity_not_observed" if self.source is None else "name_not_available"


@dataclass(frozen=True, slots=True)
class DialogIdentityObservation:
    dialog_id: int
    name: ObservedText = IDENTITY_OMITTED
    username: ObservedText = IDENTITY_OMITTED
    dialog_type: ObservedDialogType = IDENTITY_OMITTED
    complete: bool = False
    source: str = "realtime"
    observed_at: int | None = None
