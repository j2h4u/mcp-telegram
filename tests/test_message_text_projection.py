from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from typing import cast

from telethon.tl.types import MessageEntityTextUrl

from mcp_telegram.daemon_message import project_cached_message_facts, project_cached_message_facts_by_dialog
from mcp_telegram.message_content import MessageSnapshot, project_message_content
from mcp_telegram.models import ReadMessage
from mcp_telegram.telegram_message_projection import MessageLike, message_to_dict
from mcp_telegram.text_projection import render_text_links
from mcp_telegram.tools.structured import serialize_message_content


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


def test_message_content_projector_preserves_text_and_media() -> None:
    content = project_message_content(MessageSnapshot(text="caption", media_description="[photo]"))

    assert content.text == "caption"
    assert content.media_description == "[photo]"
    assert content.kind == "message_text"
    assert content.primary_text == "caption"


def test_message_content_projector_distinguishes_media_only_and_none() -> None:
    media = project_message_content(MessageSnapshot(text="", media_description="[photo]"))
    empty = project_message_content(MessageSnapshot(text=None, media_description=None))

    assert media.kind == "media_description"
    assert media.text is None
    assert media.primary_text == "[photo]"
    assert empty.kind == "none"
    assert empty.primary_text is None


def test_delivery_serializer_uses_explicit_text_media_semantics() -> None:
    text_result = serialize_message_content("caption", "[photo]", "message_text")["content"]
    assert text_result is not None
    assert text_result["content_kind"] == "message_text"
    media_result = serialize_message_content(None, "[photo]", "media_description")["content"]
    assert media_result is not None
    assert media_result["content_kind"] == "media_description"
    assert serialize_message_content("caption", "[photo]", "none")["content"] is None


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


def test_cross_dialog_folder_projection_preserves_media_and_hidden_links() -> None:
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
            (10, 20, 3, 4, "https://example.com"),
        )
        rows = project_cached_message_facts_by_dialog(
            conn,
            [
                ReadMessage(message_id=20, sent_at=1, dialog_id=10, text="go site", media_description="[photo]"),
                ReadMessage(message_id=21, sent_at=1, dialog_id=11, text=None, media_description="[video]"),
                ReadMessage(message_id=22, sent_at=1, dialog_id=12, text="", media_description=None),
            ],
        )
        assert rows[0].text == "go [site](https://example.com)"
        assert rows[0].media_description == "[photo]"
        assert rows[0].content_kind == "message_text"
        assert rows[1].text is None
        assert rows[1].media_description == "[video]"
        assert rows[1].content_kind == "media_description"
        assert rows[2].text is None
        assert rows[2].content_kind == "none"
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

    projected = message_to_dict(cast(MessageLike, message), dialog_id=10)
    assert projected["text"] == "[сайт](https://example.com)"
    assert projected["content_kind"] == "message_text"


def test_uncached_telegram_projection_sets_media_and_empty_content_kinds() -> None:
    media = SimpleNamespace(
        id=21,
        date=None,
        edit_date=None,
        message=None,
        media=object(),
        out=False,
        sender_id=10,
        sender=None,
        reactions=None,
        reply_to=None,
        entities=None,
        action=None,
    )
    projected_media = message_to_dict(cast(MessageLike, media), dialog_id=10)
    assert projected_media["content_kind"] == "media_description"
    assert projected_media["text"] is None

    empty = SimpleNamespace(**{**media.__dict__, "id": 22, "media": None})
    projected_empty = message_to_dict(cast(MessageLike, empty), dialog_id=10)
    assert projected_empty["content_kind"] == "none"


def test_uncached_telegram_projection_uses_rich_message_text_fallback() -> None:
    message = SimpleNamespace(
        id=21,
        date=None,
        edit_date=None,
        message=None,
        rich_message=SimpleNamespace(text="rich caption"),
        media=None,
        out=False,
        sender_id=10,
        sender=None,
        reactions=None,
        reply_to=None,
        entities=None,
        action=None,
    )

    assert message_to_dict(cast(MessageLike, message), dialog_id=10)["text"] == "rich caption"


def test_uncached_telegram_projection_derives_sender_and_reaction_display() -> None:
    message = SimpleNamespace(
        id=23,
        date=None,
        edit_date=None,
        message="hello",
        media=None,
        out=True,
        sender_id=None,
        sender=None,
        reactions=SimpleNamespace(
            results=[
                SimpleNamespace(reaction=SimpleNamespace(emoticon="👍"), count=2),
                SimpleNamespace(reaction=SimpleNamespace(emoticon=None, document_id=123), count=1),
                SimpleNamespace(reaction=None, count=9),
            ]
        ),
        reply_to=None,
        entities=None,
        action=None,
    )

    projected = message_to_dict(cast(MessageLike, message), dialog_id=7, self_id=100)

    assert projected["effective_sender_id"] == 100
    assert projected["reactions_display"] == "[👍×2]"
