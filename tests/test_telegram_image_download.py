"""Photo selection keeps only bounded native still renditions."""

import logging
from datetime import UTC, datetime
from logging import Logger
from types import SimpleNamespace
from typing import cast

import pytest
from telethon import errors
from telethon.tl import functions, types

from mcp_telegram.telegram_image_download import (
    PHOTO_CHUNK_BYTES,
    PHOTO_MAX_BYTES,
    PhotoLocator,
    PhotoUnsupportedError,
    iter_photo_chunks,
    photo_locator_from_message,
)


def _message(*, ttl: int | None = None, live: bool = False) -> object:
    photo = types.Photo(
        id=7,
        access_hash=11,
        file_reference=b"private",
        date=datetime.now(UTC),
        dc_id=3,
        sizes=[
            types.PhotoSize("s", 40, 40, 100),
            types.PhotoSizeProgressive("m", 100, 100, [1000, 2000]),
            types.PhotoSize("large", 200, 200, PHOTO_MAX_BYTES + 1),
            types.PhotoStrippedSize("i", b"inline"),
        ],
    )
    return SimpleNamespace(
        media=types.MessageMediaPhoto(photo=photo, ttl_seconds=ttl, live_photo=live), ttl_period=None
    )


def test_selects_largest_complete_bounded_still_rendition_without_exposing_locator() -> None:
    result = photo_locator_from_message(_message())
    assert (result.photo_id, result.dc_id, result.rendition, result.size, result.width, result.height) == (
        7,
        3,
        "m",
        2000,
        100,
        100,
    )
    assert type(result.location) is types.InputPhotoFileLocation
    assert "private" not in repr(result)


@pytest.mark.parametrize("message", [_message(ttl=5), _message(live=True), SimpleNamespace(media=None)])
def test_nonordinary_photo_is_rejected(message: object) -> None:
    with pytest.raises(PhotoUnsupportedError):
        photo_locator_from_message(message)


class _Dispatch:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.requests: list[tuple[int, int, bool | None, bytes]] = []
        self.destinations: list[int] = []

    async def get_file(self, request: object) -> object:
        assert isinstance(request, functions.upload.GetFileRequest)
        self.requests.append((request.offset, request.limit, request.cdn_supported, bytes(request.location)))
        result = self.outcomes.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def migrate(self, dc_id: int) -> None:
        self.destinations.append(dc_id)


def _file(data: bytes) -> object:
    return types.upload.File(types.storage.FileJpeg(), 0, data)


def _locator(size: int, dc: int = 4) -> PhotoLocator:
    location = types.InputPhotoFileLocation(id=7, access_hash=11, file_reference=b"private", thumb_size="m")
    return PhotoLocator(7, "m", dc, size, 100, 100, location)


def _logs() -> dict[str, Logger]:
    return {"telethon.client.downloads": logging.getLogger("test.photo")}


@pytest.mark.asyncio
async def test_real_iterator_bounded_slice_and_resume() -> None:
    size = 2 * PHOTO_CHUNK_BYTES + 3
    locator = _locator(size)
    dispatch = _Dispatch([_file(b"a" * PHOTO_CHUNK_BYTES), _file(b"b" * PHOTO_CHUNK_BYTES), _file(b"end")])
    first = [chunk async for chunk in iter_photo_chunks(locator, offset=0, dispatch=dispatch, log=_logs())]
    second = [
        chunk
        async for chunk in iter_photo_chunks(locator, offset=2 * PHOTO_CHUNK_BYTES, dispatch=dispatch, log=_logs())
    ]
    assert [len(part) for part in first + second] == [PHOTO_CHUNK_BYTES, PHOTO_CHUNK_BYTES, 3]
    assert [request[0] for request in dispatch.requests] == [0, PHOTO_CHUNK_BYTES, 2 * PHOTO_CHUNK_BYTES]
    assert all(request[1] == PHOTO_CHUNK_BYTES and not request[2] for request in dispatch.requests)
    assert all(
        request[3] == bytes(cast(types.InputPhotoFileLocation, locator.location)) for request in dispatch.requests
    )
    assert dispatch.destinations == []


@pytest.mark.asyncio
async def test_real_iterator_migration_retries_through_callback_and_counts_request() -> None:
    dispatch = _Dispatch([errors.FileMigrateError(None, capture=5), _file(b"a" * PHOTO_CHUNK_BYTES), _file(b"x")])
    chunks = [
        chunk
        async for chunk in iter_photo_chunks(_locator(PHOTO_CHUNK_BYTES + 1), offset=0, dispatch=dispatch, log=_logs())
    ]
    assert len(chunks) == 1 and len(chunks[0]) == PHOTO_CHUNK_BYTES
    assert dispatch.destinations == [5]
    assert [request[0] for request in dispatch.requests] == [0, 0]


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [errors.FileReferenceExpiredError(None), errors.FilerefUpgradeNeededError(None)])
async def test_reference_errors_propagate_without_vendor_refresh(exc: Exception) -> None:
    dispatch = _Dispatch([exc])
    with pytest.raises(type(exc)):
        _ = [chunk async for chunk in iter_photo_chunks(_locator(100), offset=0, dispatch=dispatch, log=_logs())]
    assert len(dispatch.requests) == 1


@pytest.mark.asyncio
async def test_timeout_never_sleeps_or_retries() -> None:
    dispatch = _Dispatch([errors.TimedOutError(None, "timeout")])
    with pytest.raises(TimeoutError):
        _ = [chunk async for chunk in iter_photo_chunks(_locator(100), offset=0, dispatch=dispatch, log=_logs())]
    assert len(dispatch.requests) == 1


@pytest.mark.asyncio
async def test_early_eof_and_invalid_locator_fail_closed() -> None:
    dispatch = _Dispatch([_file(b"short")])
    with pytest.raises(PhotoUnsupportedError, match="mismatch"):
        _ = [chunk async for chunk in iter_photo_chunks(_locator(100), offset=0, dispatch=dispatch, log=_logs())]
    bad = _locator(100)
    cast(types.InputPhotoFileLocation, bad.location).id = 8
    with pytest.raises(ValueError, match="locator"):
        _ = [chunk async for chunk in iter_photo_chunks(bad, offset=0, dispatch=dispatch, log=_logs())]
    assert len(dispatch.requests) == 1


@pytest.mark.asyncio
async def test_cdn_redirect_is_rejected_before_vendor_cdn_handling() -> None:
    redirect = types.upload.FileCdnRedirect(7, b"private", b"key", b"iv", [])
    dispatch = _Dispatch([redirect])
    with pytest.raises(PhotoUnsupportedError, match="redirect"):
        _ = [chunk async for chunk in iter_photo_chunks(_locator(100), offset=0, dispatch=dispatch, log=_logs())]
    assert len(dispatch.requests) == 1 and dispatch.destinations == []


@pytest.mark.asyncio
async def test_second_call_migration_stops_slice_without_losing_first_chunk() -> None:
    dispatch = _Dispatch([_file(b"a" * PHOTO_CHUNK_BYTES), errors.FileMigrateError(None, capture=5)])
    chunks = [
        chunk
        async for chunk in iter_photo_chunks(_locator(PHOTO_CHUNK_BYTES + 1), offset=0, dispatch=dispatch, log=_logs())
    ]
    assert len(chunks) == 1 and len(chunks[0]) == PHOTO_CHUNK_BYTES
    assert dispatch.destinations == [5] and len(dispatch.requests) == 2


@pytest.mark.asyncio
async def test_mutated_location_cannot_dispatch_second_request() -> None:
    locator = _locator(PHOTO_CHUNK_BYTES + 1)

    class MutatingDispatch(_Dispatch):
        async def get_file(self, request: object) -> object:
            result = await super().get_file(request)
            assert isinstance(request, functions.upload.GetFileRequest)
            assert isinstance(request.location, types.InputPhotoFileLocation)
            request.location.id = 999
            return result

    dispatch = MutatingDispatch([_file(b"a" * PHOTO_CHUNK_BYTES), _file(b"b")])
    with pytest.raises(RuntimeError, match="capability"):
        _ = [chunk async for chunk in iter_photo_chunks(locator, offset=0, dispatch=dispatch, log=_logs())]
    assert len(dispatch.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("offset", [-1, 1, PHOTO_CHUNK_BYTES])
async def test_invalid_offsets_never_dispatch(offset: int) -> None:
    dispatch = _Dispatch([])
    with pytest.raises(ValueError, match="offset"):
        _ = [chunk async for chunk in iter_photo_chunks(_locator(100), offset=offset, dispatch=dispatch, log=_logs())]
    assert dispatch.requests == []
