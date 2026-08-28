"""Canonical agent-facing projection of Telegram message content."""

from __future__ import annotations

from dataclasses import dataclass

from .media_fact import decode_media_fact, media_description
from .models import ContentKind
from .text_projection import TextLink, render_text_links


@dataclass(frozen=True, slots=True)
class MessageSnapshot:
    """Raw content facts before agent-facing rendering."""

    text: str | None
    media_kind: str | None = None
    media_payload: str | None = None
    text_links: tuple[TextLink, ...] = ()


@dataclass(frozen=True, slots=True)
class MessageContent:
    """Rendered content while retaining both text and media facts."""

    text: str | None
    media_description: str | None
    kind: ContentKind
    media_kind: str | None = None

    @property
    def primary_text(self) -> str | None:
        """Return text when available, otherwise the media description."""
        return self.text if self.text is not None else self.media_description


def project_message_content(snapshot: MessageSnapshot) -> MessageContent:
    """Project raw message content into the canonical agent-facing shape.

    Telegram text-url entities are rendered only in text. Blank values become
    ``None`` so callers can distinguish no content from an empty display value.
    """
    text = _normalize(snapshot.text)
    fact = decode_media_fact(snapshot.media_kind, snapshot.media_payload)
    media_kind = None if fact is None else fact.kind
    description = _normalize(media_description(fact))
    rendered_text = render_text_links(text, snapshot.text_links)
    if rendered_text is not None:
        kind: ContentKind = "message_text"
    elif description is not None:
        kind = "media_description"
    else:
        kind = "none"
    return MessageContent(
        text=rendered_text,
        media_description=description,
        kind=kind,
        media_kind=media_kind,
    )


def project_message_text(text: str | None, text_links: tuple[TextLink, ...] = ()) -> str | None:
    """Render message text and persisted hidden links without media input."""
    return render_text_links(_normalize(text), text_links)


def _normalize(value: str | None) -> str | None:
    return value or None


__all__ = ["ContentKind", "MessageContent", "MessageSnapshot", "project_message_content", "project_message_text"]
