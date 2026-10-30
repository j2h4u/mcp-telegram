"""A photo redirect must originate in a guarded Telegram response."""

import asyncio
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from telethon import functions, types
from telethon.errors import FileMigrateError

from mcp_telegram.auth_scope import TelegramAuthScope
from mcp_telegram.telegram_image_download import PhotoBudget, PhotoLocator, PhotoSourceFence, PhotoWork
from mcp_telegram.telegram_rpc import TelegramRpcGate, _GuardedPhotoSession


async def test_redirect_requires_observed_file_migrate_in_active_slice() -> None:
    request = functions.upload.GetFileRequest(
        location=types.InputPhotoFileLocation(id=1, access_hash=2, file_reference=b"r", thumb_size="m"),
        offset=0,
        limit=524288,
    )
    reserve_redirect = AsyncMock()
    gate = SimpleNamespace(
        _photo_file_request=AsyncMock(side_effect=FileMigrateError(request, capture=5)),
        _close_photo_aux_lease=AsyncMock(),
        _ensure_photo_transport=AsyncMock(),
    )
    task = asyncio.current_task()
    assert task is not None
    session = _GuardedPhotoSession(
        cast(TelegramRpcGate, gate),
        PhotoLocator(1, "m", 2, 100, 1, 1, request.location),
        TelegramAuthScope(1, 42, 2, 10),
        PhotoWork("work", "attempt", "source"),
        cast(PhotoBudget, SimpleNamespace(reserve_redirect=reserve_redirect)),
        cast(PhotoSourceFence, object()),
        task,
    )
    gate._photo_session = session
    with pytest.raises(RuntimeError, match="migration is unavailable"):
        await session.migrate(5)
    session.slicing = True
    with pytest.raises(RuntimeError, match="migration is unavailable"):
        await session.migrate(5)
    with pytest.raises(FileMigrateError):
        await session.get_file(request)
    with pytest.raises(RuntimeError, match="migration is unavailable"):
        await session.migrate(6)
    reserve_redirect.assert_not_awaited()
    await session.migrate(5)
    reserve_redirect.assert_awaited_once_with(5)
    with pytest.raises(RuntimeError, match="migration is unavailable"):
        await session.migrate(5)
