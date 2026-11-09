"""Rich recovery requests cross the same physical Telegram admission boundary."""

import asyncio
from datetime import UTC, datetime
from typing import cast

import pytest
from telethon.tl import functions, types

from mcp_telegram import telegram_rpc
from mcp_telegram.hydration_queue import HydrationJob
from mcp_telegram.media_hydration import RichCompositionHydrationHandler
from mcp_telegram.rich_content import capture_rich_message, decode_rich_message, rich_message_text
from mcp_telegram.telegram_demand import RpcAttemptBudget, RpcAttemptBudgetExhaustedError, RpcAttemptEvidence
from mcp_telegram.telegram_rpc_consumers import (
    TELEGRAM_RPC_CONSUMERS,
    DemandKind,
    RpcEffect,
    demand_contract,
    rpc_effect,
)
from mcp_telegram.telegram_rpc_scheduler import TelegramRpcScope, TelegramRpcSource
from tests.test_telegram_rpc_method_wire import _clock
from tests.test_telegram_rpc_wire_integration import _WireHarness
from tests.test_telegram_rpc_wire_integration import wire as wire


async def test_rich_recovery_uses_guarded_wire_and_three_attempt_limit(  # noqa: PLR0914
    wire: _WireHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate, sender, connection, sink = wire
    clock = _clock(wire, monkeypatch)
    source = TelegramRpcSource.FACT_HYDRATION_BACKFILL
    contract = demand_contract(DemandKind.BACKFILL_HYDRATION_BATCH)
    assert contract.max_rpc_attempts_per_slice == 3
    budget = RpcAttemptBudget(3)
    scope = TelegramRpcScope(
        source,
        TELEGRAM_RPC_CONSUMERS[source].admission.service_class,
        clock.monotonic() + 60,
        asyncio.current_task(),
        demand_kind=DemandKind.BACKFILL_HYDRATION_BATCH,
        attempt_budget=budget,
        attempt_evidence=RpcAttemptEvidence(),
    )

    class WireClient:
        async def get_messages(self, *, entity: int, ids: list[int]) -> object:
            assert entity == 100 and ids == [7]
            return await sender.send_owned(functions.messages.GetMessagesRequest([types.InputMessageID(7)]), scope)

        async def get_input_entity(self, dialog_id: int) -> types.InputPeerUser:
            assert dialog_id == 100
            await sender.send_owned(functions.users.GetUsersRequest([types.InputUser(100, 9)]), scope)
            return types.InputPeerUser(100, 9)

        async def __call__(self, request: object) -> object:
            return await sender.send_owned(request, scope)

    handler = RichCompositionHydrationHandler()
    assert handler.request_cost == 3 and handler.batch_size == 1
    task = asyncio.create_task(handler.request(WireClient(), [HydrationJob(handler.kind, 100, 7, 0, 0)]))
    partial = types.RichMessage([types.PageBlockParagraph(types.TextPlain("partial"))], [], [], part=True)
    complete = types.RichMessage([types.PageBlockParagraph(types.TextPlain("complete"))], [], [])
    first = types.Message(id=7, peer_id=types.PeerUser(100), date=None, message="", rich_message=partial)
    expanded = types.Message(id=7, peer_id=types.PeerUser(100), date=None, message="", rich_message=complete)
    responses = ([first], [], types.messages.Messages(messages=[expanded], chats=[], users=[], topics=[]))
    expected = (
        functions.messages.GetMessagesRequest,
        functions.users.GetUsersRequest,
        functions.messages.GetRichMessageRequest,
    )
    for index, (request_type, response) in enumerate(zip(expected, responses, strict=True)):
        state = await asyncio.wait_for(sender._send_queue.next_state(), 3)
        request = state.request
        assert isinstance(request, request_type)
        assert rpc_effect(telegram_rpc._canonical_wire_method(request)) is RpcEffect.NONMUTATING_READ
        if isinstance(request, functions.messages.GetRichMessageRequest):
            assert telegram_rpc._request_peers(request, 42, None) == (100,)
        await sender._prepare_state(state)
        assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
        assert budget.attempts == index + 1
        state.future.set_result(response)
        clock.advance(3)
    assert await asyncio.wait_for(task, 3) == [expanded]
    assert len(sink.frames) == 3
    fourth = sender.send_owned(functions.users.GetUsersRequest([types.InputUser(100, 9)]), scope)
    state = await sender._send_queue.next_state()
    with pytest.raises(RpcAttemptBudgetExhaustedError):
        await sender._prepare_state(state)
    with pytest.raises(RpcAttemptBudgetExhaustedError):
        await fourth
    assert len(sink.frames) == 3 and budget.attempts == 3


def test_embedded_post_author_is_visible_text_without_url() -> None:
    plain = types.TextPlain
    post = types.PageBlockEmbedPost(
        "https://example.test/private",
        99,
        42,
        "Visible author",
        datetime(2026, 1, 1, tzinfo=UTC),
        [types.PageBlockParagraph(plain("post body"))],
        types.PageCaption(plain("caption"), plain("credit")),
    )
    tree, status, text_status = decode_rich_message(capture_rich_message(types.RichMessage([post], [], [])))
    assert tree is not None and status == "complete" and text_status == "complete"
    assert rich_message_text(cast(dict[str, object], tree)) == "Visible author\npost body\ncaption\ncredit"


async def test_wrong_peer_response_stops_before_expansion(wire: _WireHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    _, sender, connection, sink = wire
    clock = _clock(wire, monkeypatch)
    source = TelegramRpcSource.FACT_HYDRATION_BACKFILL
    budget = RpcAttemptBudget(3)
    scope = TelegramRpcScope(
        source,
        TELEGRAM_RPC_CONSUMERS[source].admission.service_class,
        clock.monotonic() + 60,
        asyncio.current_task(),
        demand_kind=DemandKind.BACKFILL_HYDRATION_BATCH,
        attempt_budget=budget,
        attempt_evidence=RpcAttemptEvidence(),
    )

    class WireClient:
        async def get_messages(self, **_kwargs: object) -> object:
            return await sender.send_owned(functions.messages.GetMessagesRequest([types.InputMessageID(7)]), scope)

    handler = RichCompositionHydrationHandler()
    task = asyncio.create_task(handler.request(WireClient(), [HydrationJob(handler.kind, 100, 7, 0, 0)]))
    state = await asyncio.wait_for(sender._send_queue.next_state(), 3)
    await sender._prepare_state(state)
    assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
    wrong = types.Message(
        id=7,
        peer_id=types.PeerUser(101),
        date=None,
        message="",
        rich_message=types.RichMessage([], [], [], part=True),
    )
    state.future.set_result([wrong])
    with pytest.raises(ValueError, match="different peer"):
        await asyncio.wait_for(task, 3)
    assert len(sink.frames) == 1 and budget.attempts == 1


def test_diff_and_channel_visible_fields_keep_boundaries_and_exclude_metadata() -> None:
    rich = types.RichMessage(
        [
            types.PageBlockParagraph(types.TextDiff(types.TextPlain("new"), types.TextPlain("removed-needle"))),
            types.PageBlockChannel(
                types.Channel(
                    id=53, title="visible channel", photo=types.ChatPhotoEmpty(), date=None, username="hidden-metadata"
                )
            ),
        ],
        [],
        [],
    )
    tree, status, text_status = decode_rich_message(capture_rich_message(rich))
    assert tree is not None and status == "complete" and text_status == "complete"
    assert rich_message_text(tree) == "new\nremoved-needle\nvisible channel"


def test_rich_reference_gaps_distinguish_declared_missing_and_external() -> None:
    from mcp_telegram.rich_content import rich_media_reference_gaps

    empty = types.TextEmpty()
    caption = types.PageCaption(empty, empty)
    rich = types.RichMessage(
        [
            types.PageBlockPhoto(-5, caption),
            types.PageBlockPhoto(8, caption),
            types.PageBlockVideo(9, caption),
            types.PageBlockAudio(10, caption),
            types.PageBlockDocument(10, caption),
            types.PageBlockEmbed(caption, url="https://example.test/ignored", poster_photo_id=0),
            types.PageBlockEmbed(caption, poster_photo_id=12),
            types.PageBlockEmbedPost("https://example.test/ignored", 1, 13, "author", None, [], caption),
            types.PageBlockRelatedArticles(
                empty, [types.PageRelatedArticle("https://example.test/ignored", 2, photo_id=14)]
            ),
            types.PageBlockParagraph(types.TextCustomEmoji(333, "emoji")),
            types.PageBlockParagraph(types.TextImage(444, 1, 1)),
        ],
        [types.PhotoEmpty(-5), types.PhotoEmpty(14)],
        [types.DocumentEmpty(9)],
    )
    tree, _, _ = decode_rich_message(capture_rich_message(rich))
    assert tree is not None
    assert rich_media_reference_gaps(tree) == {"missing_photo_ids": [8, 12, 13], "missing_document_ids": [10]}
    cast(list[object], tree["blocks"]).append({"_": "PageBlockPhoto", "photo_id": True})
    assert rich_media_reference_gaps(tree) == {"missing_photo_ids": [8, 12, 13], "missing_document_ids": [10]}
