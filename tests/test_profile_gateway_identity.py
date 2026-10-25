"""Collection page identities distinguish remote instances from observations."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from telethon.tl import types

from mcp_telegram.entity_profile.contracts import UserReference
from mcp_telegram.telegram_gateway import TelethonUserProfileGateway


@pytest.mark.asyncio
async def test_saved_gift_instance_identity_prefers_message_then_saved_then_unique_id() -> None:
    template = types.StarGift(900, types.DocumentEmpty(1), stars=1, convert_stars=1)
    unique = types.StarGiftUnique(303, 900, "Unique", "unique", 1, [], 1, 1)
    observed = datetime(2026, 1, 1, tzinfo=UTC)
    gifts = [
        types.SavedStarGift(observed, unique, msg_id=11, saved_id=22),
        types.SavedStarGift(observed, template, saved_id=22),
        types.SavedStarGift(observed, template, msg_id=0, saved_id=44),
        types.SavedStarGift(observed, unique, msg_id=-1, saved_id=0),
    ]
    client = AsyncMock(return_value=types.payments.SavedStarGifts(4, gifts, [], []))
    page = await TelethonUserProfileGateway(client).fetch_profile_collection_page(
        UserReference(42, 1), "saved_gifts", {}
    )
    assert [(item.key, item.identity_kind) for item in page.items] == [
        ("msg_id:11", "remote"),
        ("saved_id:22", "remote"),
        ("saved_id:44", "remote"),
        ("303", "remote"),
    ]
    assert page.reported_count == 4 and page.terminal
    client.assert_awaited_once()


@pytest.mark.asyncio
async def test_repeated_generic_gift_template_id_preserves_anonymous_occurrences() -> None:
    template = types.StarGift(900, types.DocumentEmpty(1), stars=1, convert_stars=1)
    gifts = [types.SavedStarGift(None, template), types.SavedStarGift(None, template)]
    client = AsyncMock(return_value=types.payments.SavedStarGifts(2, gifts, [], []))
    page = await TelethonUserProfileGateway(client).fetch_profile_collection_page(
        UserReference(42, 1), "saved_gifts", {}
    )
    assert [(item.key, item.identity_kind) for item in page.items] == [("0", "observation"), ("1", "observation")]
    assert [item.fields["gift"] for item in page.items] == [page.items[0].fields["gift"]] * 2
    assert len(page.items) == page.reported_count == 2
    client.assert_awaited_once()


@pytest.mark.asyncio
async def test_invalid_or_missing_photo_ids_are_observations_with_explicit_gap() -> None:
    missing = types.PhotoEmpty(5)
    del missing.id
    client = AsyncMock(
        return_value=types.photos.Photos(
            [types.PhotoEmpty(0), types.PhotoEmpty(-4), types.PhotoEmpty(True), missing], []
        )
    )
    page = await TelethonUserProfileGateway(client).fetch_profile_collection_page(
        UserReference(42, 1), "avatar_history", {}
    )
    assert [(item.key, item.identity_kind) for item in page.items] == [
        (str(index), "observation") for index in range(4)
    ]
    assert "item_identity_missing" in page.diagnostics
    assert not page.items[-1].detail_complete
    client.assert_awaited_once()


@pytest.mark.asyncio
async def test_common_chat_and_channel_with_same_numeric_id_have_distinct_keys() -> None:
    client = AsyncMock(return_value=types.messages.Chats([types.ChatEmpty(7), types.ChannelForbidden(7, 1, "Group")]))
    page = await TelethonUserProfileGateway(client).fetch_profile_collection_page(
        UserReference(42, 1), "common_chats", {}
    )
    assert [(item.key, item.identity_kind) for item in page.items] == [("chat:7", "remote"), ("channel:7", "remote")]
    assert page.terminal and not page.diagnostics
    client.assert_awaited_once()
