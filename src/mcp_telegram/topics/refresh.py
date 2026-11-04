"""Application use case for refreshing a dialog's topic snapshot."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from ..entity_profile.repository import bound_profile_account
from ..image_authorization import ScopeToken
from ..messages.sqlite_bundle import persist_received_messages
from ..messages.telegram_adapter import extract_received_messages
from ..reactions.contracts import ReactionAggregateSource, ReactionObservationBoundary
from ..reactions.persistence import allocate_observation_boundary
from ..sqlite_writer import SQLiteWriter
from ..sync_transactions import write_transaction
from ..telegram_demand import AcquisitionKind
from ..telegram_rpc_scheduler import TelegramRpcSource, rpc_scope
from .catalog import TopicCatalogRepository
from .contracts import is_topic_capable
from .ports import TelegramTopicGateway, TopicSnapshotRepository


class TopicRefresher:
    def __init__(
        self,
        gateway: TelegramTopicGateway,
        repository: TopicSnapshotRepository,
        *,
        writer: SQLiteWriter,
        capture_scope: Callable[[], ScopeToken | None],
        claim_scope: Callable[[ScopeToken], bool],
    ) -> None:
        self._gateway = gateway
        self._repository = repository
        self._writer = writer
        self._capture_scope = capture_scope
        self._claim_scope = claim_scope

    async def refresh(self, dialog_id: int, entity: object) -> int:
        token = self._capture_scope()
        if not is_topic_capable(entity) or token is None or not self._claim_scope(token):
            return 0

        def reserve(conn: sqlite3.Connection) -> tuple[int, ReactionObservationBoundary]:
            with write_transaction(conn):
                order = self._repository.for_connection(conn).begin_snapshot()
                return order, allocate_observation_boundary(conn, ReactionAggregateSource.HISTORY)

        observation_order, reaction_boundary = await self._writer.run(reserve)
        with rpc_scope(
            TelegramRpcSource.TOPIC_RECONCILIATION,
            acquisition_kind=AcquisitionKind.TOPIC_SNAPSHOT,
        ):
            batch = await self._gateway.fetch_topics(entity)
        received = extract_received_messages(batch.messages)

        def publish(conn: sqlite3.Connection) -> int:
            with write_transaction(conn):
                if (
                    not TopicCatalogRepository(conn).acquisition_allowed(dialog_id)
                    or bound_profile_account(conn) != token.scope.account_id
                    or not self._claim_scope(token)
                ):
                    return 0
                self._repository.for_connection(conn).upsert_topics(
                    dialog_id, batch.topics, observation_order=observation_order
                )
                persist_received_messages(
                    conn,
                    received,
                    scope=token,
                    claim_scope=self._claim_scope,
                    observation_order=observation_order,
                    expected_dialog_id=dialog_id,
                    reaction_boundary=reaction_boundary,
                )
                return len(batch.topics)

        return await self._writer.run(publish)
