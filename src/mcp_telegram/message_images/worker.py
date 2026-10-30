"""Finite image acquisition under the existing durable demand coordinator."""

import asyncio
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

from telethon.errors import FileReferenceExpiredError, FilerefUpgradeNeededError, RPCError

from ..entity_profile.repository import bound_profile_account
from ..flood import TelegramRpcThrottled
from ..image_authorization import ScopeToken
from ..messages.sqlite_bundle import insert_messages_with_fts
from ..messages.telegram_adapter import extract_message_row
from ..observation_order import allocate_observation_order
from ..sqlite_writer import SQLiteWriter
from ..sync_transactions import write_transaction
from ..telegram_demand import DemandStatus, RpcAttemptBudget, RpcAttemptBudgetExhaustedError, durable_demand_context
from ..telegram_demand_progress import SliceOutcome, SliceOutcomeKind, WorkKey, WorkRevision
from ..telegram_image_download import (
    PhotoDownloadSession,
    PhotoLocator,
    PhotoTarget,
    PhotoUnsupportedError,
    PhotoWork,
    TelegramImageGateway,
)
from ..telegram_rpc_consumers import DemandKind
from ..telegram_rpc_scheduler import RpcAdmissionError, TelegramRpcMethodRecoveryPending, rpc_attempt_budget
from .contracts import ImageScope
from .fence import DurableBudget, SourceFence
from .limits import PHOTO_MAX_BYTES
from .repository import ImageSource, current_source
from .store import cleanup_unpublished, publish_image


@dataclass(slots=True)
class _Transfer:
    scope: ScopeToken
    target: PhotoTarget
    attempt: int
    source: ImageSource
    locator: PhotoLocator
    session: PhotoDownloadSession
    owner: asyncio.Task[object] | None
    data: bytearray


def _matches_resolved_photo(source: ImageSource, updated: ImageSource | None, locator: PhotoLocator) -> bool:
    return (
        updated is not None
        and updated.supported
        and updated.photo_id == locator.photo_id
        and (source.photo_id is None or source.photo_id == locator.photo_id)
    )


def _transfer_generation(transfer: _Transfer | None, attempt: int, fallback: int) -> int:
    return transfer.source.generation if transfer is not None and transfer.attempt == attempt else fallback


def _transfer_offset(transfer: _Transfer | None, attempt: int, target: PhotoTarget, scope: ScopeToken) -> int:
    return (
        len(transfer.data)
        if transfer is not None
        and transfer.attempt == attempt
        and transfer.target == target
        and transfer.scope == scope
        else 0
    )


class MessageImageWorker:
    """Single coordinator-owned active transfer and its durable budget."""

    demand_kind = DemandKind.MESSAGE_IMAGE_ACQUISITION

    def __init__(
        self,
        conn: sqlite3.Connection,
        writer: SQLiteWriter,
        directory: Path,
        scope: ImageScope,
        gateway: TelegramImageGateway,
    ) -> None:
        self._conn = conn
        self._writer = writer
        self._directory = directory
        self._scope = scope
        self._gateway = gateway
        self._active: _Transfer | None = None
        self._resume: _Transfer | None = None
        self._opening: tuple[int, int, int, int] | None = None

    def status(self, now: float, *, excluded: frozenset[WorkRevision] = frozenset()) -> DemandStatus | None:
        scope = self._scope.capture()
        if scope is None or bound_profile_account(self._conn) != scope.scope.account_id:
            return None
        rows = cast(
            list[tuple[int, int, int, int, int]],
            self._conn.execute(
                "SELECT dialog_id,message_id,attempt_generation,source_generation,offset FROM message_image_jobs "
                "WHERE account_id=? AND state IN ('queued','running') ORDER BY dialog_id,message_id",
                (scope.scope.account_id,),
            ).fetchall(),
        )
        for dialog, message, attempt, source, offset in rows:
            revision = WorkRevision(
                WorkKey(DemandKind.MESSAGE_IMAGE_ACQUISITION, scope.scope.account_id, f"{dialog}:{message}"),
                f"{attempt}:{source}:{offset}",
            )
            if revision not in excluded:
                return DemandStatus(now, selected=revision)
        return None

    async def run_slice(self, budget: RpcAttemptBudget) -> SliceOutcome:
        selected = budget.selected
        if selected is None:
            raise RuntimeError("image slice has no selected work")
        scope = self._scope.capture()
        if scope is None or scope.scope.account_id != selected.key.account_id:
            return SliceOutcome(selected, SliceOutcomeKind.STALE)
        dialog_id, message_id = (int(part) for part in selected.key.target.split(":"))
        target = PhotoTarget(dialog_id, message_id)
        try:
            with durable_demand_context(DemandKind.MESSAGE_IMAGE_ACQUISITION), rpc_attempt_budget(budget):
                return await self._run_transfer(selected, scope, target)
        except (
            TelegramRpcThrottled,
            TelegramRpcMethodRecoveryPending,
            RpcAttemptBudgetExhaustedError,
            RpcAdmissionError,
        ):
            self._revoke_opening()
            raise
        except asyncio.CancelledError:
            await self.close()
            raise
        except PhotoUnsupportedError:
            await self.close()
            return await self._fail(selected, scope, reason="unsupported_photo")
        except OSError, ValueError, RuntimeError, RPCError:
            return await self._recover_or_fail(selected, scope, target)

    async def _recover_or_fail(self, selected: WorkRevision, scope: ScopeToken, target: PhotoTarget) -> SliceOutcome:
        source = current_source(self._conn, target.dialog_id, target.message_id)
        if self._active is not None and source != self._active.source:
            return await self._rebind_same_photo(selected, scope, target, source)
        if self._active is None and source is not None and source.generation != int(selected.revision.split(":")[1]):
            return await self._rebind_same_photo(selected, scope, target, source)
        await self.close()
        return await self._fail(selected, scope, reason="acquisition_failed")

    def _source_is_current(
        self, conn: sqlite3.Connection, scope: ScopeToken, target: PhotoTarget, source: ImageSource
    ) -> bool:
        return (
            bound_profile_account(conn) == scope.scope.account_id
            and current_source(conn, target.dialog_id, target.message_id) == source
            and self._scope.is_current(scope)
        )

    async def _run_transfer(self, selected: WorkRevision, scope: ScopeToken, target: PhotoTarget) -> SliceOutcome:
        attempt, source_generation, offset = (int(part) for part in selected.revision.split(":"))
        source = current_source(self._conn, target.dialog_id, target.message_id)
        if source is None or source.generation != source_generation:
            return await self._rebind_same_photo(selected, scope, target, source)
        transfer = self._active
        if (
            transfer is None
            or transfer.target != target
            or transfer.attempt != attempt
            or transfer.owner != asyncio.current_task()
        ):
            await self.close(preserve_resume=True)
            transfer = await self._open(scope, target, attempt, source_generation)
        if transfer is None:
            return await self._handle_unopened(selected, scope, target, source_generation)
        try:
            return await self._receive(selected, transfer, offset)
        except FileReferenceExpiredError, FilerefUpgradeNeededError:
            return await self._refresh_reference(selected, transfer)

    async def _handle_unopened(
        self, selected: WorkRevision, scope: ScopeToken, target: PhotoTarget, generation: int
    ) -> SliceOutcome:
        current = current_source(self._conn, target.dialog_id, target.message_id)
        if current is not None and current.generation != generation:
            return await self._rebind_same_photo(selected, scope, target, current)
        return SliceOutcome(selected, SliceOutcomeKind.STALE)

    async def _rebind_same_photo(
        self, selected: WorkRevision, scope: ScopeToken, target: PhotoTarget, source: ImageSource | None
    ) -> SliceOutcome:
        active = self._active
        resume = active if active is not None else self._resume
        await self.close()
        if source is None or source.photo_id is None or not source.supported:
            return SliceOutcome(selected, SliceOutcomeKind.STALE)
        attempt, selected_generation, _offset = (int(part) for part in selected.revision.split(":"))
        generation = _transfer_generation(active, attempt, selected_generation)
        saved_offset = _transfer_offset(resume, attempt, target, scope)

        def rebind(conn: sqlite3.Connection) -> bool:
            with write_transaction(conn):
                row = cast(
                    tuple[int] | None,
                    conn.execute(
                        "SELECT 1 FROM message_image_jobs WHERE account_id=? AND dialog_id=? AND message_id=? "
                        "AND attempt_generation=? AND source_generation=? AND photo_id=? AND state IN ('queued','running')",
                        (
                            scope.scope.account_id,
                            target.dialog_id,
                            target.message_id,
                            attempt,
                            generation,
                            source.photo_id,
                        ),
                    ).fetchone(),
                )
                if row is None or not self._source_is_current(conn, scope, target, source):
                    return False
                self._scope.sources.revoke(scope.scope.account_id, target.dialog_id, target.message_id, attempt)
                return (
                    conn.execute(
                        "UPDATE message_image_jobs SET source_generation=?,offset=?,state='queued' "
                        "WHERE account_id=? AND dialog_id=? AND message_id=? AND attempt_generation=? AND source_generation=?",
                        (
                            source.generation,
                            saved_offset,
                            scope.scope.account_id,
                            target.dialog_id,
                            target.message_id,
                            attempt,
                            generation,
                        ),
                    ).rowcount
                    == 1
                )

        if not await self._writer.run(rebind):
            return SliceOutcome(selected, SliceOutcomeKind.STALE)
        self._resume = resume if saved_offset else None
        return SliceOutcome(selected, SliceOutcomeKind.PROGRESSED, f"{attempt}:{source.generation}:{saved_offset}")

    async def _receive(self, selected: WorkRevision, transfer: _Transfer, offset: int) -> SliceOutcome:
        if len(transfer.data) != offset:
            return await self._record_offset(selected, transfer, len(transfer.data))
        if len(transfer.data) == transfer.locator.size:
            return await self._publish(selected, transfer)
        async for chunk in transfer.session.chunks(offset=offset):
            if len(transfer.data) + len(chunk) > min(PHOTO_MAX_BYTES, transfer.locator.size):
                raise ValueError("photo size exceeded")
            transfer.data.extend(chunk)
        if len(transfer.data) == transfer.locator.size:
            return await self._publish(selected, transfer)
        if len(transfer.data) == offset:
            raise ValueError("photo download made no progress")
        return await self._record_offset(selected, transfer, len(transfer.data))

    async def _refresh_reference(self, selected: WorkRevision, transfer: _Transfer) -> SliceOutcome:
        def reserve(conn: sqlite3.Connection) -> bool:
            with write_transaction(conn):
                if not self._current(conn, transfer):
                    return False
                return (
                    conn.execute(
                        "UPDATE message_image_jobs SET reference_refreshes=1 WHERE account_id=? AND dialog_id=? "
                        "AND message_id=? AND attempt_generation=? AND reference_refreshes=0 AND state='running'",
                        (
                            transfer.scope.scope.account_id,
                            transfer.target.dialog_id,
                            transfer.target.message_id,
                            transfer.attempt,
                        ),
                    ).rowcount
                    == 1
                )

        if not await self._writer.run(reserve):
            raise ValueError("photo reference refresh unavailable")
        data = transfer.data
        previous = transfer.locator
        await self.close()
        refreshed = await self._open(transfer.scope, transfer.target, transfer.attempt, transfer.source.generation)
        if refreshed is None or (
            refreshed.locator.photo_id,
            refreshed.locator.rendition,
            refreshed.locator.size,
            refreshed.locator.width,
            refreshed.locator.height,
        ) != (
            previous.photo_id,
            previous.rendition,
            previous.size,
            previous.width,
            previous.height,
        ):
            await self.close()
            raise ValueError("photo changed during reference refresh")
        refreshed.data = data
        return await self._record_offset(selected, refreshed, len(data))

    async def _open(self, scope: ScopeToken, target: PhotoTarget, attempt: int, generation: int) -> _Transfer | None:
        source = current_source(self._conn, target.dialog_id, target.message_id)
        if source is None or source.generation != generation or not source.supported:
            return None
        self._opening = (scope.scope.account_id, target.dialog_id, target.message_id, attempt)

        def reserve(conn: sqlite3.Connection) -> int:
            with write_transaction(conn):
                if not self._source_is_current(conn, scope, target, source):
                    raise RuntimeError("image source changed")
                return allocate_observation_order(conn)

        order = await self._writer.run(reserve)
        allowance = DurableBudget(self._writer, scope.scope.account_id, target.dialog_id, target.message_id, attempt)
        source_fence = SourceFence(
            self._writer, self._scope, scope, target.dialog_id, target.message_id, source, attempt
        )
        observation = await self._gateway.resolve_photo(
            target, scope=scope.scope, observation_order=order, budget=allowance, source_fence=source_fence
        )
        locator = observation.locator
        if locator.size <= 0 or locator.size > PHOTO_MAX_BYTES:
            raise PhotoUnsupportedError("unsupported_photo")
        extracted = replace(extract_message_row(target.dialog_id, observation.message), observation_order=order)
        if extracted.message.message_id != target.message_id or extracted.private_photo_id != locator.photo_id:
            raise ValueError("photo identity mismatch")

        def apply(conn: sqlite3.Connection) -> ImageSource | None:
            with write_transaction(conn):
                if not self._source_is_current(conn, scope, target, source):
                    return None
                if insert_messages_with_fts(conn, [extracted]) != 1:
                    return None
                updated = current_source(conn, target.dialog_id, target.message_id)
                if not _matches_resolved_photo(source, updated, locator):
                    return None
                assert updated is not None
                count = conn.execute(
                    "UPDATE message_image_jobs SET state='running',source_generation=?,photo_id=?,offset=0 "
                    "WHERE account_id=? AND dialog_id=? AND message_id=? AND attempt_generation=? AND source_generation=?",
                    (
                        updated.generation,
                        updated.photo_id,
                        scope.scope.account_id,
                        target.dialog_id,
                        target.message_id,
                        attempt,
                        generation,
                    ),
                ).rowcount
                return updated if count == 1 else None

        updated = await self._writer.run(apply)
        if updated is None:
            self._revoke_opening()
            return None
        fence = SourceFence(self._writer, self._scope, scope, target.dialog_id, target.message_id, updated, attempt)
        session = await self._gateway.open_photo_download(
            locator,
            scope=scope.scope,
            work=PhotoWork(
                f"{scope.scope.account_id}:{target.dialog_id}:{target.message_id}",
                str(attempt),
                str(updated.generation),
            ),
            budget=allowance,
            source_fence=fence,
        )
        data = self._take_resume(scope, target, attempt, locator)
        transfer = _Transfer(scope, target, attempt, updated, locator, session, asyncio.current_task(), data)
        self._active = transfer
        self._opening = None
        return transfer

    def _take_resume(self, scope: ScopeToken, target: PhotoTarget, attempt: int, locator: PhotoLocator) -> bytearray:
        resume, self._resume = self._resume, None
        if (
            resume is not None
            and resume.scope == scope
            and resume.target == target
            and resume.attempt == attempt
            and (
                resume.locator.photo_id,
                resume.locator.rendition,
                resume.locator.size,
                resume.locator.width,
                resume.locator.height,
            )
            == (locator.photo_id, locator.rendition, locator.size, locator.width, locator.height)
        ):
            return resume.data
        return bytearray()

    async def _record_offset(self, selected: WorkRevision, transfer: _Transfer, offset: int) -> SliceOutcome:
        def save(conn: sqlite3.Connection) -> bool:
            with write_transaction(conn):
                if not self._current(conn, transfer):
                    return False
                return (
                    conn.execute(
                        "UPDATE message_image_jobs SET offset=? WHERE account_id=? AND dialog_id=? AND message_id=? "
                        "AND attempt_generation=? AND source_generation=? AND state='running'",
                        (
                            offset,
                            transfer.scope.scope.account_id,
                            transfer.target.dialog_id,
                            transfer.target.message_id,
                            transfer.attempt,
                            transfer.source.generation,
                        ),
                    ).rowcount
                    == 1
                )

        accepted = await self._writer.run(save)
        if not accepted:
            source = current_source(self._conn, transfer.target.dialog_id, transfer.target.message_id)
            return await self._rebind_same_photo(selected, transfer.scope, transfer.target, source)
        revision = f"{transfer.attempt}:{transfer.source.generation}:{offset}"
        return SliceOutcome(selected, SliceOutcomeKind.PROGRESSED, revision)

    def _current(self, conn: sqlite3.Connection, transfer: _Transfer) -> bool:
        return (
            bound_profile_account(conn) == transfer.scope.scope.account_id
            and current_source(conn, transfer.target.dialog_id, transfer.target.message_id) == transfer.source
            and self._scope.is_current(transfer.scope)
        )

    async def _publish(self, selected: WorkRevision, transfer: _Transfer) -> SliceOutcome:
        artifact = await asyncio.to_thread(
            publish_image, self._directory, transfer.scope.scope.account_id, bytes(transfer.data)
        )

        def commit(conn: sqlite3.Connection) -> bool:
            with write_transaction(conn):
                if not self._current(conn, transfer):
                    return False
                self._scope.sources.revoke(
                    transfer.scope.scope.account_id,
                    transfer.target.dialog_id,
                    transfer.target.message_id,
                    transfer.attempt,
                )
                changed = conn.execute(
                    "UPDATE message_image_jobs SET state='complete',offset=? WHERE account_id=? AND dialog_id=? "
                    "AND message_id=? AND attempt_generation=? AND source_generation=? AND state='running'",
                    (
                        artifact.byte_size,
                        transfer.scope.scope.account_id,
                        transfer.target.dialog_id,
                        transfer.target.message_id,
                        transfer.attempt,
                        transfer.source.generation,
                    ),
                ).rowcount
                if changed != 1:
                    return False
                conn.execute(
                    "INSERT OR REPLACE INTO message_image_artifacts VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        transfer.scope.scope.account_id,
                        transfer.target.dialog_id,
                        transfer.target.message_id,
                        transfer.locator.photo_id,
                        transfer.source.generation,
                        artifact.filename,
                        artifact.sha256,
                        artifact.byte_size,
                        transfer.locator.width,
                        transfer.locator.height,
                    ),
                )
                return True

        receipt = [False]

        def commit_with_receipt(conn: sqlite3.Connection) -> bool:
            committed = commit(conn)
            receipt[0] = committed
            return committed

        try:
            committed = await self._writer.run(commit_with_receipt)
        except BaseException:
            if not receipt[0]:
                await asyncio.to_thread(
                    (self._directory / str(transfer.scope.scope.account_id) / artifact.filename).unlink, missing_ok=True
                )
            raise
        if not committed:
            await asyncio.to_thread(
                (self._directory / str(transfer.scope.scope.account_id) / artifact.filename).unlink, missing_ok=True
            )
        if not committed:
            source = current_source(self._conn, transfer.target.dialog_id, transfer.target.message_id)
            return await self._rebind_same_photo(selected, transfer.scope, transfer.target, source)
        await self.close()
        return SliceOutcome(
            selected, SliceOutcomeKind.TERMINAL, f"complete:{transfer.attempt}:{transfer.source.generation}"
        )

    async def _fail(self, selected: WorkRevision, scope: ScopeToken, *, reason: str) -> SliceOutcome:
        dialog_id, message_id = (int(part) for part in selected.key.target.split(":"))
        attempt = int(selected.revision.split(":", 1)[0])

        def fail(conn: sqlite3.Connection) -> bool:
            with write_transaction(conn):
                if bound_profile_account(conn) != scope.scope.account_id or not self._scope.is_current(scope):
                    return False
                self._scope.sources.revoke(scope.scope.account_id, dialog_id, message_id, attempt)
                return (
                    conn.execute(
                        "UPDATE message_image_jobs SET state='failed',reason=? "
                        "WHERE account_id=? AND dialog_id=? AND message_id=? AND attempt_generation=? "
                        "AND state IN ('queued','running')",
                        (reason, selected.key.account_id, dialog_id, message_id, attempt),
                    ).rowcount
                    == 1
                )

        if not await self._writer.run(fail):
            return SliceOutcome(selected, SliceOutcomeKind.STALE)
        return SliceOutcome(selected, SliceOutcomeKind.TERMINAL, f"failed:{attempt}")

    async def close(self, *, preserve_resume: bool = False) -> None:
        self._revoke_opening()
        if not preserve_resume:
            self._resume = None
        transfer, self._active = self._active, None
        if transfer is not None:
            self._scope.sources.revoke(
                transfer.scope.scope.account_id, transfer.target.dialog_id, transfer.target.message_id, transfer.attempt
            )
            await transfer.session.close()

    def _revoke_opening(self) -> None:
        opening, self._opening = self._opening, None
        if opening is not None:
            self._scope.sources.revoke(*opening)

    async def reconcile_files(self) -> None:
        """Reconcile only this verified account's artifacts before worker startup."""
        scope = self._scope.capture()
        if scope is None or bound_profile_account(self._conn) != scope.scope.account_id:
            raise RuntimeError("verified account unavailable")
        rows = cast(
            list[tuple[str]],
            self._conn.execute(
                "SELECT filename FROM message_image_artifacts WHERE account_id=?", (scope.scope.account_id,)
            ).fetchall(),
        )
        try:
            await asyncio.to_thread(
                cleanup_unpublished, self._directory, scope.scope.account_id, frozenset(row[0] for row in rows)
            )
        except OSError:
            raise RuntimeError("image storage unavailable") from None
