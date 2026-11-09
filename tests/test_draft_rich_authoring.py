"""Rich source intent and authoritative draft observations remain distinct."""

from typing import cast

import pytest
from telethon.tl import types

from mcp_telegram.drafts.commands import DraftCommandOwner
from mcp_telegram.drafts.history import read_current_draft
from tests.sqlite_writer_helpers import inline_writer
from tests.test_draft_commands import _Harness
from tests.test_draft_commands import harness as harness


async def test_rich_update_dispatches_and_replays_complete_observation(harness: _Harness) -> None:
    harness.set_remote("", rich_message=types.RichMessage([types.PageBlockHeading1(types.TextPlain("same"))], [], []))
    harness.publish()

    async def postflight(deadline: float) -> bool:
        harness.set_remote(
            "", rich_message=types.RichMessage([types.PageBlockHeading2(types.TextPlain("same"))], [], [])
        )
        harness.publish()
        return True

    owner = DraftCommandOwner(
        harness,
        harness.conn,
        writer=inline_writer(harness.conn),
        account_id=100,
        resolve_peer=harness.resolve_peer,
        preflight=harness.preflight,
        request_recovery=harness.recover,
        postflight=postflight,
    )
    request = harness.command("update", "rich-change", text="## same", format="rich_markdown")
    result = await owner.execute(request)
    assert isinstance(harness.requests[0].rich_message, types.InputRichMessageMarkdown)
    assert harness.requests[0].message == "" and harness.requests[0].entities is None
    assert result["verification"] == {"status": "observed", "basis": "post_write_snapshot"}
    assert result["current_is_operation_confirmation"] is False
    current = cast(dict[str, object], result["current"])
    assert current["rich_content_status"] == "complete"
    assert current["expected_version"] == read_current_draft(harness.conn, harness.scope).expected_version
    assert await owner.execute(request) == result


@pytest.mark.parametrize(
    "fields",
    [
        {"format": None, "text": "x"},
        {"format": "rich_html"},
        {"text": "x", "format": "rich_html", "drop_formatting": True},
    ],
)
async def test_invalid_rich_envelopes_never_dispatch(harness: _Harness, fields: dict[str, object]) -> None:
    harness.set_remote("old")
    harness.publish()
    result = await harness.owner.execute(harness.command("update", "invalid", **fields))
    assert result["error"] == "invalid_arguments"
    assert harness.requests == []


@pytest.mark.parametrize("unknown,partial", [(False, True), (True, False)])
async def test_incomplete_or_unknown_write_never_claims_observation(
    harness: _Harness, unknown: bool, partial: bool
) -> None:
    async def postflight(deadline: float) -> bool:
        harness.set_remote(
            "", rich_message=types.RichMessage([types.PageBlockHeading1(types.TextPlain("x"))], [], [], part=partial)
        )
        harness.publish()
        return True

    if unknown:
        harness.after_error = TimeoutError()
    owner = DraftCommandOwner(
        harness,
        harness.conn,
        writer=inline_writer(harness.conn),
        account_id=100,
        resolve_peer=harness.resolve_peer,
        preflight=harness.preflight,
        request_recovery=harness.recover,
        postflight=postflight,
    )
    result = await owner.execute(harness.command("create", "uncertain-rich", text="# x", format="rich_markdown"))
    assert cast(dict[str, object], result["verification"])["status"] == "unverified"
    assert result["current_is_operation_confirmation"] is False
    assert result["outcome"] == ("unknown" if unknown else "acknowledged")


async def test_rich_replacement_refuses_unretained_media(harness: _Harness) -> None:
    harness.set_remote("old", media=types.InputMediaUploadedPhoto(file=types.InputFile(1, 1, "x", "a")))
    harness.publish()
    result = await harness.owner.execute(harness.command("update", "media", text="<b>x</b>", format="rich_html"))
    assert result["error"] == "media_dependencies_not_retained"
    assert harness.requests == []


async def test_drop_formatting_dispatches_even_when_text_is_unchanged(harness: _Harness) -> None:
    harness.set_remote("same", entities=[types.MessageEntityBold(0, 4)])
    harness.publish()
    result = await harness.owner.execute(harness.command("update", "drop", text="same", drop_formatting=True))
    assert result["outcome"] == "acknowledged"
    assert harness.requests[0].message == "same"
    assert harness.requests[0].entities is None


@pytest.mark.parametrize("unknown", [False, True])
async def test_oversized_observation_keeps_durable_result_and_canonical_fact(harness: _Harness, unknown: bool) -> None:
    blocks: list[types.TypePageBlock] = [types.PageBlockParagraph(types.TextPlain("x" * 900)) for _ in range(600)]

    async def postflight(deadline: float) -> bool:
        harness.set_remote("", rich_message=types.RichMessage(blocks, [], []))
        harness.publish()
        return True

    if unknown:
        harness.after_error = TimeoutError()
    owner = DraftCommandOwner(
        harness,
        harness.conn,
        writer=inline_writer(harness.conn),
        account_id=100,
        resolve_peer=harness.resolve_peer,
        preflight=harness.preflight,
        request_recovery=harness.recover,
        postflight=postflight,
    )
    request = harness.command("create", "large", text="# draft", format="rich_markdown")
    result = await owner.execute(request)
    assert result["outcome"] == ("unknown" if unknown else "acknowledged")
    assert result["result_persisted"] is True
    assert result["verification"] == {"status": "unverified", "basis": "none", "reason": "snapshot_too_large"}
    assert result["current_is_operation_confirmation"] is False
    current = cast(dict[str, object], result["current"])
    state = read_current_draft(harness.conn, harness.scope)
    assert current["expected_version"] == state.expected_version
    assert current["content_included"] is False and "rich_message" not in current
    assert state.rich_message_json is not None and len(state.rich_message_json) > 512 * 1024
    assert await owner.execute(request) == result
