"""Local image delivery contracts; acquisition credentials remain private."""

from collections.abc import Callable
from dataclasses import dataclass

from ..image_authorization import ActiveSources, ScopeToken


@dataclass(frozen=True, slots=True)
class ImageScope:
    """Capture on the event loop and compare through a thread-safe live guard."""

    capture: Callable[[], ScopeToken | None]
    is_current: Callable[[ScopeToken], bool]
    sources: ActiveSources


@dataclass(frozen=True, slots=True)
class ImageReadResult:
    """Status and optional validated native image bytes for one local read."""

    status: dict[str, object]
    image_bytes: bytes | None = None
    mime_type: str | None = None
