"""Text-only native rich-message authoring intent."""

from collections.abc import Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Literal, cast

from markdown_it import MarkdownIt
from telethon.tl import types

BodyFormat = Literal["plain", "rich_markdown", "rich_html"]


@dataclass(frozen=True, slots=True)
class AuthoredBody:
    text: str
    format: BodyFormat


class _MediaDetector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.found = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {
            "img",
            "video",
            "audio",
            "source",
            "picture",
            "iframe",
            "object",
            "embed",
            "tg-document",
            "tg-map",
            "tg-collage",
            "tg-slideshow",
        }:
            self.found = True


def _has_html_media(source: str) -> bool:
    parser = _MediaDetector()
    parser.feed(source)
    return parser.found


def _has_markdown_media(source: str) -> bool:
    tokens = MarkdownIt().parse(source)
    for token in tokens:
        for item in [token, *(token.children or [])]:
            if item.type == "image" or (item.type in {"html_inline", "html_block"} and _has_html_media(item.content)):
                return True
    return False


def parse_authored_body(request: Mapping[str, object]) -> AuthoredBody | None:
    if "format" in request and "text" not in request:
        raise ValueError("format requires text")
    if "text" not in request:
        return None
    text = request["text"]
    body_format = request.get("format", "plain")
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    if body_format not in ("plain", "rich_markdown", "rich_html"):
        raise ValueError("format must be plain, rich_markdown or rich_html")
    if (body_format == "rich_markdown" and _has_markdown_media(text)) or (
        body_format == "rich_html" and _has_html_media(text)
    ):
        raise ValueError("embedded media is not supported in rich text")
    return AuthoredBody(text, cast(BodyFormat, body_format))


def native_rich_input(body: AuthoredBody) -> types.TypeInputRichMessage | None:
    if body.format == "rich_markdown":
        return types.InputRichMessageMarkdown(markdown=body.text)
    if body.format == "rich_html":
        return types.InputRichMessageHTML(html=body.text)
    return None
