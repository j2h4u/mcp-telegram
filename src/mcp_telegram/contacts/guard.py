"""Shared command serialization for one authenticated contact peer."""

import asyncio
from weakref import WeakValueDictionary


class ContactPeerGuard:
    """Hold the same lock for contact and note commands on a peer."""

    def __init__(self) -> None:
        self._locks: WeakValueDictionary[tuple[int, int], asyncio.Lock] = WeakValueDictionary()

    def lock(self, account_id: int, entity_id: int) -> asyncio.Lock:
        return self._locks.setdefault((account_id, entity_id), asyncio.Lock())
