"""Application use case for refreshing a dialog's topic snapshot."""

from __future__ import annotations

from ..sqlite_writer import SQLiteWriter
from ..telegram_demand import AcquisitionKind
from ..telegram_rpc_scheduler import TelegramRpcSource, rpc_scope
from .contracts import is_topic_capable
from .ports import TelegramTopicGateway, TopicSnapshotRepository


class TopicRefresher:
    def __init__(
        self, gateway: TelegramTopicGateway, repository: TopicSnapshotRepository, *, writer: SQLiteWriter
    ) -> None:
        self._gateway = gateway
        self._repository = repository
        self._writer = writer

    async def refresh(self, dialog_id: int, entity: object) -> int:
        if not is_topic_capable(entity):
            return 0
        observation_order = await self._writer.run(lambda conn: self._repository.for_connection(conn).begin_snapshot())
        with rpc_scope(
            TelegramRpcSource.TOPIC_RECONCILIATION,
            acquisition_kind=AcquisitionKind.TOPIC_SNAPSHOT,
        ):
            topics = await self._gateway.fetch_topics(entity)
        await self._writer.run(
            lambda conn: self._repository.for_connection(conn).upsert_topics(
                dialog_id, topics, observation_order=observation_order
            )
        )
        return len(topics)
