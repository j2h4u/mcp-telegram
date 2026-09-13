"""The single owner of account-wide dialog-directory acquisition and publication."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol, cast

from telethon.tl import types  # type: ignore[import-untyped]
from telethon.tl.types import (  # type: ignore[import-untyped]
    InputPeerChannel,
    InputPeerChat,
    InputPeerSelf,
    InputPeerUser,
)

from .access_lifecycle import not_access_lost_sql, unhide_after_realtime_presence
from .dialog_classification import EntityKind, classify_dialog_type
from .dialog_directory_tl import (
    DialogCursor,
    RawDialogFact,
    RawDialogPage,
    get_dialogs_request,
    get_pinned_dialogs_request,
    normalize_dialogs_response,
    normalize_pinned_dialogs_response,
)
from .flood import TelegramRpcThrottled
from .folders.sqlite_repository import SQLiteFolderSnapshotRepository
from .read_state import apply_read_cursor
from .sync_db import _open_sync_db
from .telegram_demand import (
    AcquisitionKind,
    DemandStatus,
    RpcAttemptBudget,
    RpcAttemptBudgetExhaustedError,
    acquisition_context,
    demand_context,
)
from .telegram_rpc_consumers import DemandKind, demand_freshness_seconds
from .telegram_rpc_scheduler import (
    RpcAdmissionClosedError,
    TelegramRpcAdmissionDeferred,
    TelegramRpcSource,
    rpc_attempt_budget,
    rpc_scope,
)

logger = logging.getLogger(__name__)


class DialogDirectoryClient(Protocol):
    async def __call__(self, request: object) -> object: ...

    async def get_me(self) -> object: ...


@dataclass(frozen=True, slots=True)
class _DirectoryState:
    generation: int
    status: str
    ordinary_status: str
    pinned_main_status: str
    pinned_archive_status: str
    cursor: DialogCursor | None
    observation_started_at: int | None
    observation_completed_at: int | None
    account_id: int | None
    retry_at: int | None
    reason: str | None
    cursor_error: bool = False


@dataclass(frozen=True, slots=True)
class _StagedFact:
    dialog_id: int
    source: str
    peer_kind: str
    top_message: int
    name: str | None
    dialog_type: str
    archived: int
    pinned: int
    members: int | None
    created: int | None
    last_message_at: int | None
    read_inbox_max_id: int | None
    read_outbox_max_id: int | None
    unread_mentions_count: int
    unread_reactions_count: int
    unread_count: int | None
    unread_mark: int | None
    draft_text: str | None
    snapshot_at: int
    username: str | None
    identity_complete: int
    identity_source: str
    eligibility_category: str | None
    eligibility_archived: int | None
    eligibility_unread: int | None
    eligibility_mute_until: int | None


@dataclass(frozen=True, slots=True)
class _StagedEntityProjection:
    name: str | None
    dialog_type: str
    members: int | None
    created: int | None
    username: str | None
    identity_complete: int
    eligibility_category: str | None


@dataclass(frozen=True, slots=True)
class _StagedDialogProjection:
    peer_kind: str
    top_message: int
    archived: int
    pinned: int
    last_message_at: int | None
    read_inbox_max_id: int | None
    read_outbox_max_id: int | None
    unread_mentions_count: int
    unread_reactions_count: int
    unread_count: int | None
    unread_mark: int | None
    draft_text: str | None
    snapshot_at: int
    unread: int | None
    mute_until: int | None


class _IdentityOmitted:
    """Distinguish a missing realtime field from an authoritative clear."""


IDENTITY_OMITTED = _IdentityOmitted()


@dataclass(frozen=True, slots=True)
class _RealtimeIdentity:
    name: str | _IdentityOmitted | None
    username: str | _IdentityOmitted | None
    dialog_type: str | _IdentityOmitted | None
    observed_at: int


def _publication_ready(state: _DirectoryState, generation: int, account_id: int) -> bool:
    return (
        state.generation == generation
        and state.account_id == account_id
        and state.status == "in_progress"
        and state.ordinary_status == "complete"
        and state.pinned_main_status == "complete"
        and state.pinned_archive_status == "complete"
    )


def _staged_entity_projection(entity: object | None) -> _StagedEntityProjection:
    kind = EntityKind.UNKNOWN
    if isinstance(entity, types.User):
        kind = EntityKind.USER
    elif isinstance(entity, (types.Chat, types.ChatForbidden)):
        kind = EntityKind.CHAT
    elif isinstance(entity, (types.Channel, types.ChannelForbidden)):
        kind = EntityKind.CHANNEL
    identity_complete = int(_identity_is_complete(entity))
    return _StagedEntityProjection(
        name=_entity_name(entity),
        dialog_type=classify_dialog_type(entity, entity_kind=kind).value if identity_complete else "unknown",
        members=_nullable_int(getattr(entity, "participants_count", None)),
        created=int(entity.date.timestamp()) if isinstance(entity, types.Channel) and entity.date is not None else None,
        username=_primary_username(entity),
        identity_complete=identity_complete,
        eligibility_category=_eligibility_category(entity),
    )


def _staged_dialog_projection(fact: RawDialogFact, observed_at: int, folder_id: int | None) -> _StagedDialogProjection:
    raw = cast(types.Dialog, fact.dialog)
    archived = int(folder_id == 1 or getattr(raw, "folder_id", None) == 1)
    return _StagedDialogProjection(
        peer_kind=type(raw.peer).__name__,
        top_message=int(raw.top_message),
        archived=archived,
        pinned=int(folder_id is not None or bool(getattr(raw, "pinned", False))),
        last_message_at=int(fact.top_message_date.timestamp()) if fact.top_message_date is not None else None,
        read_inbox_max_id=_nullable_int(getattr(raw, "read_inbox_max_id", None)),
        read_outbox_max_id=_nullable_int(getattr(raw, "read_outbox_max_id", None)),
        unread_mentions_count=_nullable_int(getattr(raw, "unread_mentions_count", None)) or 0,
        unread_reactions_count=_nullable_int(getattr(raw, "unread_reactions_count", None)) or 0,
        unread_count=_nullable_int(getattr(raw, "unread_count", None)),
        unread_mark=(
            int(bool(getattr(raw, "unread_mark", False))) if getattr(raw, "unread_mark", None) is not None else None
        ),
        draft_text=_draft_text(getattr(raw, "draft", None)),
        snapshot_at=observed_at,
        unread=_three_valued_unread(raw),
        mute_until=_mute_until(getattr(raw, "notify_settings", None)),
    )


class CanonicalDialogDirectory:
    """Acquire raw dialog sources into one generation and atomically publish it.

    A call performs one raw RPC. That keeps scheduler admission bounded while
    retaining the page-and-cursor transaction required for crash-safe resume.
    """

    def __init__(
        self,
        client: object,
        db_path: Path,
        shutdown_event: asyncio.Event,
        *,
        startup_detail_setter: Callable[[str], None] | None = None,
    ) -> None:
        self._client = cast(DialogDirectoryClient, client)
        self._db_path = db_path
        self._shutdown_event = shutdown_event
        self._startup_detail_setter = startup_detail_setter

    def status(self, now: float, conn: sqlite3.Connection) -> DemandStatus | None:
        """Expose the strictest current eventual freshness target (900 seconds)."""
        state = self._load_state(conn)
        if state.cursor_error:
            return DemandStatus(release_at=0.0)
        if state.retry_at is not None and now < state.retry_at:
            return DemandStatus(release_at=float(state.retry_at))
        if state.status == "invalid":
            return None
        if state.status != "complete" or state.observation_started_at is None:
            return DemandStatus(release_at=0.0)
        interval = demand_freshness_seconds(DemandKind.DIALOG_BOOTSTRAP)
        # A long-running acquisition may already be stale at publication. The
        # completion time is a receipt timestamp, never a freshness rewrite.
        release_at = state.observation_started_at + interval
        return DemandStatus(release_at=release_at, freshness_deadline=release_at)

    async def run_slice(self) -> None:
        """Run exactly one pinned or ordinary raw request, then commit its result."""
        if self._shutdown_event.is_set():
            return
        conn = _open_sync_db(self._db_path)
        try:
            initial_state = self._load_state(conn)
            if initial_state.cursor_error:
                with conn:
                    conn.execute(
                        "UPDATE dialog_directory_state SET status='invalid',ordinary_status='invalid',"
                        "reason='ordinary:invalid:corrupt_cursor',retry_at=NULL WHERE singleton=1 AND generation=?",
                        (initial_state.generation,),
                    )
                return
            if initial_state.status == "invalid":
                return
            account_id = await self._bound_account_id(conn)
            state = self._prepare_or_resume(conn)
            if state is None or self._shutdown_event.is_set():
                return
            if state.pinned_main_status != "complete":
                self._set_detail("canonical dialog directory: pinned main")
                await self._acquire_pinned(conn, state, 0, account_id)
                return
            if state.pinned_archive_status != "complete":
                self._set_detail("canonical dialog directory: pinned archive")
                await self._acquire_pinned(conn, state, 1, account_id)
                return
            if state.ordinary_status != "complete":
                self._set_detail("canonical dialog directory: ordinary page")
                await self._acquire_ordinary(conn, state, account_id)
        finally:
            conn.close()

    def _set_detail(self, detail: str) -> None:
        if self._startup_detail_setter is not None:
            self._startup_detail_setter(detail)

    def bind_account_id(self, account_id: int) -> None:
        """Fence the directory to the authenticated account before readers start."""
        conn = _open_sync_db(self._db_path)
        try:
            self._bind_account(conn, account_id)
        finally:
            conn.close()

    async def _authenticated_account_id(self) -> int:
        profile = await self._client.get_me()
        return _account_id_from_profile(profile)

    async def _bound_account_id(self, conn: sqlite3.Connection) -> int:
        """Use the durable account fence without an identity RPC on every page."""
        state = self._load_state(conn)
        if state.account_id is None:
            self._bind_account(conn, await self._authenticated_account_id())
            state = self._load_state(conn)
        if state.account_id is None:
            raise RuntimeError("dialog directory account identity is missing")
        return state.account_id

    def _bind_account(self, conn: sqlite3.Connection, account_id: int) -> None:
        state = self._load_state(conn)
        if state.account_id is not None and state.account_id != account_id:
            raise RuntimeError("dialog directory account identity changed")
        if state.account_id is None:
            with conn:
                conn.execute(
                    "UPDATE dialog_directory_state SET account_id=? WHERE singleton=1 AND account_id IS NULL",
                    (account_id,),
                )

    @staticmethod
    def _propagate_policy_exception(exc: Exception) -> None:
        if isinstance(
            exc,
            (
                TelegramRpcThrottled,
                TelegramRpcAdmissionDeferred,
                RpcAttemptBudgetExhaustedError,
                RpcAdmissionClosedError,
                asyncio.CancelledError,
            ),
        ):
            raise exc

    def _prepare_or_resume(self, conn: sqlite3.Connection) -> _DirectoryState | None:
        state = self._load_state(conn)
        if state.retry_at is not None and time.time() < state.retry_at:
            return None
        if state.status == "invalid":
            return None
        if state.status == "complete":
            # The timestamp is original observation time. Starting a new
            # generation never refreshes it until a complete publication.
            with conn:
                self._start_generation(conn, state.generation + 1)
            return self._load_state(conn)
        if state.status == "pending":
            with conn:
                self._start_generation(conn, state.generation)
            return self._load_state(conn)
        if state.status == "incomplete":
            with conn:
                conn.execute("UPDATE dialog_directory_state SET status='in_progress', reason=NULL WHERE singleton=1")
            return self._load_state(conn)
        return state

    @staticmethod
    def _start_generation(conn: sqlite3.Connection, generation: int) -> None:
        started_at = int(time.time())
        conn.execute("DELETE FROM dialog_directory_staging")
        conn.execute("DELETE FROM dialog_directory_baseline")
        conn.execute("DELETE FROM dialog_directory_pins")
        conn.execute("DELETE FROM daemon_state WHERE key LIKE 'dialog_directory_pin_fence:%'")
        # Realtime pin deltas received before a source RPC completes must
        # build on the active generation's coherent prior publication.  The
        # acquisition transaction may replace this snapshot when no realtime
        # writer crossed its fence.
        conn.execute(
            "INSERT INTO dialog_directory_pins(generation,folder_id,dialog_id,position) "
            "SELECT ?,folder_id,dialog_id,position FROM dialog_directory_published_pins",
            (generation,),
        )
        conn.execute(
            "INSERT INTO dialog_directory_baseline(generation, dialog_id, baseline_revision, seen) "
            "SELECT ?, dialog_id, revision, 0 FROM dialogs",
            (generation,),
        )
        # These keys describe the last *published* directory observation.
        # Starting (or later failing) a generation records only its attempt;
        # callers keep seeing the coherent prior publication until replacement.
        conn.execute(
            "INSERT OR REPLACE INTO daemon_state(key,value) VALUES ('dialog_unread_sweep_attempted_at',?)",
            (str(started_at),),
        )
        conn.execute(
            "UPDATE dialog_directory_state SET generation=?, status='in_progress', ordinary_status='pending', "
            "pinned_main_status='pending', pinned_archive_status='pending', offset_date=NULL, offset_id=0, "
            "offset_peer=NULL, observation_started_at=?, observation_completed_at=NULL, observed_count=0, reason=NULL, retry_at=NULL "
            "WHERE singleton=1",
            (generation, started_at),
        )

    async def _acquire_pinned(
        self, conn: sqlite3.Connection, state: _DirectoryState, folder_id: int, account_id: int
    ) -> None:
        try:
            response = await self._client(get_pinned_dialogs_request(folder_id))
        except Exception as exc:  # noqa: BLE001 - transport classes vary under Telethon
            self._propagate_policy_exception(exc)
            self._mark_source_incomplete(
                conn, state.generation, f"pinned_{folder_id}:{type(exc).__name__}", account_id, source_column=None
            )
            return
        page = normalize_pinned_dialogs_response(response)
        if page.kind == "invalid":
            self._latch_semantic_invalid(
                conn, state.generation, f"pinned_{folder_id}", page.reason or "invalid", account_id
            )
            return
        if page.kind == "not_modified":
            self._mark_not_modified_without_cache(
                conn,
                state.generation,
                f"pinned_{folder_id}:not_modified_without_cache",
                account_id,
                source_column="pinned_main_status" if folder_id == 0 else "pinned_archive_status",
            )
            return
        if page.kind not in {"terminal", "page"}:
            raise RuntimeError(f"unexpected normalized pinned response: {page.kind}")
        with conn:
            self._stage_facts(conn, state.generation, page.facts, folder_id=folder_id, account_id=account_id)
            _commit_pinned_generation(
                conn,
                state.generation,
                folder_id,
                [fact.dialog_id for fact in page.facts],
            )
            column = "pinned_main_status" if folder_id == 0 else "pinned_archive_status"
            conn.execute(
                f"UPDATE dialog_directory_state SET {column}='complete', retry_at=NULL WHERE singleton=1 AND account_id=? AND generation=?",
                (account_id, state.generation),
            )

    async def _acquire_ordinary(self, conn: sqlite3.Connection, state: _DirectoryState, account_id: int) -> None:
        try:
            response = await self._client(get_dialogs_request(state.cursor))
        except Exception as exc:  # noqa: BLE001 - transport classes vary under Telethon
            self._propagate_policy_exception(exc)
            self._mark_source_incomplete(
                conn, state.generation, f"ordinary:{type(exc).__name__}", account_id, source_column=None
            )
            return
        page = normalize_dialogs_response(response, state.cursor)
        if self._handle_ordinary_non_authoritative(conn, state, account_id, page):
            return
        published = False
        with conn:
            new_ids = self._new_dialog_ids(conn, state.generation, page.facts)
            self._stage_facts(conn, state.generation, page.facts, folder_id=None, account_id=account_id)
            if page.kind == "page":
                if page.cursor is None:
                    raise RuntimeError("ordinary page cannot commit without a cursor")
                self._save_cursor(conn, page.cursor, state.generation, account_id)
                if not new_ids:
                    conn.execute(
                        "UPDATE dialog_directory_state SET status='incomplete', ordinary_status='incomplete', "
                        "reason='pagination_no_new_dialogs', retry_at=? "
                        "WHERE singleton=1 AND account_id=? AND generation=?",
                        (
                            int(time.time()) + demand_freshness_seconds(DemandKind.DIALOG_BOOTSTRAP),
                            account_id,
                            state.generation,
                        ),
                    )
                    return
                conn.execute(
                    "UPDATE dialog_directory_state SET observed_count=observed_count+?, reason=NULL, retry_at=NULL "
                    "WHERE singleton=1 AND account_id=? AND generation=?",
                    (len(page.facts), account_id, state.generation),
                )
                return
            conn.execute(
                "UPDATE dialog_directory_state SET ordinary_status='complete', observed_count=observed_count+?, reason=NULL "
                "WHERE singleton=1 AND account_id=? AND generation=?",
                (len(page.facts), account_id, state.generation),
            )
            published = self._publish_if_complete(conn, state.generation, account_id)
        if published:
            self._set_detail("canonical dialog directory: complete")

    def _handle_ordinary_non_authoritative(
        self, conn: sqlite3.Connection, state: _DirectoryState, account_id: int, page: RawDialogPage
    ) -> bool:
        if page.kind == "invalid":
            self._latch_semantic_invalid(conn, state.generation, "ordinary", page.reason or "invalid", account_id)
            return True
        if page.kind == "not_modified":
            self._mark_not_modified_without_cache(
                conn,
                state.generation,
                "ordinary:not_modified_without_cache",
                account_id,
                source_column="ordinary_status",
            )
            return True
        if page.kind != "incomplete":
            return False
        reason = page.reason or "incomplete_page"
        with conn:
            # An incomplete page may still contribute catalog rows.
            # It never advances the committed cursor or publishes.
            self._stage_facts(conn, state.generation, page.facts, folder_id=None, account_id=account_id)
            conn.execute(
                "UPDATE dialog_directory_state SET status='incomplete', ordinary_status='incomplete', reason=?, retry_at=? "
                "WHERE singleton=1 AND account_id=? AND generation=?",
                (reason, int(time.time()) + 900, account_id, state.generation),
            )
        return True

    def _mark_source_incomplete(
        self,
        conn: sqlite3.Connection,
        generation: int,
        reason: str,
        account_id: int,
        *,
        source_column: str | None,
    ) -> None:
        source_update = "" if source_column is None else f", {source_column}='incomplete'"
        with conn:
            conn.execute(
                f"UPDATE dialog_directory_state SET status='incomplete'{source_update}, reason=?, retry_at=? "
                "WHERE singleton=1 AND account_id=? AND generation=?",
                (reason, int(time.time()) + 60, account_id, generation),
            )
        self._set_detail(f"canonical dialog directory: retry ({reason})")

    def _mark_not_modified_without_cache(
        self,
        conn: sqlite3.Connection,
        generation: int,
        reason: str,
        account_id: int,
        *,
        source_column: str,
    ) -> None:
        with conn:
            conn.execute(
                f"UPDATE dialog_directory_state SET status='incomplete', {source_column}='incomplete', reason=?, retry_at=? "
                "WHERE singleton=1 AND account_id=? AND generation=?",
                (reason, int(time.time()) + 900, account_id, generation),
            )
        self._set_detail(f"canonical dialog directory: retry ({reason})")

    def _latch_semantic_invalid(
        self, conn: sqlite3.Connection, generation: int, source: str, specific: str, account_id: int
    ) -> None:
        """Latch an impossible source outcome without destroying prior work."""
        column = {
            "ordinary": "ordinary_status",
            "pinned_0": "pinned_main_status",
            "pinned_1": "pinned_archive_status",
        }.get(source)
        if column is None:
            raise ValueError(f"unknown directory source: {source}")
        with conn:
            cursor = conn.execute(
                f"UPDATE dialog_directory_state SET status='invalid', {column}='invalid', reason=?, retry_at=NULL "
                "WHERE singleton=1 AND account_id=? AND generation=? AND status != 'invalid'",
                (f"{source}:invalid:{specific}", account_id, generation),
            )
            first_latch = cursor.rowcount == 1
        if first_latch:
            logger.warning(
                "canonical_dialog_directory_semantic_invalid generation=%d source=%s reason=%s",
                generation,
                source,
                specific,
            )
        self._set_detail(f"canonical dialog directory: invalid {source} ({specific})")

    def _stage_facts(
        self,
        conn: sqlite3.Connection,
        generation: int,
        facts: tuple[RawDialogFact, ...],
        *,
        folder_id: int | None,
        account_id: int,
    ) -> None:
        state = self._load_state(conn)
        observed_at = state.observation_started_at
        if observed_at is None:
            raise RuntimeError("directory generation has no observation start")
        if state.generation != generation or state.account_id != account_id or state.status != "in_progress":
            raise RuntimeError("stale_directory_page")
        source = "ordinary" if folder_id is None else f"pinned:{folder_id}"
        rows = [self._staged_fact(fact, observed_at, folder_id, source) for fact in facts]
        for row in rows:
            baseline = cast(
                tuple[object] | None,
                conn.execute(
                    "SELECT baseline_revision FROM dialog_directory_baseline WHERE generation=? AND dialog_id=?",
                    (generation, row.dialog_id),
                ).fetchone(),
            )
            baseline_revision = _required_int(baseline[0], "baseline revision") if baseline is not None else None
            conn.execute(
                "INSERT INTO dialog_directory_staging("
                "generation,dialog_id,source,peer_kind,top_message,name,type,archived,pinned,members,created,last_message_at,read_inbox_max_id,read_outbox_max_id,unread_mentions_count,unread_reactions_count,unread_count,unread_mark,draft_text,snapshot_at,baseline_revision,username,identity_observed_at,identity_complete,identity_source,eligibility_category,eligibility_archived,eligibility_unread,eligibility_mute_until,eligibility_observed_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(generation,dialog_id) DO UPDATE SET "
                "source=excluded.source,peer_kind=excluded.peer_kind,top_message=excluded.top_message,"
                "name=excluded.name,type=excluded.type,archived=excluded.archived,pinned=excluded.pinned,members=excluded.members,"
                "created=excluded.created,last_message_at=excluded.last_message_at,read_inbox_max_id=excluded.read_inbox_max_id,"
                "read_outbox_max_id=excluded.read_outbox_max_id,unread_mentions_count=excluded.unread_mentions_count,"
                "unread_reactions_count=excluded.unread_reactions_count,unread_count=excluded.unread_count,"
                "unread_mark=excluded.unread_mark,draft_text=excluded.draft_text,snapshot_at=excluded.snapshot_at,"
                "baseline_revision=excluded.baseline_revision,username=excluded.username,"
                "identity_observed_at=excluded.identity_observed_at,identity_complete=excluded.identity_complete,"
                "identity_source=excluded.identity_source,eligibility_category=excluded.eligibility_category,"
                "eligibility_archived=excluded.eligibility_archived,eligibility_unread=excluded.eligibility_unread,"
                "eligibility_mute_until=excluded.eligibility_mute_until,eligibility_observed_at=excluded.eligibility_observed_at",
                (
                    generation,
                    row.dialog_id,
                    row.source,
                    row.peer_kind,
                    row.top_message,
                    row.name,
                    row.dialog_type,
                    row.archived,
                    row.pinned,
                    row.members,
                    row.created,
                    row.last_message_at,
                    row.read_inbox_max_id,
                    row.read_outbox_max_id,
                    row.unread_mentions_count,
                    row.unread_reactions_count,
                    row.unread_count,
                    row.unread_mark,
                    row.draft_text,
                    row.snapshot_at,
                    baseline_revision,
                    row.username,
                    row.snapshot_at,
                    row.identity_complete,
                    row.identity_source,
                    row.eligibility_category,
                    row.eligibility_archived,
                    row.eligibility_unread,
                    row.eligibility_mute_until,
                    row.snapshot_at,
                ),
            )
            conn.execute(
                "UPDATE dialog_directory_baseline SET seen=1 WHERE generation=? AND dialog_id=?",
                (generation, row.dialog_id),
            )

    @staticmethod
    def _new_dialog_ids(conn: sqlite3.Connection, generation: int, facts: tuple[RawDialogFact, ...]) -> set[int]:
        staged_ids = {
            _required_int(row[0], "staged dialog id")
            for row in cast(
                list[tuple[object]],
                conn.execute(
                    "SELECT dialog_id FROM dialog_directory_staging WHERE generation=? AND source='ordinary'",
                    (generation,),
                ).fetchall(),
            )
        }
        return {fact.dialog_id for fact in facts if fact.dialog_id not in staged_ids}

    def _staged_fact(self, fact: RawDialogFact, observed_at: int, folder_id: int | None, source: str) -> _StagedFact:
        entity = _staged_entity_projection(fact.entity)
        dialog = _staged_dialog_projection(fact, observed_at, folder_id)
        return _StagedFact(
            fact.dialog_id,
            source,
            dialog.peer_kind,
            dialog.top_message,
            entity.name,
            entity.dialog_type,
            dialog.archived,
            dialog.pinned,
            entity.members,
            entity.created,
            dialog.last_message_at,
            dialog.read_inbox_max_id,
            dialog.read_outbox_max_id,
            dialog.unread_mentions_count,
            dialog.unread_reactions_count,
            dialog.unread_count,
            dialog.unread_mark,
            dialog.draft_text,
            dialog.snapshot_at,
            entity.username,
            entity.identity_complete,
            "directory",
            entity.eligibility_category,
            dialog.archived,
            dialog.unread,
            dialog.mute_until,
        )

    def _publish_if_complete(self, conn: sqlite3.Connection, generation: int, account_id: int) -> bool:
        state = self._load_state(conn)
        if not _publication_ready(state, generation, account_id):
            return False
        # Eligibility facts must merge before the dialogs UPDATE below. That
        # UPDATE fires the revision trigger, so doing this afterwards would
        # make the pre-acquisition revision fence reject every existing row.
        # Realtime always wins when its revision already differs.
        conn.execute(
            "UPDATE dialog_directory_facts AS current SET "
            "category=CASE WHEN staged.eligibility_category IS NULL THEN current.category ELSE staged.eligibility_category END, "
            "archived=CASE WHEN staged.eligibility_archived IS NULL THEN current.archived ELSE staged.eligibility_archived END, "
            "unread=CASE WHEN staged.eligibility_unread IS NULL THEN current.unread ELSE staged.eligibility_unread END, "
            "mute_until=CASE WHEN staged.eligibility_mute_until IS NULL THEN current.mute_until ELSE staged.eligibility_mute_until END, "
            "observed_at=CASE WHEN staged.eligibility_category IS NULL AND staged.eligibility_archived IS NULL "
            "AND staged.eligibility_unread IS NULL AND staged.eligibility_mute_until IS NULL THEN current.observed_at "
            "WHEN current.observed_at IS NULL THEN staged.eligibility_observed_at "
            "WHEN staged.eligibility_observed_at IS NULL THEN current.observed_at "
            "ELSE MIN(current.observed_at,staged.eligibility_observed_at) END "
            "FROM dialog_directory_staging AS staged JOIN dialogs AS dialog ON dialog.dialog_id=staged.dialog_id "
            "WHERE staged.generation=? AND staged.dialog_id=current.dialog_id AND staged.baseline_revision=dialog.revision",
            (generation,),
        )
        # A previously hidden row can be re-seen after absence handling
        # removed its facts row. Restore that row under the same revision fence.
        conn.execute(
            "INSERT INTO dialog_directory_facts(dialog_id,category,archived,unread,mute_until,observed_at) "
            "SELECT staged.dialog_id,staged.eligibility_category,staged.eligibility_archived,staged.eligibility_unread,"
            "staged.eligibility_mute_until,staged.eligibility_observed_at FROM dialog_directory_staging AS staged "
            "JOIN dialogs AS dialog ON dialog.dialog_id=staged.dialog_id "
            "WHERE staged.generation=? AND staged.baseline_revision=dialog.revision "
            "AND NOT EXISTS (SELECT 1 FROM dialog_directory_facts current WHERE current.dialog_id=staged.dialog_id)",
            (generation,),
        )
        # Positive dialog facts are fenced by the revision captured before RPC.
        conn.execute(
            "UPDATE dialogs AS current SET "
            "name=CASE WHEN staged.identity_complete=1 THEN staged.name "
            "WHEN current.identity_complete=1 THEN current.name ELSE COALESCE(current.name,staged.name) END, "
            "type=CASE WHEN staged.identity_complete=1 THEN staged.type "
            "WHEN current.identity_complete=1 THEN current.type ELSE COALESCE(NULLIF(current.type,'unknown'),NULLIF(staged.type,'unknown'),'unknown') END, "
            "username=CASE WHEN staged.identity_complete=1 THEN staged.username "
            "WHEN current.identity_complete=1 THEN current.username ELSE COALESCE(current.username,staged.username) END, "
            "identity_complete=CASE WHEN staged.identity_complete=1 THEN 1 ELSE current.identity_complete END, "
            "identity_source=CASE WHEN staged.identity_complete=1 THEN staged.identity_source "
            "WHEN current.identity_complete=1 AND (staged.name IS NOT NULL OR staged.username IS NOT NULL) THEN 'mixed' "
            "WHEN current.identity_complete=1 THEN current.identity_source "
            "WHEN current.identity_source IS NULL AND current.name IS NULL AND current.username IS NULL "
            "AND (current.type IS NULL OR current.type='unknown') THEN staged.identity_source "
            "WHEN current.identity_source IS NULL THEN 'mixed' "
            "WHEN staged.name IS NOT NULL OR staged.username IS NOT NULL THEN 'mixed' ELSE current.identity_source END, "
            "identity_observed_at=CASE WHEN staged.identity_complete=1 THEN staged.identity_observed_at "
            "WHEN staged.name IS NULL AND staged.username IS NULL THEN current.identity_observed_at "
            "WHEN current.identity_observed_at IS NULL THEN staged.identity_observed_at "
            "WHEN staged.identity_observed_at IS NULL THEN current.identity_observed_at "
            "ELSE MIN(current.identity_observed_at,staged.identity_observed_at) END, "
            "archived=staged.archived, "
            "pinned=CASE WHEN EXISTS (SELECT 1 FROM dialog_directory_pins pin WHERE pin.generation=staged.generation AND pin.folder_id=0 AND pin.dialog_id=staged.dialog_id) THEN 1 ELSE 0 END, "
            "members=staged.members, created=staged.created, last_message_at=staged.last_message_at, "
            "snapshot_at=staged.snapshot_at, unread_mentions_count=staged.unread_mentions_count, "
            "unread_reactions_count=staged.unread_reactions_count, draft_text=staged.draft_text, "
            "read_inbox_max_id=CASE WHEN staged.read_inbox_max_id IS NULL THEN current.read_inbox_max_id "
            "WHEN current.read_inbox_max_id IS NULL THEN staged.read_inbox_max_id "
            "ELSE MAX(current.read_inbox_max_id, staged.read_inbox_max_id) END, "
            "read_outbox_max_id=CASE WHEN staged.read_outbox_max_id IS NULL THEN current.read_outbox_max_id "
            "WHEN current.read_outbox_max_id IS NULL THEN staged.read_outbox_max_id "
            "ELSE MAX(current.read_outbox_max_id, staged.read_outbox_max_id) END, "
            "unread_count=CASE WHEN staged.unread_count IS NOT NULL AND (current.unread_count_observed_at IS NULL OR current.unread_count_observed_at < staged.snapshot_at) THEN staged.unread_count ELSE current.unread_count END, "
            "unread_count_observed_at=CASE WHEN staged.unread_count IS NOT NULL AND (current.unread_count_observed_at IS NULL OR current.unread_count_observed_at < staged.snapshot_at) THEN staged.snapshot_at ELSE current.unread_count_observed_at END, "
            "unread_mark=CASE WHEN staged.unread_mark IS NOT NULL AND (current.unread_mark_observed_at IS NULL OR current.unread_mark_observed_at < staged.snapshot_at) THEN staged.unread_mark ELSE current.unread_mark END, "
            "unread_mark_observed_at=CASE WHEN staged.unread_mark IS NOT NULL AND (current.unread_mark_observed_at IS NULL OR current.unread_mark_observed_at < staged.snapshot_at) THEN staged.snapshot_at ELSE current.unread_mark_observed_at END, "
            f"hidden=CASE WHEN {not_access_lost_sql('current.dialog_id')} THEN 0 ELSE current.hidden END "
            "FROM dialog_directory_staging AS staged WHERE staged.generation=? AND staged.dialog_id=current.dialog_id "
            "AND staged.baseline_revision=current.revision",
            (generation,),
        )
        conn.execute(
            "INSERT INTO dialogs(dialog_id,name,type,username,identity_observed_at,identity_complete,identity_source,archived,pinned,members,created,last_message_at,snapshot_at,hidden,needs_refresh,unread_mentions_count,unread_reactions_count,unread_count,unread_mark,unread_count_observed_at,unread_mark_observed_at,draft_text,read_inbox_max_id,read_outbox_max_id) "
            "SELECT staged.dialog_id,staged.name,staged.type,staged.username,staged.identity_observed_at,staged.identity_complete,staged.identity_source,staged.archived,"
            "CASE WHEN EXISTS (SELECT 1 FROM dialog_directory_pins pin WHERE pin.generation=staged.generation AND pin.folder_id=0 AND pin.dialog_id=staged.dialog_id) THEN 1 ELSE 0 END,"
            "staged.members,staged.created,staged.last_message_at,"
            "staged.snapshot_at,0,0,staged.unread_mentions_count,staged.unread_reactions_count,staged.unread_count,staged.unread_mark,"
            "CASE WHEN staged.unread_count IS NULL THEN NULL ELSE staged.snapshot_at END,"
            "CASE WHEN staged.unread_mark IS NULL THEN NULL ELSE staged.snapshot_at END,staged.draft_text,"
            "staged.read_inbox_max_id,staged.read_outbox_max_id FROM dialog_directory_staging AS staged "
            "WHERE staged.generation=? AND staged.baseline_revision IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM dialogs current WHERE current.dialog_id=staged.dialog_id) "
            f"AND {not_access_lost_sql('staged.dialog_id')}",
            (generation,),
        )
        # New rows did not have a revision to fence before insertion. Their
        # eligibility facts are inserted only after the canonical row exists.
        conn.execute(
            "INSERT INTO dialog_directory_facts(dialog_id,category,archived,unread,mute_until,observed_at) "
            "SELECT staged.dialog_id,staged.eligibility_category,staged.eligibility_archived,staged.eligibility_unread,"
            "staged.eligibility_mute_until,staged.eligibility_observed_at FROM dialog_directory_staging AS staged "
            "JOIN dialogs AS dialog ON dialog.dialog_id=staged.dialog_id "
            "WHERE staged.generation=? AND staged.baseline_revision IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM dialog_directory_facts current WHERE current.dialog_id=staged.dialog_id)",
            (generation,),
        )
        # Read cursors are monotonic facts of the completed publication. Apply
        # them even when a concurrent realtime dialog update advanced revision
        # and therefore fenced the mutable snapshot fields above.
        conn.execute(
            "UPDATE dialogs AS current SET "
            "read_inbox_max_id=CASE WHEN staged.read_inbox_max_id IS NULL THEN current.read_inbox_max_id "
            "WHEN current.read_inbox_max_id IS NULL THEN staged.read_inbox_max_id "
            "ELSE MAX(current.read_inbox_max_id, staged.read_inbox_max_id) END, "
            "read_outbox_max_id=CASE WHEN staged.read_outbox_max_id IS NULL THEN current.read_outbox_max_id "
            "WHEN current.read_outbox_max_id IS NULL THEN staged.read_outbox_max_id "
            "ELSE MAX(current.read_outbox_max_id, staged.read_outbox_max_id) END "
            "FROM dialog_directory_staging AS staged "
            "WHERE staged.generation=? AND staged.dialog_id=current.dialog_id",
            (generation,),
        )
        read_rows = cast(
            list[tuple[int, int | None, int | None]],
            conn.execute(
                "SELECT dialog_id,read_inbox_max_id,read_outbox_max_id FROM dialog_directory_staging WHERE generation=?",
                (generation,),
            ).fetchall(),
        )
        for dialog_id, inbox, outbox in read_rows:
            if inbox is not None:
                apply_read_cursor(conn, dialog_id, "inbox", inbox)
            if outbox is not None:
                apply_read_cursor(conn, dialog_id, "outbox", outbox)
        # Absence is checked after positive merge. New rows and changed rows
        # therefore survive an in-flight snapshot even when unseen by it.
        if state.observation_started_at is None:
            raise RuntimeError("complete directory generation has no observation start")
        conn.execute(
            "DELETE FROM dialog_directory_facts WHERE EXISTS ("
            "SELECT 1 FROM dialog_directory_baseline baseline JOIN dialogs current ON current.dialog_id=baseline.dialog_id "
            "WHERE baseline.generation=? AND baseline.dialog_id=dialog_directory_facts.dialog_id "
            "AND baseline.seen=0 AND baseline.baseline_revision=current.revision "
            "AND current.hidden=0)",
            (generation,),
        )
        conn.execute(
            "UPDATE dialogs AS current SET hidden=1, snapshot_at=? "
            "WHERE current.hidden=0 AND EXISTS ("
            "SELECT 1 FROM dialog_directory_baseline baseline "
            "WHERE baseline.generation=? AND baseline.dialog_id=current.dialog_id "
            "AND baseline.seen=0 AND baseline.baseline_revision=current.revision) "
            f"AND {not_access_lost_sql('current.dialog_id')}",
            (state.observation_started_at, generation),
        )
        observed_count = self._staged_count(conn, generation)
        visible_count = self._visible_count(conn)
        completed_at = int(time.time())
        conn.execute(
            "UPDATE dialog_directory_state SET status='complete', observation_completed_at=?, observed_count=?, reason=NULL, retry_at=NULL "
            "WHERE singleton=1 AND account_id=? AND generation=?",
            (completed_at, observed_count, account_id, generation),
        )
        conn.executemany(
            "INSERT OR REPLACE INTO daemon_state(key,value) VALUES (?,?)",
            (
                ("dialog_unread_sweep_status", "complete"),
                ("dialog_unread_sweep_completed_at", str(completed_at)),
                ("dialog_unread_sweep_observed_count", str(observed_count)),
                ("dialog_unread_sweep_last_visible_count", str(visible_count)),
            ),
        )
        conn.execute("DELETE FROM dialog_directory_published_pins")
        conn.execute(
            "INSERT INTO dialog_directory_published_pins(folder_id,dialog_id,position) "
            "SELECT folder_id,dialog_id,position FROM dialog_directory_pins WHERE generation=?",
            (generation,),
        )
        conn.execute("DELETE FROM daemon_state WHERE key LIKE 'dialog_directory_pin_fence:%'")
        conn.execute(
            "UPDATE dialog_directory_publication SET account_id=?,generation=?,observation_started_at=?,observation_completed_at=? WHERE singleton=1",
            (account_id, generation, state.observation_started_at, completed_at),
        )
        # Folder rules are already accepted local state.  Reproject them in
        # this transaction so readers cannot observe a new catalog with an
        # old membership generation, and do not trigger another Telegram RPC.
        SQLiteFolderSnapshotRepository(conn).reproject_current_rules_in_transaction(now=completed_at)
        conn.execute("DELETE FROM dialog_directory_staging WHERE generation=?", (generation,))
        conn.execute("DELETE FROM dialog_directory_baseline WHERE generation=?", (generation,))
        conn.execute("DELETE FROM dialog_directory_pins WHERE generation=?", (generation,))
        return True

    def _save_cursor(self, conn: sqlite3.Connection, cursor: DialogCursor, generation: int, account_id: int) -> None:
        peer_json = _encode_input_peer(cursor)
        conn.execute(
            "UPDATE dialog_directory_state SET offset_date=?, offset_id=?, offset_peer=? "
            "WHERE singleton=1 AND account_id=? AND generation=?",
            (cursor.offset_date.isoformat(), cursor.offset_id, peer_json, account_id, generation),
        )

    @staticmethod
    def _staged_count(conn: sqlite3.Connection, generation: int) -> int:
        row = cast(
            tuple[object] | None,
            conn.execute("SELECT COUNT(*) FROM dialog_directory_staging WHERE generation=?", (generation,)).fetchone(),
        )
        return _required_int(row[0], "directory staged count") if row is not None else 0

    @staticmethod
    def _visible_count(conn: sqlite3.Connection) -> int:
        row = cast(tuple[object] | None, conn.execute("SELECT COUNT(*) FROM dialogs WHERE hidden=0").fetchone())
        return _required_int(row[0], "directory visible count") if row is not None else 0

    @staticmethod
    def _load_state(conn: sqlite3.Connection) -> _DirectoryState:
        row = cast(
            tuple[object, ...] | None,
            conn.execute(
                "SELECT generation,status,ordinary_status,pinned_main_status,pinned_archive_status,offset_date,offset_id,offset_peer,"
                "observation_started_at,observation_completed_at,account_id,retry_at,reason FROM dialog_directory_state WHERE singleton=1"
            ).fetchone(),
        )
        if row is None:
            raise RuntimeError("canonical dialog directory state is missing")
        cursor = None
        cursor_error = False
        if row[1] != "invalid" and row[12] != "legacy_wrapper_cursor_unverified":
            try:
                cursor = _decode_cursor(row[5], row[6], row[7])
            except ValueError, TypeError, json.JSONDecodeError, RuntimeError:
                cursor_error = True
        return _DirectoryState(
            _required_int(row[0], "directory generation"),
            str(row[1]),
            str(row[2]),
            str(row[3]),
            str(row[4]),
            cursor,
            _nullable_int(row[8]),
            _nullable_int(row[9]),
            _nullable_int(row[10]),
            _nullable_int(row[11]),
            str(row[12]) if row[12] is not None else None,
            cursor_error,
        )


class CanonicalDialogDirectoryDemandAdapter:
    """One durable lifecycle for bootstrap and recurring raw directory refresh."""

    demand_kind = DemandKind.DIALOG_BOOTSTRAP

    def __init__(self, directory: CanonicalDialogDirectory, conn: sqlite3.Connection) -> None:
        self._directory = directory
        self._conn = conn

    def status(self, now: float) -> DemandStatus | None:
        return self._directory.status(now, self._conn)

    async def run_slice(self, budget: RpcAttemptBudget) -> None:
        if not isinstance(budget, RpcAttemptBudget):
            raise TypeError("budget must be an RpcAttemptBudget")
        with demand_context(self.demand_kind):
            with acquisition_context(AcquisitionKind.DIALOG_TRAVERSAL):
                with rpc_attempt_budget(budget):
                    with rpc_scope(TelegramRpcSource.DIALOG_SYNC):
                        await self._directory.run_slice()


def _entity_name(entity: object | None) -> str | None:
    if entity is None:
        return None
    title = getattr(entity, "title", None)
    if isinstance(title, str) and title:
        return title
    first = getattr(entity, "first_name", None)
    last = getattr(entity, "last_name", None)
    pieces = [part for part in (first, last) if isinstance(part, str) and part]
    return " ".join(pieces) or None


def _draft_text(draft: object) -> str | None:
    for name in ("message", "text"):
        value = getattr(draft, name, None)
        if isinstance(value, str):
            return value[:80] or None
    return None


def _nullable_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _identity_is_complete(entity: object | None) -> bool:
    """Return whether this entity can authoritatively replace identity fields."""
    if isinstance(entity, types.User):
        return not bool(entity.min)
    if isinstance(entity, types.Chat):
        return True
    if isinstance(entity, types.Channel):
        return not bool(entity.min)
    return False


def _primary_username(entity: object | None) -> str | None:
    username = getattr(entity, "username", None)
    if not isinstance(username, str):
        return None
    username = username.removeprefix("@")
    return username or None


def _eligibility_category(entity: object | None) -> str | None:
    """Classify only flags whose source object is complete and authoritative."""
    category: str | None = None
    if not _identity_is_complete(entity):
        return category
    if isinstance(entity, types.User):
        if entity.bot is True:
            category = "bot"
        elif isinstance(entity.contact, bool):
            category = "contact" if entity.contact else "non_contact"
    elif isinstance(entity, types.Chat):
        category = "group"
    elif isinstance(entity, types.Channel):
        if entity.megagroup is True:
            category = "group"
        elif entity.broadcast is True:
            category = "broadcast"
    return category


def _three_valued_unread(raw: object) -> int | None:
    operands: list[bool | None] = []
    for field in ("unread_count", "unread_mentions_count"):
        value = _nullable_int(getattr(raw, field, None))
        operands.append(None if value is None else value > 0)
    mark = getattr(raw, "unread_mark", None)
    operands.append(mark if isinstance(mark, bool) else None)
    if any(operand is True for operand in operands):
        return 1
    if all(operand is False for operand in operands):
        return 0
    return None


def _mute_until(settings: object | None) -> int | None:
    value = getattr(settings, "mute_until", None)
    if isinstance(value, datetime):
        return int(value.timestamp())
    return _nullable_int(value)


def _apply_partial_realtime_identity(
    conn: sqlite3.Connection,
    dialog_id: int,
    observation: _RealtimeIdentity,
) -> int:
    prior = cast(
        tuple[str | None, str | None, str, int | None] | None,
        conn.execute(
            "SELECT name,username,type,identity_observed_at FROM dialogs WHERE dialog_id=?", (dialog_id,)
        ).fetchone(),
    )
    if prior is None or _identity_observation_is_empty(observation):
        return 0
    return _store_partial_realtime_identity(conn, dialog_id, prior, observation)


def _identity_observation_is_empty(observation: _RealtimeIdentity) -> bool:
    return (
        observation.name is IDENTITY_OMITTED
        and observation.username is IDENTITY_OMITTED
        and observation.dialog_type is IDENTITY_OMITTED
    )


def _store_partial_realtime_identity(
    conn: sqlite3.Connection,
    dialog_id: int,
    prior: tuple[str | None, str | None, str, int | None],
    observation: _RealtimeIdentity,
) -> int:
    prior_name, prior_username, prior_type, prior_observed_at = prior
    retained_type = observation.dialog_type is IDENTITY_OMITTED or observation.dialog_type is None
    updated_name = prior_name if observation.name is IDENTITY_OMITTED else observation.name
    updated_username = prior_username if observation.username is IDENTITY_OMITTED else observation.username
    updated_type = prior_type if retained_type else observation.dialog_type
    boundary = observation.observed_at if prior_observed_at is None else min(prior_observed_at, observation.observed_at)
    cursor = conn.execute(
        "UPDATE dialogs SET name=?,username=?,type=?,identity_observed_at=?,identity_source=?,"
        "revision=revision+1 WHERE dialog_id=?",
        (updated_name, updated_username, updated_type, boundary, "mixed" if retained_type else "realtime", dialog_id),
    )
    if cursor.rowcount:
        unhide_after_realtime_presence(conn, dialog_id)
    return cursor.rowcount


def _apply_complete_realtime_identity(
    conn: sqlite3.Connection,
    dialog_id: int,
    observation: _RealtimeIdentity,
) -> int:
    if not isinstance(observation.dialog_type, str):
        raise ValueError("complete realtime identity requires a dialog type")
    if observation.name is IDENTITY_OMITTED or observation.username is IDENTITY_OMITTED:
        raise ValueError("complete realtime identity requires every identity field")
    cursor = conn.execute(
        "UPDATE dialogs SET name=?,username=?,type=?,identity_observed_at=?,identity_complete=1,"
        "identity_source='realtime',revision=revision+1 WHERE dialog_id=?",
        (observation.name, observation.username, observation.dialog_type, observation.observed_at, dialog_id),
    )
    if cursor.rowcount:
        unhide_after_realtime_presence(conn, dialog_id)
    return cursor.rowcount


def apply_realtime_identity(  # noqa: PLR0913
    conn: sqlite3.Connection,
    dialog_id: int,
    *,
    name: str | _IdentityOmitted | None = IDENTITY_OMITTED,
    username: str | _IdentityOmitted | None = IDENTITY_OMITTED,
    dialog_type: str | _IdentityOmitted | None = IDENTITY_OMITTED,
    observed_at: int,
    complete: bool,
) -> int:
    """Apply a realtime identity observation on an existing catalog row."""
    observation = _RealtimeIdentity(name, username, dialog_type, observed_at)
    if complete:
        return _apply_complete_realtime_identity(conn, dialog_id, observation)
    return _apply_partial_realtime_identity(conn, dialog_id, observation)


def _state_wire(state: _DirectoryState) -> dict[str, object]:
    return {
        "generation": state.generation,
        "status": state.status,
        "reason": state.reason,
    }


def recover_invalid_generation_in_transaction(conn: sqlite3.Connection) -> dict[str, object]:
    """Explicitly replace only a semantic-invalid attempt on the daemon writer."""
    state = CanonicalDialogDirectory._load_state(conn)
    previous = _state_wire(state)
    if state.status != "invalid":
        return {"ok": False, "error": "dialog_directory_not_invalid", "state": previous}
    CanonicalDialogDirectory._start_generation(conn, state.generation + 1)
    current = CanonicalDialogDirectory._load_state(conn)
    return {"ok": True, "previous": previous, "current": _state_wire(current)}


def sync_active_generation_pins_from_publication(conn: sqlite3.Connection, folder_id: int) -> None:
    """Fence an active source against newer realtime pin membership and order."""
    row = cast(
        tuple[int, str] | None,
        conn.execute("SELECT generation,status FROM dialog_directory_state WHERE singleton=1").fetchone(),
    )
    if row is None or row[1] != "in_progress":
        return
    generation = int(cast(int, row[0]))
    conn.execute("DELETE FROM dialog_directory_pins WHERE generation=? AND folder_id=?", (generation, folder_id))
    conn.execute(
        "INSERT INTO dialog_directory_pins(generation,folder_id,dialog_id,position) "
        "SELECT ?,folder_id,dialog_id,position FROM dialog_directory_published_pins WHERE folder_id=?",
        (generation, folder_id),
    )


def apply_active_generation_pin_delta(conn: sqlite3.Connection, folder_id: int, dialog_id: int, pinned: bool) -> None:
    """Apply a realtime single-peer pin delta to the active generation.

    The active generation is the source of truth while acquisition is in
    progress.  Reading the published relation here would discard pins already
    acquired by the in-flight generation.
    """
    row = cast(
        tuple[object, object] | None,
        conn.execute("SELECT generation,status FROM dialog_directory_state WHERE singleton=1").fetchone(),
    )
    if row is None or row[1] != "in_progress":
        return
    generation = int(cast(int, row[0]))
    current = [
        int(cast(int, pin[0]))
        for pin in cast(
            list[tuple[object, object]],
            conn.execute(
                "SELECT dialog_id,position FROM dialog_directory_pins "
                "WHERE generation=? AND folder_id=? ORDER BY position,dialog_id",
                (generation, folder_id),
            ).fetchall(),
        )
    ]
    if pinned:
        current = [dialog_id, *(existing for existing in current if existing != dialog_id)]
    else:
        current = [existing for existing in current if existing != dialog_id]
    conn.execute(
        "DELETE FROM dialog_directory_pins WHERE generation=? AND folder_id=?",
        (generation, folder_id),
    )
    conn.executemany(
        "INSERT INTO dialog_directory_pins(generation,folder_id,dialog_id,position) VALUES (?,?,?,?)",
        [(generation, folder_id, current_id, position) for position, current_id in enumerate(current)],
    )


def _pin_fence_key(generation: int, folder_id: int) -> str:
    return f"dialog_directory_pin_fence:{generation}:{folder_id}"


def record_realtime_pin_fence(
    conn: sqlite3.Connection,
    folder_id: int,
    event: dict[str, object],
) -> None:
    """Record an in-flight pin event for the next source commit.

    Events and source RPC commits use separate connections and cannot share a
    Python flag.  This small durable fence keeps event ordering across that
    await boundary without changing the published schema.
    """
    row = cast(
        tuple[object, object] | None,
        conn.execute("SELECT generation,status FROM dialog_directory_state WHERE singleton=1").fetchone(),
    )
    if row is None or row[1] != "in_progress":
        return
    generation = int(cast(int, row[0]))
    key = _pin_fence_key(generation, folder_id)
    prior = cast(
        tuple[object] | None,
        conn.execute("SELECT value FROM daemon_state WHERE key=?", (key,)).fetchone(),
    )
    events: list[dict[str, object]] = []
    if prior is not None and prior[0] is not None:
        decoded = cast(object, json.loads(str(cast(object, prior[0]))))
        if not isinstance(decoded, list) or not all(isinstance(item, dict) for item in decoded):
            raise RuntimeError("invalid dialog directory pin fence")
        events = cast(list[dict[str, object]], decoded)
    events.append(event)
    conn.execute(
        "INSERT OR REPLACE INTO daemon_state(key,value) VALUES (?,?)",
        (key, json.dumps(events, separators=(",", ":"))),
    )


def read_realtime_pin_fence(conn: sqlite3.Connection, generation: int, folder_id: int) -> list[dict[str, object]]:
    row = cast(
        tuple[object] | None,
        conn.execute("SELECT value FROM daemon_state WHERE key=?", (_pin_fence_key(generation, folder_id),)).fetchone(),
    )
    if row is None or row[0] is None:
        return []
    decoded = cast(object, json.loads(str(cast(object, row[0]))))
    if not isinstance(decoded, list) or not all(isinstance(item, dict) for item in decoded):
        raise RuntimeError("invalid dialog directory pin fence")
    return cast(list[dict[str, object]], decoded)


def replace_active_generation_pins(
    conn: sqlite3.Connection, generation: int, folder_id: int, dialog_ids: list[int]
) -> None:
    conn.execute(
        "DELETE FROM dialog_directory_pins WHERE generation=? AND folder_id=?",
        (generation, folder_id),
    )
    conn.executemany(
        "INSERT INTO dialog_directory_pins(generation,folder_id,dialog_id,position) VALUES (?,?,?,?)",
        [(generation, folder_id, dialog_id, position) for position, dialog_id in enumerate(dialog_ids)],
    )


def apply_realtime_pin_fence(
    conn: sqlite3.Connection, generation: int, folder_id: int, dialog_ids: list[int]
) -> list[int]:
    """Replay fenced realtime pin events onto an acquired RPC order."""
    current = list(dialog_ids)
    for event in read_realtime_pin_fence(conn, generation, folder_id):
        current = _apply_realtime_pin_fence_event(current, event)
    replace_active_generation_pins(conn, generation, folder_id, current)
    return current


def _apply_realtime_pin_fence_event(current: list[int], event: dict[str, object]) -> list[int]:
    kind = event.get("kind")
    if kind == "rewrite":
        return _pin_rewrite_from_fence(event)
    if kind == "delta":
        return _pin_delta_from_fence(current, event)
    raise RuntimeError("invalid dialog directory pin event fence")


def _pin_rewrite_from_fence(event: dict[str, object]) -> list[int]:
    raw_ids = event.get("dialog_ids")
    if not isinstance(raw_ids, list) or not all(isinstance(item, int) for item in raw_ids):
        raise RuntimeError("invalid dialog directory pin rewrite fence")
    return list(cast(list[int], raw_ids))


def _pin_delta_from_fence(current: list[int], event: dict[str, object]) -> list[int]:
    event_id = event.get("dialog_id")
    event_pinned = event.get("pinned")
    if not isinstance(event_id, int) or not isinstance(event_pinned, bool):
        raise RuntimeError("invalid dialog directory pin delta fence")
    if event_pinned:
        return [event_id, *(existing for existing in current if existing != event_id)]
    return [existing for existing in current if existing != event_id]


def _commit_pinned_generation(
    conn: sqlite3.Connection,
    generation: int,
    folder_id: int,
    rpc_ids: list[int],
) -> None:
    fence_events = read_realtime_pin_fence(conn, generation, folder_id)
    if fence_events:
        apply_realtime_pin_fence(conn, generation, folder_id, rpc_ids)
    else:
        # No realtime writer published this source during the request, so this
        # RPC is the authoritative, replaceable source order.
        replace_active_generation_pins(conn, generation, folder_id, rpc_ids)


def _insert_realtime_eligibility(
    conn: sqlite3.Connection,
    dialog_id: int,
    supplied: tuple[str | None, int | None, int | None, int | None],
    observed_at: int,
) -> None:
    conn.execute(
        "INSERT INTO dialog_directory_facts(dialog_id,category,archived,unread,mute_until,observed_at) VALUES (?,?,?,?,?,?)",
        (dialog_id, *supplied, observed_at),
    )


def _merge_realtime_eligibility(
    conn: sqlite3.Connection,
    dialog_id: int,
    supplied: tuple[str | None, int | None, int | None, int | None],
    prior: tuple[str | None, int | None, int | None, int | None, int | None],
    observed_at: int,
) -> bool:
    values: tuple[str | None, int | None, int | None, int | None] = (
        supplied[0] if supplied[0] is not None else prior[0],
        supplied[1] if supplied[1] is not None else prior[1],
        supplied[2] if supplied[2] is not None else prior[2],
        supplied[3] if supplied[3] is not None else prior[3],
    )
    if values == prior[:4]:
        return False
    retained = any(new is None and old is not None for new, old in zip(supplied, prior[:4], strict=True))
    boundary = min(prior[4], observed_at) if retained and prior[4] is not None else observed_at
    conn.execute(
        "UPDATE dialog_directory_facts SET category=?,archived=?,unread=?,mute_until=?,observed_at=? WHERE dialog_id=?",
        (*values, boundary, dialog_id),
    )
    return True


def apply_realtime_eligibility(  # noqa: PLR0913
    conn: sqlite3.Connection,
    dialog_id: int,
    *,
    observed_at: int,
    category: str | None = None,
    archived: int | None = None,
    unread: int | None = None,
    mute_until: int | None = None,
) -> int:
    """Merge a partial realtime eligibility observation without renewing retained facts."""
    supplied = (category, archived, unread, mute_until)
    if all(value is None for value in supplied):
        return 0
    prior = cast(
        tuple[str | None, int | None, int | None, int | None, int | None] | None,
        conn.execute(
            "SELECT category,archived,unread,mute_until,observed_at FROM dialog_directory_facts WHERE dialog_id=?",
            (dialog_id,),
        ).fetchone(),
    )
    if prior is None:
        _insert_realtime_eligibility(conn, dialog_id, supplied, observed_at)
    elif not _merge_realtime_eligibility(conn, dialog_id, supplied, prior, observed_at):
        return 0
    conn.execute("UPDATE dialogs SET revision=revision+1 WHERE dialog_id=?", (dialog_id,))
    return 1


def clear_realtime_mute(conn: sqlite3.Connection, dialog_id: int, *, observed_at: int) -> int:
    """Clear a realtime mute fact while fencing an in-flight directory snapshot."""
    cursor = conn.execute(
        "UPDATE dialog_directory_facts SET mute_until=NULL, "
        "observed_at=CASE WHEN observed_at IS NULL THEN ? ELSE MIN(observed_at,?) END "
        "WHERE dialog_id=? AND mute_until IS NOT NULL",
        (observed_at, observed_at, dialog_id),
    )
    if cursor.rowcount:
        conn.execute("UPDATE dialogs SET revision=revision+1 WHERE dialog_id=?", (dialog_id,))
    return cursor.rowcount


def _account_id_from_profile(profile: object) -> int:
    account_id = getattr(profile, "id", None)
    if isinstance(account_id, bool) or not isinstance(account_id, int) or account_id <= 0:
        raise RuntimeError("directory requires an authenticated account id")
    return account_id


def _encode_input_peer(cursor: DialogCursor) -> str:
    peer = cursor.offset_peer
    if isinstance(peer, InputPeerUser):
        payload = {"kind": "user", "id": peer.user_id, "access_hash": peer.access_hash}
    elif isinstance(peer, InputPeerChat):
        payload = {"kind": "chat", "id": peer.chat_id}
    elif isinstance(peer, InputPeerChannel):
        payload = {"kind": "channel", "id": peer.channel_id, "access_hash": peer.access_hash}
    elif isinstance(peer, InputPeerSelf):
        payload = {"kind": "self"}
    else:
        raise ValueError("directory cursor must use a reconstructible peer")
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _decode_cursor(raw_date: object, raw_id: object, raw_peer: object) -> DialogCursor | None:
    offset_id = _required_int(raw_id, "cursor offset id")
    if raw_date is None and raw_peer is None and offset_id == 0:
        return None
    if not isinstance(raw_date, str) or not isinstance(raw_peer, str):
        raise RuntimeError("directory cursor is not reconstructible")
    payload = cast(dict[str, object], json.loads(raw_peer))
    kind = payload.get("kind")
    if kind == "self":
        peer = InputPeerSelf()
    else:
        peer_id = _required_int(payload.get("id"), "cursor peer id")
        if kind == "user":
            peer = InputPeerUser(peer_id, _required_int(payload.get("access_hash"), "cursor access hash"))
        elif kind == "chat":
            peer = InputPeerChat(peer_id)
        elif kind == "channel":
            peer = InputPeerChannel(peer_id, _required_int(payload.get("access_hash"), "cursor access hash"))
        else:
            raise RuntimeError("directory cursor has an unknown peer kind")
    return DialogCursor(datetime.fromisoformat(raw_date), offset_id, peer)


def _required_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise RuntimeError(f"{label} is invalid")
    return int(value)
