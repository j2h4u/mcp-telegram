"""Private contracts for explicitly requested, bounded Telegram photo acquisition."""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from logging import Logger
from types import SimpleNamespace
from typing import TYPE_CHECKING, Protocol, SupportsBytes, cast

from telethon import errors  # type: ignore[import-untyped]
from telethon.client.downloads import DownloadMethods  # type: ignore[import-untyped]
from telethon.tl import (
    functions,  # type: ignore[import-untyped]
    types,  # type: ignore[import-untyped]
)

from .auth_scope import TelegramAuthScope
from .message_images.limits import (
    PHOTO_CHUNK_BYTES as PHOTO_CHUNK_BYTES,
)
from .message_images.limits import (
    PHOTO_MAX_ATTEMPTS as PHOTO_MAX_ATTEMPTS,
)
from .message_images.limits import (
    PHOTO_MAX_BYTES as PHOTO_MAX_BYTES,
)
from .message_images.limits import (
    PHOTO_MAX_LEASE_CONTROLS as PHOTO_MAX_LEASE_CONTROLS,
)
from .message_images.limits import (
    PHOTO_MAX_SETUP_ATTEMPTS as PHOTO_MAX_SETUP_ATTEMPTS,
)
from .message_images.limits import (
    PHOTO_MAX_SLICE_REQUESTS as PHOTO_MAX_SLICE_REQUESTS,
)

if TYPE_CHECKING:
    from telethon.client.telegramclient import TelegramClient


@dataclass(frozen=True, slots=True)
class PhotoTarget:
    dialog_id: int
    message_id: int


@dataclass(frozen=True, slots=True, repr=False)
class PhotoLocator:
    photo_id: int
    rendition: str
    dc_id: int
    size: int
    width: int | None
    height: int | None
    location: object = field(repr=False)


@dataclass(frozen=True, slots=True)
class PhotoWork:
    key: str
    attempt_generation: str
    source_generation: str


@dataclass(frozen=True, slots=True)
class PhotoObservation:
    message: object = field(repr=False)
    locator: PhotoLocator


class PhotoUnsupportedError(ValueError):
    """A message does not expose an eligible bounded native still photo."""


def _still_size(size: object) -> tuple[int, int, int, str] | None:
    if type(size) not in (types.PhotoSize, types.PhotoSizeProgressive):
        return None
    photo_size = cast(types.PhotoSize, size)
    if type(size) is types.PhotoSize:
        length = photo_size.size
    else:
        progressive = cast(types.PhotoSizeProgressive, size)
        if not progressive.sizes:
            return None
        length = max(progressive.sizes)
    if not (0 < length <= PHOTO_MAX_BYTES and photo_size.w > 0 and photo_size.h > 0 and photo_size.type):
        return None
    return photo_size.w, photo_size.h, length, photo_size.type


def photo_locator_from_message(message: object) -> PhotoLocator:
    """Select the largest supported complete still rendition from a native photo."""
    media = getattr(message, "media", None)
    if type(media) is not types.MessageMediaPhoto:
        raise PhotoUnsupportedError("unsupported_photo")
    media = cast(types.MessageMediaPhoto, media)
    if any((media.ttl_seconds is not None, getattr(message, "ttl_period", None) is not None, media.live_photo)):
        raise PhotoUnsupportedError("unsupported_photo")
    photo = media.photo
    if type(photo) is not types.Photo:
        raise PhotoUnsupportedError("unsupported_photo")
    if photo.id == 0 or photo.dc_id <= 0 or not isinstance(photo.file_reference, bytes):
        raise PhotoUnsupportedError("invalid_photo_locator")
    choices = [candidate for item in photo.sizes if (candidate := _still_size(item)) is not None]
    if not choices:
        raise PhotoUnsupportedError("no_supported_photo_rendition")
    width, height, length, rendition = max(choices, key=lambda item: (item[0] * item[1], item[2]))
    location = types.InputPhotoFileLocation(
        id=photo.id, access_hash=photo.access_hash, file_reference=photo.file_reference, thumb_size=rendition
    )
    return PhotoLocator(photo.id, rendition, photo.dc_id, length, width, height, location)


class PhotoBudget(Protocol):
    async def reserve(self, *, setup: bool) -> None:
        """Durably spend one physical-attempt allowance before a claim can occur."""
        ...

    async def reserve_redirect(self, dc_id: int) -> None:
        """Durably bind the one server-directed migration to this attempt."""
        ...

    async def current_redirect(self) -> int | None:
        """Return the server-directed destination already bound to this attempt."""
        ...


class PhotoSourceToken(Protocol):
    def is_current(self) -> bool:
        """Synchronously revalidate the captured source at the physical claim."""
        ...


class PhotoSourceFence(Protocol):
    async def prepare(self) -> PhotoSourceToken:
        """Capture the verified account and current canonical source revision."""
        ...


class PhotoDownloadSession(Protocol):
    def chunks(self, *, offset: int) -> AsyncIterator[bytes]:
        """Read at most two approved chunks from a retained capability."""
        ...

    async def close(self) -> None:
        """Revoke the capability and drain the owned transport."""
        ...


class _PhotoSliceExhaustedError(Exception):
    """The pinned iterator reached this slice's physical dispatch limit."""


class _PhotoFileDispatch(Protocol):
    async def get_file(self, request: functions.upload.GetFileRequest) -> object: ...

    async def migrate(self, dc_id: int) -> None: ...


class _PhotoIteratorFacade(DownloadMethods):
    """Give the pinned iterator only an opaque, location-bound dispatch port."""

    def __init__(self, locator: PhotoLocator, dispatch: _PhotoFileDispatch, log: dict[str, Logger]) -> None:
        self._locator = locator
        self._location_bytes = bytes(cast(SupportsBytes, locator.location))
        self._dispatch = dispatch
        self._log = log
        self.session = SimpleNamespace(dc_id=locator.dc_id)
        self._sender = object()
        self._calls = 0
        self._migrated = False

    async def _call(self, sender: object, request: object, **_kwargs: object) -> object:
        if sender is not self._sender or type(request) is not functions.upload.GetFileRequest:
            raise RuntimeError("unsupported photo iterator request")
        self._validate_request(request)
        if self._calls >= PHOTO_MAX_SLICE_REQUESTS:
            raise _PhotoSliceExhaustedError
        self._calls += 1
        try:
            result = await self._dispatch.get_file(request)
        except errors.TimedOutError as exc:
            raise TimeoutError("photo request timed out") from exc
        if type(result) is types.upload.FileCdnRedirect or type(result) is not types.upload.File:
            raise PhotoUnsupportedError("unsupported_photo_redirect")
        return result

    def _validate_request(self, request: functions.upload.GetFileRequest) -> None:
        if (
            type(request.location) is not types.InputPhotoFileLocation
            or bytes(request.location) != self._location_bytes
        ):
            raise RuntimeError("photo iterator request exceeds its capability")
        if request.cdn_supported or request.precise or request.limit != PHOTO_CHUNK_BYTES:
            raise RuntimeError("photo iterator request exceeds its capability")
        if request.offset < 0 or request.offset % PHOTO_CHUNK_BYTES or request.offset >= self._locator.size:
            raise RuntimeError("photo iterator request exceeds its capability")

    async def _borrow_exported_sender(self, dc_id: int) -> object:
        if self._migrated or type(dc_id) is not int or dc_id <= 0:
            raise PhotoUnsupportedError("unsupported_photo_migration")
        self._migrated = True
        await self._dispatch.migrate(dc_id)
        return self._sender

    async def _return_exported_sender(self, sender: object) -> None:
        if sender is not self._sender:
            raise RuntimeError("unknown photo iterator capability")


def _validate_offset(offset: int, size: int) -> None:
    if type(offset) is not int or offset < 0 or offset % PHOTO_CHUNK_BYTES or offset >= size:
        raise ValueError("invalid photo download offset")


def _validate_locator(locator: PhotoLocator, offset: int) -> None:
    location = locator.location
    if type(location) is not types.InputPhotoFileLocation:
        raise ValueError("invalid photo locator")
    location = cast(types.InputPhotoFileLocation, location)
    if location.id != locator.photo_id or location.thumb_size != locator.rendition:
        raise ValueError("invalid photo locator")
    if type(locator.dc_id) is not int or locator.dc_id <= 0 or type(locator.size) is not int:
        raise ValueError("invalid photo locator")
    if not 0 < locator.size <= PHOTO_MAX_BYTES:
        raise ValueError("invalid photo locator")
    _validate_offset(offset, locator.size)


async def iter_photo_chunks(
    locator: PhotoLocator, *, offset: int, dispatch: _PhotoFileDispatch, log: dict[str, Logger]
) -> AsyncIterator[bytes]:
    """Run one two-request slice using Telethon's pinned iterator."""
    _validate_locator(locator, offset)
    facade = _PhotoIteratorFacade(locator, dispatch, log)
    iterator = cast("TelegramClient", cast(object, facade)).iter_download(
        cast(types.InputPhotoFileLocation, locator.location),
        offset=offset,
        limit=min(PHOTO_MAX_SLICE_REQUESTS, (locator.size - offset + PHOTO_CHUNK_BYTES - 1) // PHOTO_CHUNK_BYTES),
        chunk_size=PHOTO_CHUNK_BYTES,
        request_size=PHOTO_CHUNK_BYTES,
        file_size=locator.size,
        dc_id=locator.dc_id,
    )
    total = offset
    try:
        async for raw in iterator:
            chunk = bytes(raw)
            total += len(chunk)
            if total < locator.size and len(chunk) < PHOTO_CHUNK_BYTES:
                raise PhotoUnsupportedError("photo_size_mismatch")
            if total > locator.size or total > PHOTO_MAX_BYTES:
                raise PhotoUnsupportedError("photo_size_exceeded")
            yield chunk
            if facade._calls >= PHOTO_MAX_SLICE_REQUESTS:
                break
    except _PhotoSliceExhaustedError:
        pass
    finally:
        await iterator.close()


class TelegramImageGateway(Protocol):
    async def resolve_photo(
        self,
        target: PhotoTarget,
        *,
        scope: TelegramAuthScope,
        observation_order: int,
        budget: PhotoBudget,
        source_fence: PhotoSourceFence,
    ) -> PhotoObservation:
        """Resolve the exact source message for canonical application."""
        ...

    async def open_photo_download(
        self,
        locator: PhotoLocator,
        *,
        scope: TelegramAuthScope,
        work: PhotoWork,
        budget: PhotoBudget,
        source_fence: PhotoSourceFence,
    ) -> PhotoDownloadSession:
        """Open only the approved rendition through a bounded owned capability."""
        ...
