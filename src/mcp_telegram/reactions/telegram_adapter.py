"""Telethon implementation of the reaction gateway port."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Protocol, cast

from telethon.tl import types
from telethon.tl.functions.messages import GetMessageReactionsListRequest

from ..telegram_gateway import (
    CATCHABLE_GATEWAY_FAILURES,
    translate_reaction_detail_failure,
)
from ..telegram_rpc_error import describe_telegram_rpc_error
from ..telegram_rpc_scheduler import RpcAdmissionClosedError
from .contracts import (
    ReactionDetailFetchResult,
    ReactionDetailPage,
)
from .ports import TelegramReactionGateway
from .projection import project_reaction_event

logger = logging.getLogger(__name__)


class _TelegramClientLike(Protocol):
    async def get_input_entity(self, entity: object) -> object: ...

    def __call__(self, _request: object) -> Awaitable[object]: ...


class TelethonTelegramReactionGateway(TelegramReactionGateway):
    """Reaction adapter that inherits the caller's refresh RPC scope."""

    def __init__(self, client: object) -> None:
        self._client = cast(_TelegramClientLike, client)

    async def fetch_reaction_page(
        self, entity: object, message_id: int, *, offset: str | None, limit: int
    ) -> ReactionDetailFetchResult:
        """Fetch exactly one detail page; aggregate data is never re-read here."""
        stage = "resolve"
        try:
            resolve = getattr(self._client, "get_input_entity", None)
            peer = (
                await cast(Callable[[object], Awaitable[object]], resolve)(entity)
                if isinstance(entity, int) and callable(resolve)
                else entity
            )
            stage = "rpc"
            response = cast(
                types.messages.MessageReactionsList,
                await self._client(
                    GetMessageReactionsListRequest(
                        peer=cast(types.TypeInputPeer, peer), id=message_id, limit=limit, offset=offset
                    )
                ),
            )
            stage = "decode"
            next_raw = response.next_offset
            next_offset = None if next_raw is None else str(next_raw)
            events = tuple(project_reaction_event(item) for item in response.reactions)
            return ReactionDetailFetchResult(page=ReactionDetailPage(events, next_offset))
        except RpcAdmissionClosedError:
            raise
        except CATCHABLE_GATEWAY_FAILURES as exc:
            failure = translate_reaction_detail_failure(exc)
            descriptor = describe_telegram_rpc_error(exc)
            logger.log(
                logging.WARNING if failure.retryable or stage == "decode" else logging.INFO,
                "reaction_detail_fetch_failed stage=%s error_type=%s error_code=%s error_symbol=%s failure_kind=%s",
                stage,
                descriptor.error_type,
                descriptor.code,
                descriptor.symbol,
                failure.kind.value,
            )
            return ReactionDetailFetchResult(failure=failure)
