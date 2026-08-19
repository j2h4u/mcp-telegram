"""Agent-facing projection of Telegram hidden text links."""

from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import quote

TextLink = tuple[int, int, str]


def render_text_links(text: str | None, links: Sequence[TextLink]) -> str | None:
    """Render Telegram ``text_url`` spans as inline Markdown links.

    Telegram offsets and lengths use UTF-16 code units. Replacements therefore
    operate on UTF-16 bytes from right to left so supplementary characters do
    not shift or corrupt later spans. Invalid or overlapping spans are ignored;
    the original text remains authoritative.
    """
    if not text or not links:
        return text

    encoded = text.encode("utf-16-le")
    next_start = len(encoded)
    for offset, length, url in sorted(links, key=lambda link: link[0], reverse=True):
        start = offset * 2
        end = start + length * 2
        if not url or offset < 0 or length <= 0 or end > next_start or end > len(encoded):
            continue
        try:
            label = encoded[start:end].decode("utf-16-le")
        except UnicodeDecodeError:
            continue
        replacement = f"[{_escape_label(label)}]({_escape_destination(url)})".encode("utf-16-le")
        encoded = encoded[:start] + replacement + encoded[end:]
        next_start = start
    return encoded.decode("utf-16-le")


def _escape_label(label: str) -> str:
    return label.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def _escape_destination(url: str) -> str:
    return quote(url, safe="/:?#[]@!$&'*+,;=%~.-_")
