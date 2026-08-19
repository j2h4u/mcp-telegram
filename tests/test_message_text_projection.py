from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from typing import cast

from telethon.tl.types import MessageEntityTextUrl

from mcp_telegram.daemon_message import project_cached_message_facts
from mcp_telegram.models import ReadMessage
from mcp_telegram.telegram_message_projection import MessageLike, message_to_dict
from mcp_telegram.text_projection import render_text_links


def test_render_text_links_uses_telegram_utf16_offsets() -> None:
    text = "🚀 Смотри сайт и канал"
    # The leading emoji occupies two UTF-16 code units.
    links = [(10, 4, "https://example.com/a_(b)"), (17, 5, "https://t.me/example")]

    assert render_text_links(text, links) == (
        "🚀 Смотри [сайт](https://example.com/a_%28b%29) и [канал](https://t.me/example)"
    )


def test_render_text_links_escapes_markdown_label_syntax() -> None:
    assert render_text_links("read [this]", [(5, 6, "https://example.com")]) == (
        "read [\\[this\\]](https://example.com)"
    )


def test_cached_message_projection_renders_persisted_hidden_link() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(
            """
            CREATE TABLE message_entities (
                dialog_id INTEGER, message_id INTEGER, offset INTEGER,
                length INTEGER, type TEXT, value TEXT
            );
            CREATE TABLE message_reactions (
                dialog_id INTEGER, message_id INTEGER, emoji TEXT, count INTEGER
            );
            """
        )
        conn.execute(
            "INSERT INTO message_entities VALUES (?, ?, ?, ?, 'text_url', ?)",
            (10, 20, 0, 4, "https://example.com"),
        )

        projected = project_cached_message_facts(
            conn,
            10,
            [ReadMessage(message_id=20, sent_at=1, dialog_id=10, text="сайт")],
        )

        assert projected[0].text == "[сайт](https://example.com)"
    finally:
        conn.close()


def test_uncached_telegram_projection_renders_hidden_link() -> None:
    message = SimpleNamespace(
        id=20,
        date=None,
        edit_date=None,
        message="сайт",
        media=None,
        out=False,
        sender_id=10,
        sender=None,
        reactions=None,
        reply_to=None,
        entities=[MessageEntityTextUrl(offset=0, length=4, url="https://example.com")],
        action=None,
    )

    assert message_to_dict(cast(MessageLike, message), dialog_id=10)["text"] == "[сайт](https://example.com)"
