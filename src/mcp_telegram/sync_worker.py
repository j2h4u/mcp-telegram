"""FullSyncWorker — bulk history fetch engine for v1.5 Persistent Sync.

Fetches all historical messages for marked dialogs in batches of 100,
checkpointing progress after each batch so restarts resume without
re-scanning from scratch.

FloodWait causes an interruptible sleep — progress is never lost on
rate limits.

DM enrollment consumes completed canonical dialog publications locally.

Architecture:
- Standalone module so daemon.py stays focused on process lifecycle.
- FullSyncWorker is a stateful class instantiated once per daemon run.
- Plugs into daemon.py sync_main() between heartbeat ticks.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from functools import wraps
from typing import Protocol, cast

from .access_lifecycle import AccessLossResult, set_access_lost
from .dialog_identity import ensure_dialog_identity_baseline, publish_dialog_identity
from .entity_profile.membership import MembershipSignalCounts
from .entity_profile.repository import bound_profile_account
from .entity_store import EntitySnapshot, upsert_entity_stub
from .flood import TelegramRpcThrottled, _raise_if_latched
from .history_enrollment import (
    ensure_automatic_dm_enrollment,
    full_history_enabled,
    read_intent,
    record_automatic_group_decision,
)
from .hydration_queue import HydrationPriority
from .image_authorization import ScopeToken
from .message_contracts import ExtractedMessage as _ExtractedMessage
from .message_history.contracts import (
    MESSAGE_HISTORY_PAGE_SIZE,
    TOPIC_ATTRIBUTION_EXTRACTOR_VERSION,
    HistoryAccessObservation,
    HistoryAccessProbeResult,
    MessageHistoryAccessLostError,
    MessageHistoryUnavailableError,
)
from .message_history.ports import FullHistoryPagePort
from .messages.sqlite_bundle import (
    ReceivedMessageScopeChangedError,
    insert_messages_with_fts,
    persist_received_messages,
)
from .observation_order import allocate_observation_order
from .reactions.contracts import ReactionAggregateSource, ReactionObservationBoundary
from .reactions.persistence import allocate_observation_boundary
from .read_state import apply_read_cursor
from .resolver import latinize
from .sqlite_writer import SQLiteWriter
from .sync_transactions import require_write_transaction, write_transaction
from .telegram_demand import (
    AcquisitionKind,
    DemandStatus,
    RpcAttemptBudget,
    RpcAttemptBudgetExhaustedError,
    UnclassifiedTelegramDemandError,
    acquisition_context,
    current_demand_token,
    demand_context,
    durable_demand_context,
)
from .telegram_demand_progress import SliceOutcome, SliceOutcomeKind, WorkKey, WorkRevision
from .telegram_rpc_consumers import DemandKind
from .telegram_rpc_scheduler import (
    RpcAdmissionClosedError,
    RpcAdmissionExpiredError,
    RpcAdmissionSaturatedError,
    TelegramRpcAdmissionDeferred,
    TelegramRpcSource,
    rpc_attempt_budget,
    rpc_scope,
)

logger = logging.getLogger(__name__)

_BATCH_SIZE = MESSAGE_HISTORY_PAGE_SIZE
_DM_ENROLLMENT_KEY_LAST_PUBLICATION_GENERATION = "full_sync_dm_enrollment_last_publication_generation"
_TOTAL_MESSAGES_REPAIR_GATE_STATE_KEY = "full_sync_total_messages_repair_retry_at"
_TOTAL_MESSAGES_REPAIR_FAILURE_DELAY_S = 60


class AutomaticGroupHistoryPolicy(Protocol):
    @property
    def max_members(self) -> int: ...
    @property
    def recent_days(self) -> int: ...
    @property
    def max_messages(self) -> int: ...
    @property
    def probe_retry_seconds(self) -> int: ...


@contextmanager
def _full_sync_demand_scope(kind: DemandKind, acquisition_kind: AcquisitionKind) -> Iterator[None]:
    """Install a precise root for direct calls while preserving an adapter root."""
    try:
        token = current_demand_token()
    except UnclassifiedTelegramDemandError:
        with demand_context(kind):
            with acquisition_context(acquisition_kind):
                yield
    else:
        if token.kind is not kind:
            raise RuntimeError(f"active demand kind {token.kind.value} cannot execute {kind.value}")
        with acquisition_context(acquisition_kind):
            yield


@contextmanager
def _full_sync_root_scope(kind: DemandKind) -> Iterator[None]:
    """Reuse the coordinator's bounded root or create one for direct calls."""
    with durable_demand_context(kind):
        yield


def _full_sync_rpc_scope[**P, R](
    kind: DemandKind,
    acquisition_kind: AcquisitionKind,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Give full/history synchronization precise demand identity."""

    def decorate(func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @wraps(func)
        async def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
            with _full_sync_demand_scope(kind, acquisition_kind):
                with rpc_scope(TelegramRpcSource.FULL_SYNC):
                    return await func(*args, **kwargs)

        return wrapped

    return decorate


_NEXT_PENDING_SQL = (
    "SELECT sd.dialog_id, sd.sync_progress FROM synced_dialogs sd "
    "JOIN full_history_enrollment fhe ON fhe.dialog_id = sd.dialog_id AND fhe.enabled = 1 "
    "WHERE sd.status IN ('syncing', 'not_synced') "
    "AND NOT EXISTS (SELECT 1 FROM dialogs directory WHERE directory.dialog_id=sd.dialog_id AND directory.hidden=1) "
    "AND COALESCE(sd.full_sync_retry_at,0) <= ? "
    "ORDER BY CASE WHEN sd.dialog_id > COALESCE((SELECT CAST(value AS INTEGER) FROM daemon_state "
    "WHERE key='full_sync_peer_cursor'),0) THEN 0 ELSE 1 END, sd.dialog_id LIMIT 1"
)
_NEXT_TOTAL_MESSAGES_REPAIR_SQL = (
    "SELECT sd.dialog_id FROM synced_dialogs sd "
    "JOIN full_history_enrollment fhe ON fhe.dialog_id = sd.dialog_id AND fhe.enabled = 1 "
    "WHERE sd.total_messages IS NULL AND sd.status NOT IN ('not_synced', 'access_lost') "
    "AND NOT EXISTS (SELECT 1 FROM dialogs directory WHERE directory.dialog_id=sd.dialog_id AND directory.hidden=1) "
    "AND COALESCE(sd.full_sync_retry_at,0) <= ? "
    "ORDER BY CASE WHEN sd.dialog_id > COALESCE((SELECT CAST(value AS INTEGER) FROM daemon_state "
    "WHERE key='full_sync_total_peer_cursor'),0) THEN 0 ELSE 1 END, sd.dialog_id LIMIT 1"
)
_UPDATE_TOTAL_MESSAGES_SQL = (
    "UPDATE synced_dialogs SET total_messages = ? WHERE dialog_id = ? AND total_messages IS NULL "
    "AND status NOT IN ('not_synced', ?) "
    "AND EXISTS (SELECT 1 FROM full_history_enrollment WHERE dialog_id = ? AND enabled = 1)"
)
_UPDATE_PROGRESS_SQL = (
    "UPDATE synced_dialogs SET sync_progress = ?, status = ?, "
    "total_messages = COALESCE(?, total_messages) WHERE dialog_id = ? "
    "AND COALESCE(sync_progress,0)=? "
    "AND EXISTS (SELECT 1 FROM full_history_enrollment WHERE dialog_id = ? AND enabled = 1)"
)
_UPDATE_PROGRESS_DONE_SQL = (
    "UPDATE synced_dialogs SET sync_progress = ?, status = ?, total_messages = COALESCE(?, total_messages), "
    "last_synced_at = ? WHERE dialog_id = ? "
    "AND EXISTS (SELECT 1 FROM full_history_enrollment WHERE dialog_id = ? AND enabled = 1) "
    "AND COALESCE(sync_progress,0)=?"
)


class TotalMessagesProbe(Protocol):
    """Single-message access probe used by total repair, outside page ports."""

    def capture_scope(self) -> ScopeToken | None: ...

    def claim_scope(self, token: ScopeToken) -> bool: ...

    async def probe_total_messages(self, dialog_id: int) -> HistoryAccessProbeResult: ...


@dataclass(frozen=True, slots=True)
class _FetchedBatchPage:
    total_messages: int | None
    batch: tuple[_ExtractedMessage, ...]
    next_before_message_id: int | None
    retry: tuple[int, bool] | None = None
    reaction_observed_at: int | None = None


def _dm_enrollment_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = cast(
        tuple[str | None] | None, conn.execute("SELECT value FROM daemon_state WHERE key = ?", (key,)).fetchone()
    )
    return None if row is None else row[0]


def _set_dm_enrollment_state(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    require_write_transaction(conn)
    conn.execute("INSERT OR REPLACE INTO daemon_state(key, value) VALUES (?, ?)", (key, value))


def _total_messages_repair_retry_at(conn: sqlite3.Connection) -> int | None:
    value = _dm_enrollment_state(conn, _TOTAL_MESSAGES_REPAIR_GATE_STATE_KEY)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        logger.warning("sync_total_repair_retry_corrupt value=%r", value)
        return None


def _set_total_messages_repair_retry(conn: sqlite3.Connection, retry_at: int | None) -> None:
    require_write_transaction(conn)
    _set_dm_enrollment_state(
        conn,
        _TOTAL_MESSAGES_REPAIR_GATE_STATE_KEY,
        None if retry_at is None else str(retry_at),
    )


# ---------------------------------------------------------------------------
# FullSyncWorker
# ---------------------------------------------------------------------------


_FULL_MEMBERSHIP_OBSERVER: ContextVar[Callable[[MembershipSignalCounts], None] | None] = ContextVar(
    "full_membership_observer", default=None
)


_FULL_ACCESS_LOSS_OBSERVER: ContextVar[Callable[[MembershipSignalCounts], None] | None] = ContextVar(
    "full_access_loss_observer", default=None
)


def _report_membership(
    observer: Callable[[MembershipSignalCounts], None] | None, counts: list[MembershipSignalCounts]
) -> None:
    if observer is None:
        return
    for item in counts:
        try:
            observer(item)
        except Exception:  # noqa: BLE001 - observability must not affect committed work
            logger.debug("membership_observation_failed")


class FullSyncWorker:
    """Core bulk-fetch engine for the v1.5 sync daemon.

    Fetches historical Telegram messages in batches and stores them in
    sync.db.  One instance is created per daemon run; it is called
    between heartbeat ticks in sync_main().

    Args:
        history_port: Transport-neutral backward history page port.
        conn: Read-only SQLite connection to sync.db.
        writer: Dedicated owner of all runtime writes.
        shutdown_event: asyncio.Event set when SIGTERM is received.
            Used to make FloodWait sleeps interruptible.
        total_messages_probe: Narrow access/total probe kept outside page ports.
    """

    def __init__(  # noqa: PLR0913
        self,
        history_port: FullHistoryPagePort,
        conn: sqlite3.Connection,
        shutdown_event: asyncio.Event,
        *,
        writer: SQLiteWriter,
        total_messages_probe: TotalMessagesProbe | None = None,
        automatic_group_history: AutomaticGroupHistoryPolicy | None = None,
    ) -> None:
        self._history_port = history_port
        self._total_messages_probe = total_messages_probe
        self._automatic_group_history = automatic_group_history
        self._conn = conn
        self._writer = writer
        self._shutdown_event = shutdown_event
        self._last_page_error: BaseException | None = None
        self._last_total_repair_error: BaseException | None = None
        self._last_page_accepted = False
        self._last_page_stale = False
        self._last_automatic_pending = False
        self._last_total_accepted = False
        self._last_total_stale = False
        self._last_page_committed_revision: str | None = None
        self._last_total_committed_revision: str | None = None

    def _published_dm_generation(self, conn: sqlite3.Connection) -> int | None:
        row = cast(
            tuple[int] | None,
            conn.execute(
                "SELECT generation FROM dialog_directory_publication WHERE singleton = 1 AND generation IS NOT NULL"
            ).fetchone(),
        )
        return None if row is None else int(row[0])

    def _last_consumed_dm_generation(self, conn: sqlite3.Connection) -> int:
        value = _dm_enrollment_state(conn, _DM_ENROLLMENT_KEY_LAST_PUBLICATION_GENERATION)
        if value is None:
            return 0
        try:
            return int(value)
        except ValueError:
            logger.warning("dm_publication_consumed_generation_corrupt value=%r", value)
            return 0

    def _upsert_local_entity_stub(
        self, conn: sqlite3.Connection, dialog_id: int, dialog_type: str, name: str | None, now: int
    ) -> None:
        """Create a local identity row while retaining richer existing fields."""
        require_write_transaction(conn)
        upsert_entity_stub(
            conn,
            EntitySnapshot(
                entity_id=dialog_id,
                entity_type=dialog_type,
                name=name,
                username=None,
                name_normalized=latinize(name) if name else None,
                updated_at=now,
            ),
        )

    def _consume_one_canonical_dm(self, conn: sqlite3.Connection, row: tuple[object, ...], now: int) -> int:
        require_write_transaction(conn)
        dialog_id = int(cast(int, row[0]))
        dialog_type = str(row[1])
        name = cast(str | None, row[2])
        inbox = cast(int | None, row[3])
        outbox = cast(int | None, row[4])
        outcome = ensure_automatic_dm_enrollment(conn, dialog_id, now=now)
        self._upsert_local_entity_stub(conn, dialog_id, dialog_type, name, now)
        if inbox is not None:
            apply_read_cursor(conn, dialog_id, "inbox", inbox)
        if outbox is not None:
            apply_read_cursor(conn, dialog_id, "outbox", outbox)
        return int(outcome.action == "queue_full_history")

    def consume_canonical_dm_publication(self, conn: sqlite3.Connection) -> int:
        """Consume one completed canonical dialog publication entirely locally."""
        result = self._consume_dm_publication(conn, expected_generation=None)
        return 0 if result is None else result[0]

    def _consume_dm_publication(
        self, conn: sqlite3.Connection, *, expected_generation: int | None
    ) -> tuple[int, int] | None:
        with write_transaction(conn):
            generation = self._published_dm_generation(conn)
            if expected_generation is not None and generation != expected_generation:
                return None
            if generation is None or generation <= self._last_consumed_dm_generation(conn):
                return None
            rows = cast(
                list[tuple[object, ...]],
                conn.execute(
                    "SELECT dialog_id,type,name,read_inbox_max_id,read_outbox_max_id FROM dialogs "
                    "WHERE type IN ('user','bot') AND hidden=0 ORDER BY dialog_id"
                ).fetchall(),
            )
            now = int(time.time())
            enrolled = sum(self._consume_one_canonical_dm(conn, row, now) for row in rows)
            _set_dm_enrollment_state(
                conn,
                _DM_ENROLLMENT_KEY_LAST_PUBLICATION_GENERATION,
                str(generation),
            )
        logger.info("dm_publication_consumed generation=%d enrolled=%d", generation, enrolled)
        return enrolled, generation

    @_full_sync_rpc_scope(DemandKind.FULL_SYNC_PAGE, AcquisitionKind.MESSAGE_HISTORY_PAGE)
    async def process_one_batch(
        self,
        *,
        expected_dialog: int | None = None,
        expected_cursor: int | None = None,
        profile_owner_account_id: int | None = None,
    ) -> bool:
        """Fetch one batch of messages for the next pending dialog.

        Picks the first dialog with status in ('syncing', 'not_synced'),
        fetches up to 100 messages from where it left off, stores them,
        and updates sync_progress atomically.

        Returns:
            True  — all dialogs are fully synced (idle mode safe).
            False — more work remains (same dialog or other pending dialogs).
        """
        self._last_page_accepted = False
        self._last_page_stale = False
        self._last_page_committed_revision = None
        if expected_dialog is None:
            pending = self._next_pending_dialog()
        else:
            row = cast(
                tuple[int, int] | None,
                self._conn.execute(
                    "SELECT sd.dialog_id,COALESCE(sd.sync_progress,0) FROM synced_dialogs sd "
                    "JOIN full_history_enrollment fhe ON fhe.dialog_id=sd.dialog_id AND fhe.enabled=1 "
                    "WHERE sd.dialog_id=? AND sd.status IN ('syncing','not_synced') "
                    "AND COALESCE(sd.full_sync_retry_at,0)<=? "
                    "AND NOT EXISTS (SELECT 1 FROM dialogs d WHERE d.dialog_id=sd.dialog_id AND d.hidden=1)",
                    (expected_dialog, time.time()),
                ).fetchone(),
            )
            pending = None if row is None else (int(row[0]), int(row[1]))
        if expected_dialog is not None and pending != (expected_dialog, expected_cursor):
            self._last_page_stale = True
            return False
        if pending is None:
            return not await self._process_one_automatic_group(profile_owner_account_id=profile_owner_account_id)

        dialog_id, sync_progress = pending

        def persist_peer_cursor(conn: sqlite3.Connection) -> None:
            with write_transaction(conn):
                _set_dm_enrollment_state(conn, "full_sync_peer_cursor", str(dialog_id))

        await self._writer.run(persist_peer_cursor)
        _, is_done = await self._fetch_batch(
            dialog_id, sync_progress, profile_owner_account_id=profile_owner_account_id
        )

        if not is_done:
            return False  # more batches needed for this dialog
        # Dialog done — check if more pending dialogs remain
        return self._next_pending_dialog() is None

    @_full_sync_rpc_scope(DemandKind.FULL_SYNC_PAGE, AcquisitionKind.MESSAGE_LOOKUP)
    async def repair_one_total_messages(
        self, *, expected_dialog_id: int | None = None, profile_owner_account_id: int | None = None
    ) -> bool:
        """Repair one missing Telegram history total under a single RPC budget."""
        self._last_total_repair_error = None
        self._last_total_accepted = False
        self._last_total_committed_revision = None
        self._last_total_stale = False
        dialog_id = expected_dialog_id if expected_dialog_id is not None else self._next_total_messages_repair_dialog()
        if dialog_id is None:
            return True
        if expected_dialog_id is not None and not self._expected_total_repair_is_ready(dialog_id):
            self._last_total_stale = True
            return True
        if self._total_messages_probe is None:
            error = RuntimeError("total-message repair requires an explicit access probe")
            self._last_total_repair_error = error
            return False

        def persist_total_peer_cursor(conn: sqlite3.Connection) -> None:
            with write_transaction(conn):
                _set_dm_enrollment_state(conn, "full_sync_total_peer_cursor", str(dialog_id))

        await self._writer.run(persist_total_peer_cursor)
        if not self._expected_total_repair_is_ready(dialog_id):
            self._last_total_stale = True
            return True
        observation = await self._probe_total_messages_for_repair(
            dialog_id, profile_owner_account_id, self._total_messages_probe
        )
        if observation is None:
            return False
        return await self._publish_total_probe(dialog_id, observation)

    async def _publish_total_probe(self, dialog_id: int, observation: HistoryAccessObservation) -> bool:
        total_messages = observation.result.total_messages

        def persist_total(conn: sqlite3.Connection) -> None:
            with write_transaction(conn):
                probe = self._total_messages_probe
                assert probe is not None
                if bound_profile_account(conn) != observation.scope.scope.account_id:
                    self._last_total_stale = True
                    return
                persist_received_messages(
                    conn,
                    observation.result.received,
                    scope=observation.scope,
                    claim_scope=probe.claim_scope,
                    observation_order=observation.order,
                    expected_dialog_id=dialog_id,
                    reaction_boundary=observation.reactions,
                )
                if total_messages is None:
                    return
                cursor = conn.execute(
                    _UPDATE_TOTAL_MESSAGES_SQL,
                    (total_messages, dialog_id, "access_lost", dialog_id),
                )
                self._last_total_accepted = cursor.rowcount == 1
                if self._last_total_accepted:
                    row = cast(
                        tuple[int | None] | None,
                        conn.execute(
                            "SELECT total_messages FROM synced_dialogs WHERE dialog_id=?", (dialog_id,)
                        ).fetchone(),
                    )
                    if row is not None and row[0] is not None:
                        self._last_total_committed_revision = f"resolved:{int(row[0])}"
                    _set_total_messages_repair_retry(conn, None)
                    conn.execute("UPDATE synced_dialogs SET full_sync_retry_at=NULL WHERE dialog_id=?", (dialog_id,))

        try:
            await self._writer.run(persist_total)
        except ReceivedMessageScopeChangedError:
            self._last_total_stale = True
            return False
        if total_messages is None or self._last_total_stale:
            return False
        logger.info("sync_total_repair_complete dialog_id=%d total_messages=%d", dialog_id, total_messages)
        return self._next_total_messages_repair_dialog() is None

    async def _probe_total_messages_for_repair(
        self, dialog_id: int, profile_owner_account_id: int | None, probe: TotalMessagesProbe
    ) -> HistoryAccessObservation | None:

        scope = self._capture_total_probe_scope(probe, profile_owner_account_id)
        if scope is None:
            return None

        def prepare(conn: sqlite3.Connection) -> tuple[int, ReactionObservationBoundary]:
            with write_transaction(conn):
                return allocate_observation_order(conn), allocate_observation_boundary(
                    conn, ReactionAggregateSource.HISTORY
                )

        order, reactions = await self._writer.run(prepare)
        try:
            result = await probe.probe_total_messages(dialog_id)
        except RpcAttemptBudgetExhaustedError:
            raise
        except RpcAdmissionClosedError:
            raise
        except (TelegramRpcAdmissionDeferred, RpcAdmissionSaturatedError, RpcAdmissionExpiredError) as exc:
            logger.info(
                "sync_total_repair admission_deferred dialog_id=%d error_type=%s",
                dialog_id,
                type(exc).__name__,
            )
            self._last_total_repair_error = exc
            return None
        except TelegramRpcThrottled as exc:
            _raise_if_latched(exc)
            logger.warning(
                "sync_total_repair flood_wait dialog_id=%d seconds=%s",
                dialog_id,
                exc.retry_after_seconds,
            )
            self._last_total_repair_error = exc
            return None
        except MessageHistoryAccessLostError as exc:
            return await self._record_total_probe_loss(dialog_id, profile_owner_account_id, order, scope, exc)
        except (MessageHistoryUnavailableError, TimeoutError) as exc:
            logger.warning("sync_total_repair_failed dialog_id=%d error=%s", dialog_id, exc)
            self._last_total_repair_error = exc
            return None
        if result.total_messages is None:
            logger.warning("sync_total_repair_missing_total dialog_id=%d", dialog_id)
        return HistoryAccessObservation(result, scope, order, reactions)

    async def _record_total_probe_loss(
        self, dialog_id: int, owner: int | None, order: int, scope: ScopeToken, error: MessageHistoryAccessLostError
    ) -> HistoryAccessObservation | None:
        observed_at = int(time.time())
        try:
            result = await self._writer.run(
                lambda conn: self._persist_access_loss(
                    conn, dialog_id, error.reason_code, owner, order, observed_at, scope=scope
                )
            )
        except ReceivedMessageScopeChangedError:
            self._last_total_stale = True
            return None
        _report_membership(_FULL_ACCESS_LOSS_OBSERVER.get(), [result.membership_counts])
        return None

    def _capture_total_probe_scope(self, probe: TotalMessagesProbe, owner: int | None) -> ScopeToken | None:
        scope = probe.capture_scope()
        if scope is None or (owner is not None and scope.scope.account_id != owner):
            self._last_total_stale = True
            return None
        return scope

    def _persist_access_loss(  # noqa: PLR0913, PLR0917
        self,
        conn: sqlite3.Connection,
        dialog_id: int,
        reason: str,
        owner: int | None,
        order: int,
        observed_at: int,
        *,
        scope: ScopeToken | None = None,
    ) -> AccessLossResult:
        with write_transaction(conn):
            probe = self._total_messages_probe
            if scope is not None and (
                probe is None or not probe.claim_scope(scope) or bound_profile_account(conn) != scope.scope.account_id
            ):
                raise ReceivedMessageScopeChangedError
            result = set_access_lost(
                conn,
                dialog_id,
                observed_at,
                reason=reason,
                profile_owner_account_id=owner,
                observation_order=order,
                membership_revision_at=observed_at,
                processing_at=int(time.time()),
            )
            if scope is not None and (
                probe is None or not probe.claim_scope(scope) or bound_profile_account(conn) != scope.scope.account_id
            ):
                raise ReceivedMessageScopeChangedError
            return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _next_pending_dialog(self) -> tuple[int, int] | None:
        """Return (dialog_id, sync_progress) for the next pending dialog.

        Rotates eligible peers after the durable last-attempted peer.
        Returns None when no dialogs have status in ('syncing', 'not_synced').
        """
        row = cast(tuple[int, int | None] | None, self._conn.execute(_NEXT_PENDING_SQL, (time.time(),)).fetchone())
        if row is None:
            return None
        return int(row[0]), int(row[1]) if row[1] is not None else 0

    def _next_automatic_group(self) -> int | None:
        if self._automatic_group_history is None:
            return None
        cutoff = int(time.time()) - self._automatic_group_history.recent_days * 86_400
        return self._eligible_automatic_group(cutoff=cutoff)

    def _set_automatic_group_retry(self, conn: sqlite3.Connection, dialog_id: int) -> None:
        require_write_transaction(conn)
        assert self._automatic_group_history is not None
        conn.execute(
            "INSERT OR REPLACE INTO daemon_state(key,value) VALUES (?,?)",
            (
                f"automatic_group_history_retry:{dialog_id}",
                str(int(time.time()) + self._automatic_group_history.probe_retry_seconds),
            ),
        )

    def _automatic_group_retry_at(self, dialog_id: int) -> int:
        row = cast(
            tuple[int] | None,
            self._conn.execute(
                "SELECT CAST(value AS INTEGER) FROM daemon_state WHERE key=?",
                (f"automatic_group_history_retry:{dialog_id}",),
            ).fetchone(),
        )
        return 0 if row is None else int(row[0])

    def _eligible_automatic_group(self, *, cutoff: int) -> int | None:
        policy = self._automatic_group_history
        if policy is None:
            return None
        row = cast(
            tuple[int] | None,
            self._conn.execute(
                "SELECT d.dialog_id FROM dialogs d "
                "LEFT JOIN full_history_enrollment fhe ON fhe.dialog_id=d.dialog_id "
                "LEFT JOIN synced_dialogs sd ON sd.dialog_id=d.dialog_id "
                "WHERE fhe.dialog_id IS NULL AND d.hidden=0 AND d.type IN ('group','supergroup','forum') "
                "AND d.members BETWEEN 1 AND ? AND d.created >= ? "
                "AND (sd.status IS NULL OR sd.status NOT IN ('access_lost', 'synced')) "
                "AND NOT EXISTS (SELECT 1 FROM daemon_state ds WHERE ds.key='automatic_group_history_retry:' || d.dialog_id AND CAST(ds.value AS INTEGER) > ?) "
                "ORDER BY d.dialog_id LIMIT 1",
                (policy.max_members, cutoff, int(time.time())),
            ).fetchone(),
        )
        return None if row is None else int(row[0])

    def _is_eligible_automatic_group(self, conn: sqlite3.Connection, dialog_id: int, *, cutoff: int) -> bool:
        policy = self._automatic_group_history
        if policy is None:
            return False
        return (
            conn.execute(
                "SELECT 1 FROM dialogs d LEFT JOIN full_history_enrollment fhe ON fhe.dialog_id=d.dialog_id "
                "LEFT JOIN synced_dialogs sd ON sd.dialog_id=d.dialog_id "
                "WHERE d.dialog_id=? AND fhe.dialog_id IS NULL AND d.hidden=0 "
                "AND d.type IN ('group','supergroup','forum') AND d.members BETWEEN 1 AND ? AND d.created >= ? "
                "AND (sd.status IS NULL OR sd.status NOT IN ('access_lost', 'synced'))",
                (dialog_id, policy.max_members, cutoff),
            ).fetchone()
            is not None
        )

    async def _process_one_automatic_group(
        self, expected_dialog_id: int | None = None, *, profile_owner_account_id: int | None = None
    ) -> bool:
        self._last_page_accepted = False
        self._last_page_stale = False
        self._last_automatic_pending = False
        self._last_page_error = None
        self._last_page_committed_revision = None
        if self._automatic_group_history is None:
            return False
        dialog_id = expected_dialog_id if expected_dialog_id is not None else self._next_automatic_group()
        if dialog_id is None:
            return False
        if self._automatic_group_retry_at(dialog_id) > time.time():
            self._last_automatic_pending = True
            return False
        if not self._is_eligible_automatic_group(
            self._conn, dialog_id, cutoff=int(time.time()) - self._automatic_group_history.recent_days * 86_400
        ):
            self._last_page_stale = expected_dialog_id is not None
            return False

        def persist_probe_retry(conn: sqlite3.Connection) -> None:
            with write_transaction(conn):
                self._set_automatic_group_retry(conn, dialog_id)

        try:
            page = await self._fetch_batch_page(dialog_id, 0, profile_owner_account_id)
        except Exception:
            await self._writer.run(persist_probe_retry)
            raise
        if page.retry is not None or page.total_messages is None:
            await self._writer.run(persist_probe_retry)
            self._last_automatic_pending = True
            return True
        staged: list[MembershipSignalCounts] = []
        observer = _FULL_MEMBERSHIP_OBSERVER.get()
        result = await self._writer.run(
            lambda conn: self._finalize_automatic_group_page(conn, dialog_id, page, profile_owner_account_id, staged)
        )
        _report_membership(observer, staged)
        return result

    def _finalize_automatic_group_page(
        self,
        conn: sqlite3.Connection,
        dialog_id: int,
        page: _FetchedBatchPage,
        profile_owner_account_id: int | None = None,
        membership_staged: list[MembershipSignalCounts] | None = None,
    ) -> bool:
        assert self._automatic_group_history is not None
        with write_transaction(conn):
            intent = read_intent(conn, dialog_id)
            if intent.enabled is not None:
                conn.execute("DELETE FROM daemon_state WHERE key=?", (f"automatic_group_history_retry:{dialog_id}",))
                if intent.enabled:
                    conn.execute(
                        "UPDATE synced_dialogs SET status='syncing',sync_progress=0 WHERE dialog_id=? AND status IN ('own_only', 'fragment')",
                        (dialog_id,),
                    )
                    self._store_batch_page_locked(conn, dialog_id, 0, page, profile_owner_account_id, membership_staged)
                return True
            if not self._is_eligible_automatic_group(
                conn,
                dialog_id,
                cutoff=int(time.time()) - self._automatic_group_history.recent_days * 86_400,
            ):
                return True
            accepted = (
                page.total_messages is not None and page.total_messages <= self._automatic_group_history.max_messages
            )
            intent = record_automatic_group_decision(
                conn, dialog_id, enabled=accepted, total_messages=page.total_messages
            )
            conn.execute("DELETE FROM daemon_state WHERE key=?", (f"automatic_group_history_retry:{dialog_id}",))
            if accepted and intent.enabled:
                conn.execute(
                    "UPDATE synced_dialogs SET status='syncing',sync_progress=0 WHERE dialog_id=? AND status IN ('own_only', 'fragment')",
                    (dialog_id,),
                )
                self._store_batch_page_locked(conn, dialog_id, 0, page, profile_owner_account_id, membership_staged)
        return True

    def _next_total_messages_repair_dialog(self) -> int | None:
        """Return the next enrolled accessible dialog missing its Telegram total."""
        retry_at = _total_messages_repair_retry_at(self._conn)
        if retry_at is not None and retry_at > int(time.time()):
            return None
        row = cast(tuple[int] | None, self._conn.execute(_NEXT_TOTAL_MESSAGES_REPAIR_SQL, (time.time(),)).fetchone())
        return None if row is None else int(row[0])

    def _expected_total_repair_is_ready(self, dialog_id: int) -> bool:
        eligible = cast(
            tuple[int] | None,
            self._conn.execute(
                "SELECT 1 FROM synced_dialogs sd "
                "JOIN full_history_enrollment fhe ON fhe.dialog_id=sd.dialog_id AND fhe.enabled=1 "
                "WHERE sd.dialog_id=? AND sd.total_messages IS NULL "
                "AND sd.status NOT IN ('not_synced','access_lost') "
                "AND COALESCE(sd.full_sync_retry_at,0)<=? "
                "AND NOT EXISTS (SELECT 1 FROM dialogs directory WHERE directory.dialog_id=sd.dialog_id AND directory.hidden=1)",
                (dialog_id, time.time()),
            ).fetchone(),
        )
        retry_at = _total_messages_repair_retry_at(self._conn)
        return eligible is not None and (retry_at is None or retry_at <= int(time.time()))

    def _deferred_batch_page(self, dialog_id: int, sync_progress: int, exc: Exception) -> _FetchedBatchPage:
        if isinstance(exc, TelegramRpcAdmissionDeferred):
            logger.info(
                "sync_batch admission_deferred dialog_id=%d retry_after=%s — preserving checkpoint",
                dialog_id,
                exc.retry_after_seconds,
            )
        else:
            logger.info(
                "sync_batch admission_deferred dialog_id=%d error_type=%s — preserving checkpoint",
                dialog_id,
                type(exc).__name__,
            )
        self._last_page_error = exc
        return _FetchedBatchPage(None, (), None, (sync_progress, False))

    async def _handle_batch_page_error(  # noqa: PLR0913, PLR0917
        self,
        dialog_id: int,
        sync_progress: int,
        exc: Exception,
        profile_owner_account_id: int | None,
        observation_order: int,
        observed_at: int,
    ) -> _FetchedBatchPage:
        if isinstance(exc, TelegramRpcAdmissionDeferred):
            return self._deferred_batch_page(dialog_id, sync_progress, exc)
        if isinstance(exc, (RpcAdmissionSaturatedError, RpcAdmissionExpiredError)):
            return self._deferred_batch_page(dialog_id, sync_progress, exc)
        if isinstance(exc, TelegramRpcThrottled):
            logger.warning("Telegram RPC throttled dialog_id=%d — retry_after=%s", dialog_id, exc.retry_after_seconds)
            self._last_page_error = exc
            return _FetchedBatchPage(None, (), None, (sync_progress, False))
        if isinstance(exc, MessageHistoryAccessLostError):
            result = await self._writer.run(
                lambda conn: self._persist_access_loss(
                    conn, dialog_id, exc.reason_code, profile_owner_account_id, observation_order, observed_at
                )
            )
            _report_membership(_FULL_ACCESS_LOSS_OBSERVER.get(), [result.membership_counts])
            if not result.changed:
                self._last_page_stale = True
            return _FetchedBatchPage(None, (), None, (sync_progress, True))
        raise exc

    async def _fetch_batch_page(
        self, dialog_id: int, sync_progress: int, profile_owner_account_id: int | None = None
    ) -> _FetchedBatchPage:
        self._last_page_error = None

        def prepare_observation(conn: sqlite3.Connection) -> tuple[int, int, int]:
            with write_transaction(conn):
                identity_revision = ensure_dialog_identity_baseline(conn, dialog_id)
                reaction_observed_at = int(time.time())
                return identity_revision, reaction_observed_at, allocate_observation_order(conn)

        identity_revision, reaction_observed_at, observation_order = await self._writer.run(prepare_observation)
        try:
            page = await self._history_port.fetch_page(dialog_id, before_message_id=sync_progress)
        except MessageHistoryAccessLostError as exc:
            observed_at = int(time.time())
            return await self._handle_batch_page_error(
                dialog_id, sync_progress, exc, profile_owner_account_id, observation_order, observed_at
            )
        except (TelegramRpcThrottled, RpcAdmissionSaturatedError, RpcAdmissionExpiredError) as exc:
            return await self._handle_batch_page_error(
                dialog_id, sync_progress, exc, profile_owner_account_id, observation_order, reaction_observed_at
            )
        except (MessageHistoryUnavailableError, TimeoutError) as exc:
            logger.exception(
                "sync_batch_rpc_error dialog_id=%d error=%s — dialog NOT marked synced, will retry",
                dialog_id,
                exc,
            )
            self._last_page_error = exc
            return _FetchedBatchPage(None, (), None, (sync_progress, False))
        if page.identity is not None:

            def persist_identity(conn: sqlite3.Connection) -> None:
                with write_transaction(conn):
                    assert page.identity is not None
                    publish_dialog_identity(conn, dialog_id, page.identity, identity_revision)

            await self._writer.run(persist_identity)
        total_messages = page.total_messages if sync_progress == 0 else None
        if (
            page.next_before_message_id is not None
            and sync_progress > 0
            and page.next_before_message_id >= sync_progress
        ):
            self._last_page_error = MessageHistoryUnavailableError("history cursor did not move backwards")
            return _FetchedBatchPage(None, (), None, (sync_progress, False))
        return _FetchedBatchPage(
            total_messages,
            tuple(replace(item, observation_order=observation_order) for item in page.messages),
            page.next_before_message_id,
            reaction_observed_at=reaction_observed_at,
        )

    @_full_sync_rpc_scope(DemandKind.FULL_SYNC_PAGE, AcquisitionKind.MESSAGE_HISTORY_PAGE)
    async def _fetch_batch(
        self, dialog_id: int, sync_progress: int, *, profile_owner_account_id: int | None = None
    ) -> tuple[int, bool]:
        """Fetch up to 100 messages for dialog_id older than sync_progress.

        Uses offset_id=sync_progress (exclusive) so each batch fetches
        messages strictly older than the last committed checkpoint.
        After a full batch (100 msgs), sync_progress advances to the min
        message_id; a partial or empty batch marks the dialog 'synced'.

        On TelegramRpcThrottled: sleep interruptibly, return (same_progress, False).
        On an ordinary remote error: log ERROR, return (same_progress, False) — dialog stays
        in-progress for retry on the next sync cycle.

        Returns:
            (new_progress, is_done)
        """
        if not full_history_enabled(self._conn, dialog_id):
            return sync_progress, True
        if sync_progress == 0:
            logger.info("sync_start dialog_id=%d", dialog_id)
        page = await self._fetch_batch_page(dialog_id, sync_progress, profile_owner_account_id)
        if page.retry is not None:
            return page.retry
        return await self._store_batch_page(
            dialog_id,
            sync_progress,
            page,
            profile_owner_account_id=profile_owner_account_id,
        )

    @_full_sync_rpc_scope(DemandKind.FULL_SYNC_PAGE, AcquisitionKind.MESSAGE_HISTORY_PAGE)
    async def _store_batch_page(
        self,
        dialog_id: int,
        sync_progress: int,
        page: _FetchedBatchPage,
        *,
        profile_owner_account_id: int | None = None,
    ) -> tuple[int, bool]:
        """Persist one fetched batch and update sync progress."""

        staged: list[MembershipSignalCounts] = []
        observer = _FULL_MEMBERSHIP_OBSERVER.get()

        def persist_page(conn: sqlite3.Connection) -> tuple[int, bool]:
            with write_transaction(conn):
                return self._store_batch_page_locked(
                    conn, dialog_id, sync_progress, page, profile_owner_account_id, staged
                )

        result = await self._writer.run(persist_page)
        _report_membership(observer, staged)
        return result

    def _store_batch_page_locked(  # noqa: PLR0913, PLR0917
        self,
        conn: sqlite3.Connection,
        dialog_id: int,
        sync_progress: int,
        page: _FetchedBatchPage,
        profile_owner_account_id: int | None = None,
        membership_staged: list[MembershipSignalCounts] | None = None,
    ) -> tuple[int, bool]:
        require_write_transaction(conn)
        if sync_progress == 0 and full_history_enabled(conn, dialog_id):
            conn.execute(
                "UPDATE synced_dialogs SET delta_message_id=? WHERE dialog_id=? AND delta_message_id IS NULL",
                (max((item.message.message_id for item in page.batch), default=0), dialog_id),
            )
        if page.next_before_message_id is None:
            return self._store_terminal_page_locked(conn, dialog_id, sync_progress, page)
        return self._store_continuation_page_locked(
            conn, dialog_id, sync_progress, page, profile_owner_account_id, membership_staged
        )

    def _store_terminal_page_locked(
        self, conn: sqlite3.Connection, dialog_id: int, sync_progress: int, page: _FetchedBatchPage
    ) -> tuple[int, bool]:
        if not full_history_enabled(conn, dialog_id):
            logger.info("sync_batch_discarded_disabled dialog_id=%d fetched=0", dialog_id)
            return sync_progress, True
        now = int(time.time())
        self._begin_topic_attribution_pass(conn, dialog_id, sync_progress, now)
        cursor = conn.execute(
            _UPDATE_PROGRESS_DONE_SQL,
            (sync_progress, "synced", page.total_messages, now, dialog_id, dialog_id, sync_progress),
        )
        self._last_page_accepted = cursor.rowcount == 1
        if not self._last_page_accepted:
            self._last_page_stale = True
            return sync_progress, False
        self._complete_topic_attribution_pass(conn, dialog_id, now)
        self._capture_committed_page_revision(conn, dialog_id)
        logger.info("sync_done dialog_id=%d status=synced (empty batch)", dialog_id)
        return sync_progress, True

    def _store_continuation_page_locked(  # noqa: PLR0913, PLR0917
        self,
        conn: sqlite3.Connection,
        dialog_id: int,
        sync_progress: int,
        page: _FetchedBatchPage,
        profile_owner_account_id: int | None = None,
        membership_staged: list[MembershipSignalCounts] | None = None,
    ) -> tuple[int, bool]:
        assert page.next_before_message_id is not None
        rows = list(page.batch)
        new_progress = page.next_before_message_id
        if not full_history_enabled(conn, dialog_id):
            logger.info("sync_batch_discarded_disabled dialog_id=%d fetched=%d", dialog_id, len(rows))
            self._last_page_stale = True
            return sync_progress, True
        now = int(time.time())
        cursor = conn.execute(
            _UPDATE_PROGRESS_SQL,
            (new_progress, "syncing", page.total_messages, dialog_id, sync_progress, dialog_id),
        )
        self._last_page_accepted = cursor.rowcount == 1
        if not self._last_page_accepted:
            self._last_page_stale = True
            return sync_progress, False
        self._begin_topic_attribution_pass(conn, dialog_id, sync_progress, now)
        result = insert_messages_with_fts(
            conn,
            rows,
            priority=HydrationPriority.BACKFILL,
            reaction_observed_at=page.reaction_observed_at,
            profile_owner_account_id=profile_owner_account_id,
        )
        if membership_staged is not None:
            membership_staged.append(result.membership_counts)
        self._record_no_topic_attribution(conn, dialog_id, rows)
        self._capture_committed_page_revision(conn, dialog_id)
        logger.debug(
            "sync_batch dialog_id=%d fetched=%d progress=%d done=%s",
            dialog_id,
            len(page.batch),
            new_progress,
            False,
        )
        return new_progress, False

    def _capture_committed_page_revision(self, conn: sqlite3.Connection, dialog_id: int) -> None:
        row = cast(
            tuple[int | None, str] | None,
            conn.execute("SELECT sync_progress,status FROM synced_dialogs WHERE dialog_id=?", (dialog_id,)).fetchone(),
        )
        if row is not None:
            self._last_page_committed_revision = (
                f"{int(row[0] or 0)}:{int(full_history_enabled(conn, dialog_id))}:{row[1]}"
            )

    def _begin_topic_attribution_pass(
        self, conn: sqlite3.Connection, dialog_id: int, sync_progress: int, observed_at: int
    ) -> None:
        """Start or safely resume a current-extractor full-history receipt."""
        require_write_transaction(conn)
        del sync_progress
        conn.execute(
            "UPDATE synced_dialogs SET topic_attribution_version=?, topic_attribution_state='partial', "
            "topic_attribution_observed_at=?, topic_attribution_completed_at=NULL, topic_attribution_no_topic_count=0 "
            "WHERE dialog_id=? AND status IN ('not_synced', 'syncing') "
            "AND (topic_attribution_version<>? OR topic_attribution_state<>'partial')",
            (TOPIC_ATTRIBUTION_EXTRACTOR_VERSION, observed_at, dialog_id, TOPIC_ATTRIBUTION_EXTRACTOR_VERSION),
        )

    def _record_no_topic_attribution(
        self, conn: sqlite3.Connection, dialog_id: int, batch: Sequence[_ExtractedMessage]
    ) -> None:
        """Record legal extractor NULL outcomes from one current history page."""
        require_write_transaction(conn)
        count = sum(message.message.forum_topic_id is None for message in batch)
        if count:
            conn.execute(
                "UPDATE synced_dialogs SET topic_attribution_no_topic_count=topic_attribution_no_topic_count+? "
                "WHERE dialog_id=? AND topic_attribution_version=? AND topic_attribution_state='partial'",
                (count, dialog_id, TOPIC_ATTRIBUTION_EXTRACTOR_VERSION),
            )

    def _complete_topic_attribution_pass(self, conn: sqlite3.Connection, dialog_id: int, completed_at: int) -> None:
        """Publish a full current-extractor traversal, including legal NULL outcomes."""
        require_write_transaction(conn)
        conn.execute(
            "UPDATE synced_dialogs SET topic_attribution_state='complete', topic_attribution_completed_at=? "
            "WHERE dialog_id=? AND topic_attribution_version=? AND topic_attribution_state='partial'",
            (completed_at, dialog_id, TOPIC_ATTRIBUTION_EXTRACTOR_VERSION),
        )


class FullSyncDemandAdapter:
    """Bounded durable adapter over ``synced_dialogs.sync_progress``."""

    demand_kind = DemandKind.FULL_SYNC_PAGE

    def __init__(
        self,
        worker: FullSyncWorker,
        *,
        account_id: int,
        membership_observer: Callable[[MembershipSignalCounts], None] | None = None,
        access_loss_observer: Callable[[MembershipSignalCounts], None] | None = None,
    ) -> None:
        self._access_loss_observer = access_loss_observer
        self._membership_observer = membership_observer
        self._worker = worker
        self._account_id = account_id

    def _revision(self, target: str, revision: str) -> WorkRevision:
        return WorkRevision(WorkKey(self.demand_kind, self._account_id, target), revision)

    def status(self, now: float, *, excluded: frozenset[WorkRevision] = frozenset()) -> DemandStatus | None:
        """Report pending history without changing local or Telegram state."""
        pending = self._pending_dialog_status(now, excluded)
        if pending is not None and pending.release_at <= now:
            return pending
        statuses = [
            status
            for status in (self._automatic_group_status(excluded), self._total_repair_status(now, excluded))
            if status is not None
        ]
        for status in statuses:
            if status.release_at <= now:
                return status
        if pending is not None:
            statuses.append(pending)
        return min(statuses, key=lambda status: status.release_at) if statuses else None

    def _pending_dialog_status(self, now: float, excluded: frozenset[WorkRevision]) -> DemandStatus | None:
        rows = cast(
            list[tuple[int, int | None, int | None, str]],
            self._worker._conn.execute(
                "SELECT sd.dialog_id,sd.sync_progress,sd.full_sync_retry_at,sd.status FROM synced_dialogs sd "
                "JOIN full_history_enrollment fhe ON fhe.dialog_id=sd.dialog_id AND fhe.enabled=1 "
                "WHERE sd.status IN ('syncing','not_synced') "
                "AND NOT EXISTS (SELECT 1 FROM dialogs d WHERE d.dialog_id=sd.dialog_id AND d.hidden=1) "
                "ORDER BY CASE WHEN sd.dialog_id>COALESCE((SELECT CAST(value AS INTEGER) FROM daemon_state WHERE key='full_sync_peer_cursor'),0) THEN 0 ELSE 1 END,sd.dialog_id"
            ).fetchall(),
        )
        future: DemandStatus | None = None
        for dialog_id_value, cursor_value, retry_at, dialog_status in rows:
            dialog_id, cursor = int(dialog_id_value), int(cursor_value or 0)
            selected = self._revision(
                f"dialog:{dialog_id}",
                f"{cursor}:{int(full_history_enabled(self._worker._conn, dialog_id))}:{dialog_status}",
            )
            if selected not in excluded:
                status = DemandStatus(release_at=float(retry_at or 0), selected=selected)
                if status.release_at <= now:
                    return status
                if future is None or status.release_at < future.release_at:
                    future = status
        return future

    def _automatic_group_status(self, excluded: frozenset[WorkRevision]) -> DemandStatus | None:
        policy = self._worker._automatic_group_history
        if policy is None:
            return None
        now = int(time.time())
        cutoff = now - policy.recent_days * 86_400
        rows = cast(
            list[tuple[int, int | None]],
            self._worker._conn.execute(
                "SELECT d.dialog_id,CAST(ds.value AS INTEGER) FROM dialogs d "
                "LEFT JOIN full_history_enrollment fhe ON fhe.dialog_id=d.dialog_id "
                "LEFT JOIN synced_dialogs sd ON sd.dialog_id=d.dialog_id "
                "LEFT JOIN daemon_state ds ON ds.key='automatic_group_history_retry:' || d.dialog_id "
                "WHERE fhe.dialog_id IS NULL AND d.hidden=0 AND d.type IN ('group','supergroup','forum') "
                "AND d.members BETWEEN 1 AND ? AND d.created >= ? "
                "AND (sd.status IS NULL OR sd.status NOT IN ('access_lost','synced')) ORDER BY d.dialog_id",
                (policy.max_members, cutoff),
            ).fetchall(),
        )
        candidates: list[tuple[float, int, WorkRevision]] = []
        for group_id, retry_at in rows:
            selected = self._revision(f"automatic-group:{group_id}", "automatic-group")
            if selected not in excluded:
                candidates.append((float(retry_at or 0), group_id, selected))
        if not candidates:
            return None
        release, _, selected = min(candidates, key=lambda item: (item[0] > now, item[0], item[1]))
        return DemandStatus(release_at=release, selected=selected)

    def _total_repair_status(self, now: float, excluded: frozenset[WorkRevision]) -> DemandStatus | None:
        retry_at = _total_messages_repair_retry_at(self._worker._conn)
        rows = cast(
            list[tuple[int, int | None]],
            self._worker._conn.execute(
                _NEXT_TOTAL_MESSAGES_REPAIR_SQL.replace(
                    "SELECT sd.dialog_id", "SELECT sd.dialog_id,sd.full_sync_retry_at"
                )
                .replace("AND COALESCE(sd.full_sync_retry_at,0) <= ? ", "")
                .replace(" LIMIT 1", "")
            ).fetchall(),
        )
        future: DemandStatus | None = None
        for repair, peer_retry in rows:
            selected = self._revision(f"total:{repair}", f"missing:{repair}")
            if selected not in excluded:
                status = DemandStatus(release_at=float(max(peer_retry or 0, retry_at or 0)), selected=selected)
                if status.release_at <= now:
                    return status
                if future is None or status.release_at < future.release_at:
                    future = status
        return future

    async def run_slice(self, budget: RpcAttemptBudget) -> SliceOutcome:
        """Fetch at most one history page under the transport attempt budget."""
        if not isinstance(budget, RpcAttemptBudget):
            raise TypeError("budget must be an RpcAttemptBudget")
        selected = budget.selected
        if selected is None:
            raise RuntimeError("full-sync slice requires an owner-selected revision")
        validation = self._validate_selected(selected, time.time())
        if validation is not None:
            return validation
        token = _FULL_MEMBERSHIP_OBSERVER.set(self._membership_observer)
        access_token = _FULL_ACCESS_LOSS_OBSERVER.set(self._access_loss_observer)
        try:
            stale = await self._dispatch_selected(selected, budget)
        finally:
            _FULL_MEMBERSHIP_OBSERVER.reset(token)
            _FULL_ACCESS_LOSS_OBSERVER.reset(access_token)
        if stale:
            return SliceOutcome(selected, SliceOutcomeKind.STALE)
        return self._outcome_for_selected(selected)

    def _validate_selected(self, selected: WorkRevision, now: float) -> SliceOutcome | None:
        if selected.key.demand_kind is not self.demand_kind or selected.key.account_id != self._account_id:
            return SliceOutcome(selected, SliceOutcomeKind.STALE)
        target = selected.key.target
        if target.startswith("dialog:"):
            return self._validate_dialog(selected, now)
        if target.startswith("automatic-group:"):
            return self._validate_automatic_group(selected, now)
        if target.startswith("total:"):
            return self._validate_total_repair(selected, now)
        return SliceOutcome(selected, SliceOutcomeKind.STALE)

    def _validate_dialog(self, selected: WorkRevision, now: float) -> SliceOutcome | None:
        dialog_id = int(selected.key.target.split(":", 1)[1])
        row = cast(
            tuple[int | None, int | None, str, int | None] | None,
            self._worker._conn.execute(
                "SELECT sd.sync_progress,sd.full_sync_retry_at,sd.status,fhe.enabled FROM synced_dialogs sd "
                "LEFT JOIN full_history_enrollment fhe ON fhe.dialog_id=sd.dialog_id WHERE sd.dialog_id=?",
                (dialog_id,),
            ).fetchone(),
        )
        if row is None or row[2] not in {"syncing", "not_synced"} or not row[3]:
            return SliceOutcome(selected, SliceOutcomeKind.STALE)
        revision = f"{int(row[0] or 0)}:{int(full_history_enabled(self._worker._conn, dialog_id))}:{row[2]}"
        if revision != selected.revision:
            return SliceOutcome(selected, SliceOutcomeKind.STALE)
        if float(row[1] or 0) > now:
            return SliceOutcome(selected, SliceOutcomeKind.RETRYABLE_FAILURE)
        return None

    def _validate_automatic_group(self, selected: WorkRevision, now: float) -> SliceOutcome | None:
        dialog_id = int(selected.key.target.split(":", 1)[1])
        policy = self._worker._automatic_group_history
        eligible = (
            policy is not None
            and selected.revision == "automatic-group"
            and self._worker._is_eligible_automatic_group(
                self._worker._conn, dialog_id, cutoff=int(now) - policy.recent_days * 86_400
            )
        )
        if not eligible:
            return SliceOutcome(selected, SliceOutcomeKind.STALE)
        if self._worker._automatic_group_retry_at(dialog_id) > now:
            return SliceOutcome(selected, SliceOutcomeKind.RETRYABLE_FAILURE)
        return None

    def _validate_total_repair(self, selected: WorkRevision, now: float) -> SliceOutcome | None:
        dialog_id = int(selected.key.target.split(":", 1)[1])
        row = cast(
            tuple[int | None, int | None] | None,
            self._worker._conn.execute(
                "SELECT total_messages,full_sync_retry_at FROM synced_dialogs "
                "WHERE dialog_id=? AND status NOT IN ('not_synced', 'access_lost')",
                (dialog_id,),
            ).fetchone(),
        )
        if row is None or row[0] is not None or selected.revision != f"missing:{dialog_id}":
            return SliceOutcome(selected, SliceOutcomeKind.STALE)
        if float(row[1] or 0) > now:
            return SliceOutcome(selected, SliceOutcomeKind.RETRYABLE_FAILURE)
        return None

    async def _dispatch_selected(self, selected: WorkRevision, budget: RpcAttemptBudget) -> bool:
        with _full_sync_root_scope(DemandKind.FULL_SYNC_PAGE):
            with rpc_attempt_budget(budget):
                if selected.key.target.startswith("automatic-group:"):
                    return await self._dispatch_automatic_group(selected)
                if selected.key.target.startswith("dialog:"):
                    return await self._dispatch_dialog(selected)
                return await self._dispatch_total_repair(selected)

    async def _dispatch_automatic_group(self, selected: WorkRevision) -> bool:
        dialog_id = int(selected.key.target.split(":", 1)[1])
        await self._worker._process_one_automatic_group(dialog_id, profile_owner_account_id=self._account_id)
        self._raise_unexpected_page_error()
        return self._worker._last_page_stale

    async def _dispatch_dialog(self, selected: WorkRevision) -> bool:
        dialog_id = int(selected.key.target.split(":", 1)[1])
        cursor = int(selected.revision.split(":", 1)[0])
        await self._worker.process_one_batch(
            expected_dialog=dialog_id, expected_cursor=cursor, profile_owner_account_id=self._account_id
        )
        self._raise_unexpected_page_error()
        return self._worker._last_page_stale

    async def _dispatch_total_repair(self, selected: WorkRevision) -> bool:
        dialog_id = int(selected.key.target.split(":", 1)[1])
        await self._worker.repair_one_total_messages(
            expected_dialog_id=dialog_id, profile_owner_account_id=self._account_id
        )
        self._raise_unexpected_total_error()
        return self._worker._last_total_stale

    def _raise_unexpected_page_error(self) -> None:
        error = self._worker._last_page_error
        if error is not None and not isinstance(error, (MessageHistoryUnavailableError, TimeoutError)):
            raise error

    def _raise_unexpected_total_error(self) -> None:
        error = self._worker._last_total_repair_error
        if error is not None and not isinstance(error, (MessageHistoryUnavailableError, TimeoutError)):
            raise error

    def _outcome_for_selected(self, selected: WorkRevision) -> SliceOutcome:
        error = (
            self._worker._last_total_repair_error
            if selected.key.target.startswith("total:")
            else self._worker._last_page_error
        )
        if error is not None:
            return self._error_outcome(selected, error)
        if self._worker._last_page_stale or (
            selected.key.target.startswith("total:") and self._worker._last_total_stale
        ):
            return SliceOutcome(selected, SliceOutcomeKind.STALE)
        if selected.key.target.startswith("automatic-group:") and self._worker._last_automatic_pending:
            return SliceOutcome(selected, SliceOutcomeKind.RETRYABLE_FAILURE)
        return self._accepted_outcome(selected)

    @staticmethod
    def _error_outcome(selected: WorkRevision, error: BaseException) -> SliceOutcome:
        deferred = isinstance(
            error,
            (TelegramRpcThrottled, TelegramRpcAdmissionDeferred, RpcAdmissionSaturatedError, RpcAdmissionExpiredError),
        )
        if deferred:
            return SliceOutcome(selected, SliceOutcomeKind.DEFERRED)
        if isinstance(error, (MessageHistoryUnavailableError, TimeoutError)):
            return SliceOutcome(selected, SliceOutcomeKind.RETRYABLE_FAILURE)
        return SliceOutcome(selected, SliceOutcomeKind.NO_PROGRESS)

    def _accepted_outcome(self, selected: WorkRevision) -> SliceOutcome:
        if selected.key.target.startswith("dialog:") and self._worker._last_page_accepted:
            revision = self._worker._last_page_committed_revision
            if revision is not None:
                kind = SliceOutcomeKind.TERMINAL if revision.endswith(":synced") else SliceOutcomeKind.PROGRESSED
                return SliceOutcome(selected, kind, revision)
        if selected.key.target.startswith("total:") and self._worker._last_total_accepted:
            revision = self._worker._last_total_committed_revision
            if revision is not None:
                return SliceOutcome(selected, SliceOutcomeKind.TERMINAL, revision)
        return SliceOutcome(selected, SliceOutcomeKind.NO_PROGRESS)


class FullSyncDmEnrollmentDemandAdapter:
    """Consume completed canonical dialog publications into local sync state."""

    demand_kind = DemandKind.FULL_SYNC_DM_ENROLLMENT

    def __init__(self, worker: FullSyncWorker, *, account_id: int) -> None:
        self._worker = worker
        self._account_id = account_id

    def status(self, now: float, *, excluded: frozenset[WorkRevision] = frozenset()) -> DemandStatus | None:
        """Report an unconsumed completed publication without writing."""
        del now
        generation = self._worker._published_dm_generation(self._worker._conn)
        if generation is None or generation <= self._worker._last_consumed_dm_generation(self._worker._conn):
            return None
        selected = WorkRevision(WorkKey(self.demand_kind, self._account_id, "publication"), str(generation))
        return None if selected in excluded else DemandStatus(release_at=0.0, selected=selected)

    async def run_slice(self, budget: RpcAttemptBudget) -> SliceOutcome:
        """Consume the pending publication without making a Telegram request."""
        if not isinstance(budget, RpcAttemptBudget):
            raise TypeError("budget must be an RpcAttemptBudget")
        selected = budget.selected
        if selected is None:
            raise RuntimeError("DM enrollment slice requires an owner-selected revision")
        current = self.status(time.time())
        if current is None or current.selected != selected:
            return SliceOutcome(selected, SliceOutcomeKind.STALE)
        with _full_sync_root_scope(DemandKind.FULL_SYNC_DM_ENROLLMENT):
            with rpc_attempt_budget(budget):
                receipt = await self._worker._writer.run(
                    lambda conn: self._worker._consume_dm_publication(conn, expected_generation=int(selected.revision))
                )
        if receipt is None:
            return SliceOutcome(selected, SliceOutcomeKind.STALE)
        _, generation = receipt
        return SliceOutcome(selected, SliceOutcomeKind.PROGRESSED, f"{generation}:consumed")


_EXPORTED_SYMBOLS = (
    FullSyncDmEnrollmentDemandAdapter,
    FullSyncDemandAdapter,
    FullSyncWorker,
    FullSyncWorker.process_one_batch,
    FullSyncWorker.repair_one_total_messages,
)

__all__ = [
    "FullSyncDemandAdapter",
    "FullSyncDmEnrollmentDemandAdapter",
    "FullSyncWorker",
    "TotalMessagesProbe",
]
