"""One-RPC folder rule acquisition and local canonical projection."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, replace

from ..auth_scope import TelegramAuthScope
from ..image_authorization import ScopeToken
from ..observation_order import allocate_observation_order
from ..sqlite_writer import SQLiteWriter
from ..sync_transactions import write_transaction
from ..telegram_demand import AcquisitionKind
from ..telegram_rpc_scheduler import TelegramRpcSource, rpc_scope
from .contracts import FolderRuleObservation
from .intake import marker
from .ports import FolderSnapshotRepository, TelegramFolderGateway


class _PublicationScopeRevokedError(Exception):
    """Roll back publication when the original account lease is revoked."""


@dataclass(frozen=True, slots=True)
class FolderRefreshResult:
    dialog_count: int
    generation: int | None
    token: str
    started_at: int
    committed_revision: str | None = None
    accepted: bool = True
    observation_order: int | None = None
    publication_revision: int | None = None


class FolderRefresher:
    """Observe rules once, then project entirely from the published catalog."""

    def __init__(  # noqa: PLR0913 - explicit durable writer and scope fence
        self,
        gateway: TelegramFolderGateway,
        repository: FolderSnapshotRepository,
        *,
        writer: SQLiteWriter,
        auth_scope: Callable[[], TelegramAuthScope | None] | None = None,
        capture_scope: Callable[[], ScopeToken | None] | None = None,
        claim_scope: Callable[[ScopeToken], bool] | None = None,
    ) -> None:
        self._gateway = gateway
        self._repository = repository
        self._writer = writer
        self._auth_scope = auth_scope
        if (capture_scope is None) != (claim_scope is None):
            raise ValueError("folder scope capture and claim must be configured together")
        self._capture_scope = capture_scope
        self._claim_scope = claim_scope

    async def acquire(self, *, started_at: int | None = None) -> FolderRuleObservation:
        observation_started_at = int(time.time()) if started_at is None else started_at
        with rpc_scope(TelegramRpcSource.FOLDER_RECONCILIATION, acquisition_kind=AcquisitionKind.FOLDER_SNAPSHOT):
            return await self._gateway.fetch_rules(started_at=observation_started_at)

    async def refresh(
        self, *, completed_at: int | None = None, expected_revision: str | None = None
    ) -> FolderRefreshResult:
        completion = int(time.time()) if completed_at is None else completed_at
        intake_marker = marker()
        initial_membership_revision = self._repository.read_membership_revision()
        scope = None if self._auth_scope is None else self._auth_scope()
        scope_token = None if self._capture_scope is None else self._capture_scope()
        observation_order = await self._writer.run(allocate_observation_order)
        observation = await self.acquire(started_at=completion)

        def publish(conn: sqlite3.Connection) -> FolderRefreshResult:
            with write_transaction(conn):
                repository = self._repository.for_connection(conn)
                current_revision = repository.read_snapshot_revision()
                current_catalog = repository.read_catalog_identity()
                expected_matches = (
                    expected_revision is None or repr((current_revision, current_catalog)) == expected_revision
                )
                scope_claimed = self._claim_observation_scope(scope, scope_token)
                if not scope_claimed:
                    return FolderRefreshResult(0, None, observation.token, observation.started_at, accepted=False)
                generation = repository.project_observation(
                    observation,
                    completed_at=completion,
                    observation_order=observation_order,
                    membership_revision=initial_membership_revision
                    if marker() == intake_marker and expected_matches
                    else -1,
                    owner_account_id=None if scope is None else scope.account_id,
                )
                if generation is not None:
                    assert current_catalog is not None
                    self._record_enumeration_receipt(
                        conn,
                        repository,
                        observation,
                        account_id=current_catalog[0],
                        order=observation_order,
                        membership_revision=initial_membership_revision,
                        intake_marker=intake_marker,
                        expected_matches=expected_matches,
                    )
                if not self._claim_observation_scope(scope, scope_token):
                    raise _PublicationScopeRevokedError
                return self._publication_result(
                    repository,
                    observation,
                    generation=generation,
                    order=observation_order,
                    expected_matches=expected_matches,
                    directory_missing=current_catalog is None,
                )

        result = await self._publish_scoped(publish, observation)
        if result.accepted and marker() != intake_marker:
            await self._writer.run(
                lambda conn: self._invalidate_raced_publication(
                    conn, scope, scope_token, result.committed_revision, result.publication_revision
                )
            )
            return replace(result, accepted=False, committed_revision=None, observation_order=None)
        if (
            result.accepted
            and self._claim_scope is not None
            and (scope_token is None or not self._claim_scope(scope_token))
        ):
            return replace(result, accepted=False, committed_revision=None, observation_order=None)
        return result

    @staticmethod
    def _publication_result(  # noqa: PLR0913 - result combines independent publication evidence
        repository: FolderSnapshotRepository,
        observation: FolderRuleObservation,
        *,
        generation: int | None,
        order: int,
        expected_matches: bool,
        directory_missing: bool,
    ) -> FolderRefreshResult:
        revision = (
            None
            if generation is None
            else repr((repository.read_snapshot_revision(), repository.read_catalog_identity()))
        )
        accepted = expected_matches and (generation is not None or directory_missing)
        return FolderRefreshResult(
            0,
            generation,
            observation.token,
            observation.started_at,
            revision if accepted else None,
            accepted=accepted,
            observation_order=order if generation is not None and accepted else None,
            publication_revision=repository.read_catalog_version()[1] if generation is not None else None,
        )

    @staticmethod
    def _record_enumeration_receipt(  # noqa: PLR0913 - independent observation and receipt fences
        conn: sqlite3.Connection,
        repository: FolderSnapshotRepository,
        observation: FolderRuleObservation,
        *,
        account_id: int,
        order: int,
        membership_revision: int,
        intake_marker: int,
        expected_matches: bool,
    ) -> None:
        conn.execute(
            "INSERT INTO daemon_state(key,value) VALUES ('folder_observation_order',?) "
            "ON CONFLICT(key) DO UPDATE SET value=MAX(CAST(value AS INTEGER),CAST(excluded.value AS INTEGER))",
            (str(order),),
        )
        if (
            marker() == intake_marker
            and expected_matches
            and repository.read_membership_revision() == membership_revision
            and repository.read_catalog_version()[3]
        ):
            repository.refine_receipts(observation, account_id=account_id, order=order)

    async def _publish_scoped(
        self, publish: Callable[[sqlite3.Connection], FolderRefreshResult], observation: FolderRuleObservation
    ) -> FolderRefreshResult:
        try:
            return await self._writer.run(publish)
        except _PublicationScopeRevokedError:
            return FolderRefreshResult(0, None, observation.token, observation.started_at, accepted=False)

    def _invalidate_raced_publication(
        self,
        conn: sqlite3.Connection,
        scope: TelegramAuthScope | None,
        token: ScopeToken | None,
        revision: str | None,
        publication_revision: int | None,
    ) -> None:
        if revision is None:
            return
        with write_transaction(conn):
            repository = self._repository.for_connection(conn)
            current = repr((repository.read_snapshot_revision(), repository.read_catalog_identity()))
            if (
                current == revision
                and repository.read_catalog_version()[1] == publication_revision
                and self._claim_observation_scope(scope, token)
            ):
                repository.invalidate()

    def _claim_observation_scope(self, scope: TelegramAuthScope | None, scope_token: ScopeToken | None) -> bool:
        valid_scope = self._auth_scope is None or (scope is not None and self._auth_scope() == scope)
        scope_claimed = self._claim_scope is None or (scope_token is not None and self._claim_scope(scope_token))
        return all(
            (
                scope_claimed,
                scope_token is None or scope_token.scope == scope,
                valid_scope,
            )
        )

    async def reproject_current_rules(self, *, now: int) -> int | None:
        return await self._writer.run(
            lambda conn: self._repository.for_connection(conn).reproject_current_rules(now=now)
        )
