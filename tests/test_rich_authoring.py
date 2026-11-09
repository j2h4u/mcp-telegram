from collections.abc import Mapping
from typing import cast

import pytest
from telethon.tl import types

from mcp_telegram.rich_authoring import AuthoredBody, BodyFormat, native_rich_input, parse_authored_body


def test_body_presence_and_native_sources():
    assert parse_authored_body({}) is None
    assert parse_authored_body({"text": "hello"}) == AuthoredBody("hello", "plain")
    assert native_rich_input(AuthoredBody("hello", "plain")) is None
    assert isinstance(native_rich_input(AuthoredBody("# heading", "rich_markdown")), types.InputRichMessageMarkdown)
    assert isinstance(native_rich_input(AuthoredBody("<b>bold</b>", "rich_html")), types.InputRichMessageHTML)


@pytest.mark.parametrize("payload", [{"format": "plain"}, {"text": "x", "format": None}, {"text": None}])
def test_invalid_body_presence(payload: dict[str, object]):
    with pytest.raises(ValueError):
        parse_authored_body(payload)


@pytest.mark.parametrize(
    "text,format",
    [
        ("![caption](https://example.com/a.png)", "rich_markdown"),
        ("<IMG src='x'>", "rich_html"),
        ("<video src='x'></video>", "rich_markdown"),
        ("<tg-document src='x'/>", "rich_html"),
        ("<tg-map latitude='1' longitude='2'/>", "rich_html"),
        ("<tg-document src='x'></tg-document>", "rich_markdown"),
        ("<tg-collage></tg-collage>", "rich_html"),
        ("<tg-slideshow></tg-slideshow>", "rich_html"),
    ],
)
def test_embedded_media_rejected(text: str, format: BodyFormat):
    with pytest.raises(ValueError, match="media"):
        parse_authored_body({"text": text, "format": format})


def test_text_structures_allowed():
    text = "# Heading\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n- caption\n\n$e=mc^2$"
    assert parse_authored_body({"text": text, "format": "rich_markdown"}) == AuthoredBody(text, "rich_markdown")


def test_native_tree_survives_structured_wire_compaction():
    from mcp_telegram.tools._base import omit_none_mapping_values, structured_result

    tree = {"blocks": [{"type": "paragraph", "caption": None, "nested": {"value": None}}]}
    value = {"current": {"rich_message": tree, "other": None}, "wrapper": None}
    result = structured_result(cast(Mapping[str, object], omit_none_mapping_values(value)))
    assert result.structured_content == {"current": {"rich_message": tree}}


def test_public_field_presence_and_action_rules():
    from pydantic import ValidationError

    from mcp_telegram.tools.drafts import ManageDraft, _draft_request
    from mcp_telegram.tools.scheduled import ManageScheduledMessage, _scheduled_request

    draft = ManageDraft(exact_dialog_id=1, action="create", operation_key="k", text="hello")
    assert "format" not in _draft_request(draft, "manage_draft")
    assert _draft_request(draft, "manage_draft")["drop_formatting"] is False
    update = ManageScheduledMessage(
        exact_dialog_id=1,
        action="update",
        operation_key="k",
        scheduled_message_id=1,
        expected_version="v",
        schedule_at_utc="2030-01-01T00:00:00Z",
    )
    assert "format" not in _scheduled_request(update) and "text" not in _scheduled_request(update)
    with pytest.raises(ValidationError):
        ManageDraft(exact_dialog_id=1, action="clear", operation_key="k", expected_version="v", format="plain")
    with pytest.raises(ValidationError):
        ManageScheduledMessage(
            exact_dialog_id=1,
            action="update",
            operation_key="k",
            scheduled_message_id=1,
            expected_version="v",
            schedule_at_utc="2030-01-01T00:00:00Z",
            format="plain",
        )
    with pytest.raises(ValidationError):
        ManageDraft(
            exact_dialog_id=1,
            action="update",
            operation_key="k",
            expected_version="v",
            text="x",
            format="rich_html",
            drop_formatting=True,
        )
