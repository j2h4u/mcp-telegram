"""Application use case for refreshing a dialog's topic snapshot."""

from __future__ import annotations

from .contracts import is_topic_capable
from .ports import TelegramTopicGateway, TopicSnapshotRepository


class TopicRefresher:
    def __init__(self, gateway: TelegramTopicGateway, repository: TopicSnapshotRepository) -> None:
        self._gateway = gateway
        self._repository = repository

    async def refresh(self, dialog_id: int, entity: object) -> int:
        if not is_topic_capable(entity):
            return 0
        topics = await self._gateway.fetch_topics(entity)
        self._repository.upsert_topics(dialog_id, topics)
        return len(topics)
