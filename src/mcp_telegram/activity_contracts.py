"""Small cross-component contracts for activity-driven runtime paths."""

from typing import Protocol


class InputPeerResolver(Protocol):
    """Minimal async input-peer lookup required by event-driven activity."""

    async def __call__(self, dialog_id: int) -> object | None: ...


__all__ = ["InputPeerResolver"]
