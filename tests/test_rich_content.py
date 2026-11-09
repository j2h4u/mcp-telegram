import json
from types import SimpleNamespace

from telethon.tl import types

from mcp_telegram.messages.telegram_adapter import extract_message_text
from mcp_telegram.rich_content import capture_rich_message, decode_rich_message, rich_message_text


def test_rich_message_captures_table_media_and_plain_text() -> None:
    plain = types.TextPlain
    rich = types.RichMessage(
        [
            types.PageBlockParagraph(plain("Intro")),
            types.PageBlockTable(
                plain("Table"),
                [
                    types.PageTableRow(
                        [types.PageTableCell(text=plain("First")), types.PageTableCell(text=plain("Second"))]
                    )
                ],
                bordered=True,
            ),
            types.PageBlockPhoto(42, types.PageCaption(plain("Caption"), plain("Credit")), url="https://example.com"),
        ],
        [],
        [],
        rtl=True,
    )
    payload = capture_rich_message(rich)
    tree, status, text_status = decode_rich_message(payload)
    assert tree is not None
    assert status == "complete" and text_status == "complete"
    assert rich_message_text(tree) == "Intro\nTable\nFirst\tSecond\nCaption\nCredit"
    assert tree["rtl"] is True
    blocks = tree["blocks"]
    assert isinstance(blocks, list) and isinstance(blocks[2], dict)
    assert blocks[2]["photo_id"] == 42
    assert extract_message_text(SimpleNamespace(message="", rich_message=rich)) == rich_message_text(tree)
    assert extract_message_text(SimpleNamespace(message="Intro", rich_message=rich)) == rich_message_text(tree)
    assert extract_message_text(
        SimpleNamespace(message=rich_message_text(tree), rich_message=rich)
    ) == rich_message_text(tree)


def test_capture_states_and_unknown_constructor() -> None:
    assert decode_rich_message(None) == (None, "unknown", None)
    assert decode_rich_message(capture_rich_message(None)) == (None, "absent", None)
    partial = types.RichMessage([types.PageBlockParagraph(types.TextPlain("known"))], [], [], part=True)
    assert decode_rich_message(capture_rich_message(partial))[1:] == ("partial", "complete")
    unsupported = {
        "_": "RichMessage",
        "blocks": [{"_": "FutureBlock", "text": {"_": "TextPlain", "text": "safe"}, "url": "secret"}],
    }
    tree, status, text_status = decode_rich_message(json.dumps(unsupported))
    assert isinstance(tree, dict)
    assert tree == unsupported and status == "complete" and text_status == "unsupported"
    assert rich_message_text(tree) == "safe"
    assert decode_rich_message(capture_rich_message(types.RichMessage([types.PageBlockUnsupported()], [], [])))[1:] == (
        "partial",
        "unsupported",
    )


def test_constructor_inventory_and_separate_native_text() -> None:
    from mcp_telegram.entity_profile.field_manifest import TYPES_PAGE_BLOCK, TYPES_RICH_TEXT
    from mcp_telegram.rich_content import _FIELDS

    assert set(TYPES_PAGE_BLOCK) - _FIELDS.keys() == {"InputPageBlockMap", "PageBlockUnsupported"}
    assert set(TYPES_RICH_TEXT) <= _FIELDS.keys()
    rich = types.RichMessage([types.PageBlockParagraph(types.TextPlain("body"))], [], [])
    assert extract_message_text(SimpleNamespace(message="native", rich_message=rich)) == "native\nbody"
    assert extract_message_text(SimpleNamespace(message="plain", rich_message=None)) == "plain"


def test_distinct_semantic_fields_and_nested_placeholder() -> None:
    plain = types.TextPlain
    rich = types.RichMessage(
        [
            types.PageBlockBlockquote(plain("quotation"), plain("attribution")),
            types.PageBlockDetails([types.PageBlockParagraph(types.TextBold(plain("nested")))], plain("details")),
            types.PageBlockPhoto(12, types.PageCaption(plain("caption"), plain("credit"))),
        ],
        [],
        [],
    )
    tree, status, text_status = decode_rich_message(capture_rich_message(rich))
    assert tree is not None and status == "complete" and text_status == "complete"
    assert rich_message_text(tree) == "quotation\nattribution\ndetails\nnested\ncaption\ncredit"
    nested = types.RichMessage([types.PageBlockDetails([types.PageBlockUnsupported()], plain("title"))], [], [])
    assert decode_rich_message(capture_rich_message(nested))[1:] == ("partial", "unsupported")
