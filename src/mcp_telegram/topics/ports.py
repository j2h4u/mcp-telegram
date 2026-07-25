"""Variable I/O boundaries used by topic refresh."""

from __future__ import annotations

from typing import Protocol

from .contracts import TopicFact


class TelegramTopicGateway(Protocol):
    async def fetch_topics(self, entity: object) -> tuple[TopicFact, ...]: ...


class TopicSnapshotRepository(Protocol):
    def upsert_topics(self, dialog_id: int, topics: tuple[TopicFact, ...]) -> None: ...
