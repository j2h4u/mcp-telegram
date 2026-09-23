"""Local peer and linked-chat fact lookup primitives.

Linked-chat acquisition is owned by the durable refresh worker. Activity
consumers only read the canonical fact and idempotently request cold demand.
"""

import logging
import sqlite3
from collections.abc import Coroutine
from typing import Protocol, cast

from telethon.tl.types import TypeInputPeer

from .activity_substrate import ActivityClient
from .flood import TelegramRpcThrottled, _raise_if_latched
from .linked_chat_fact import LinkedChatFact, LinkedChatState, linked_chat_fact_owner
from .telegram_demand import RpcAttemptBudgetExhaustedError
from .telegram_rpc_scheduler import RpcAdmissionClosedError

logger = logging.getLogger(__name__)


class _InputEntityResolverClient(Protocol):
    def get_input_entity(self, dialog_id: int) -> Coroutine[object, object, object]: ...


async def resolve_input_peer(client: _InputEntityResolverClient, dialog_id: int) -> TypeInputPeer | None:
    """Resolve a bare dialog ID through Telethon's local session cache."""
    try:
        return cast(TypeInputPeer, await client.get_input_entity(dialog_id))
    except RpcAttemptBudgetExhaustedError:
        raise
    except RpcAdmissionClosedError:
        raise
    except TelegramRpcThrottled as exc:
        _raise_if_latched(exc)
        logger.debug("activity_peer_resolve_input_peer_throttled dialog_id=%r", dialog_id)
        return None
    except Exception:
        logger.debug("activity_peer_resolve_input_peer_miss dialog_id=%r", dialog_id, exc_info=True)
        return None


async def resolve_linked_chat_id(
    client: ActivityClient,
    conn: sqlite3.Connection,
    channel_id: int,
    *,
    timeout_s: float | None = None,
) -> LinkedChatFact:
    """Return the canonical fact and enqueue cold demand when it is unknown.

    ``client`` and ``timeout_s`` remain in the call shape for existing callers;
    this path is local-only and never resolves an input peer or sends RPCs.
    """
    del client, timeout_s
    import time

    now = int(time.time())
    with conn:
        fact = linked_chat_fact_owner.read_fact(conn, channel_id)
        if fact.state is LinkedChatState.UNKNOWN:
            linked_chat_fact_owner.ensure_cold_demand(conn, channel_id, now)
            fact = linked_chat_fact_owner.read_fact(conn, channel_id)
    return fact


def linked_chat_retry_at(fact: LinkedChatFact, *, now: int) -> int:
    """Return the retry timestamp using the linked-chat owner's policy."""
    return linked_chat_fact_owner.effective_retry_at(fact.retry_at, fact.requested_at, now)
