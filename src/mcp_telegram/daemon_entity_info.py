"""Entity info extraction service extracted from daemon_api.

This module owns the full ``get_entity_info`` orchestration plus type-specific
helpers for user/bot/channel/supergroup/group entity details.
"""

import asyncio
import json
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from dataclasses import replace as dataclass_replace
from typing import Protocol, cast

from telethon.errors import (  # type: ignore[import-untyped]
    ChatIdInvalidError,
    PeerIdInvalidError,
    RPCError,
    UserIdInvalidError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)

from .auth_scope import TelegramAuthScope
from .demand_wiring import DemandOfferSink, offer_durable_demand
from .dialog_identity import capture_identity_baseline
from .entity_profile.contracts import (
    CHANNEL_CONTACT_UNSUPPORTED_REASON,
    CHANNEL_CONTACT_UNSUPPORTED_RPC_CODE,
    CHANNEL_ID_MARKER,
    FULL_PROFILE_OWNED_FIELDS,
    FULL_USER_ENDPOINT,
    GROUP_PROFILE_ENDPOINT,
    GROUP_PROFILE_NORMALIZATION_VERSION,
    NORMALIZATION_VERSION,
    PERSONAL_CHANNEL_OWNED_FIELDS,
    PROFILE_COLLECTION_SECTIONS,
    PROFILE_SECTIONS,
    ChannelContactOverlapObservation,
    ChannelProfileObservation,
    ChannelReference,
    ChatAvatarReference,
    ChatCurrentPhoto,
    GroupProfileObservation,
    GroupReference,
    ProfileAcquisitionEvidence,
    ProfileFieldObservation,
    ProfileFieldState,
    ProfileSemanticSnapshot,
    ProjectionOutcome,
    ProjectionStatus,
    StoryProfileValidationError,
    TargetKind,
    UserProfileObservation,
    UserReference,
    completeness,
    reconcile_chat_avatar_history,
)
from .entity_profile.ports import (
    ChannelProfilePort,
    ChannelReferenceProvider,
    ChatAvatarHistoryPort,
    CommonChatsPort,
    GroupProfilePort,
    ProfileCollectionPort,
    ProfilePairObservationHook,
    UserAvatarHistoryPort,
    UserProfilePort,
)
from .entity_profile.refresh import (
    DurableRefreshSliceResult,
    DurableRefreshTerminal,
    EntityRefreshCoordinator,
    RefreshLimits,
    failure_retry_at,
    scope_changed_retry_at,
    throttled_retry_at,
)
from .entity_profile.repository import (
    EntityProfileRepository,
    EntityRefreshCursor,
    EntitySectionCommit,
    pair_readiness_latency_ms,
)
from .flood import TelegramRpcThrottled, _raise_if_latched
from .folders.read_model import dialog_placement
from .linked_chat_fact import linked_chat_fact_owner
from .models import DialogType
from .observation_order import allocate_observation_order
from .sqlite_writer import SQLiteWriter
from .sync_transactions import write_transaction
from .telegram_access import ACCESS_LOST_ERRORS
from .telegram_demand import (
    DemandStatus,
    RpcAttemptBudget,
    RpcAttemptBudgetExhaustedError,
    UnclassifiedTelegramDemandError,
)
from .telegram_rpc import raise_if_flood_wait_error
from .telegram_rpc_consumers import DemandKind
from .telegram_rpc_error import describe_telegram_rpc_error
from .telegram_rpc_scheduler import current_rpc_scope
from .telethon_dialog import classify_dialog_type, observe_dialog_identity


class _LinkedChatFactCaptureUnavailable:
    pass


_LINKED_CHAT_FACT_CAPTURE_UNAVAILABLE = _LinkedChatFactCaptureUnavailable()


def _contact_rpc_error(section: str, exc: BaseException) -> bool:
    return section == "contact_overlap" and isinstance(exc, RPCError)


def _profile_section_failure_reason(section: str, exc: BaseException) -> str:
    if section in {"current_stories", "pinned_stories"} and isinstance(exc, StoryProfileValidationError):
        return exc.reason
    fallback = type(exc).__name__.lower()
    if not _contact_rpc_error(section, exc):
        return fallback
    descriptor = describe_telegram_rpc_error(exc)
    if descriptor.symbol is None or descriptor.code is None:
        return fallback
    return f"{descriptor.error_type.lower()}:{descriptor.code}:{descriptor.symbol}"


def _scope_bound_profile_failure(section: str, exc: BaseException) -> bool:
    return _contact_rpc_error(section, exc) or (
        section in {"current_stories", "pinned_stories"} and isinstance(exc, StoryProfileValidationError)
    )


def _raw_linked_chat_channel_id(canonical_id: int | None) -> int | None:
    """Convert Telethon's canonical -100… peer ID back to the raw channel ID."""
    if canonical_id is None:
        return None
    raw_id = -int(canonical_id) - 1_000_000_000_000
    if raw_id <= 0:
        raise ValueError("linked chat observation is not a canonical channel peer ID")
    return raw_id


_CHANNEL_DIALOG_ID_OFFSET = 1_000_000_000_000
_SQLITE_INT64_MIN = -(2**63)
_SQLITE_INT64_MAX = 2**63 - 1
# Telethon's integer lookup also binds ``-(offset + peer_id)`` for a channel;
# the signed range includes one more negative value than positive values.
_TELETHON_PEER_ID_MAX = _SQLITE_INT64_MAX - _CHANNEL_DIALOG_ID_OFFSET + 1
_PERSONAL_CHANNEL_PREVIEW_CHARS = 100
_PAIR_SECTION_COUNT = 2
_ENTITY_NOT_FOUND_ERRORS = (
    ChatIdInvalidError,
    PeerIdInvalidError,
    UserIdInvalidError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)


def _is_valid_telethon_peer_id(value: int) -> bool:
    """Return whether an exact ID is safe for Telethon's SQLite peer lookup."""
    return value != 0 and _SQLITE_INT64_MIN <= value <= _TELETHON_PEER_ID_MAX


class _AuthScopeUnavailableError(RuntimeError):
    """The live primary session identity cannot authorize a pair receipt."""


class _EntityInfoClient(Protocol):
    def get_entity(self, entity_id: int) -> Awaitable[object]: ...


def _attr(obj: object, name: str, default: object | None = None) -> object | None:
    try:
        return cast(object | None, object.__getattribute__(obj, name))
    except AttributeError:
        return default


def _opt_str_attr(obj: object, name: str) -> str | None:
    value = _attr(obj, name)
    return value if isinstance(value, str) else None


def _channel_profile_unavailable_commit(
    reference: ChannelReference,
    observation: ChannelProfileObservation,
) -> EntitySectionCommit | None:
    if observation.channel_id != reference.channel_id:
        raise ValueError("channel profile observation target does not match")
    if observation.status is ProjectionStatus.UNAVAILABLE:
        return EntitySectionCommit({}, status="unavailable", reason=observation.reason)
    if observation.status is not ProjectionStatus.USABLE:
        raise ValueError(observation.reason or "channel profile is unavailable")
    return None


def _channel_profile_kind_patch(
    entity_type: DialogType,
    observation: ChannelProfileObservation,
) -> dict[str, object]:
    if entity_type is DialogType.CHANNEL:
        return {
            "subscribers_count": observation.participants_count,
            "available_reactions": dict(observation.available_reactions),
        }
    return {
        "members_count": observation.participants_count,
        "linked_broadcast_id": observation.linked_chat_id,
    }


def _channel_profile_payload(observation: ChannelProfileObservation) -> dict[str, object]:
    current = observation.current_photo
    return {"current_photo": {"photo_id": current.photo_id, "date": current.date} if current is not None else None}


def _positive_personal_channel_id(payload: Mapping[str, object]) -> int | None:
    value = payload.get("personal_channel_id")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _compact_dict(items: Mapping[str, object | None]) -> dict[str, object]:
    return {key: value for key, value in items.items() if value is not None}


def _current_photo_from_payload(payload: object) -> ChatCurrentPhoto | None:
    if not isinstance(payload, Mapping):
        return None
    value = payload.get("current_photo")
    if not isinstance(value, Mapping):
        return None
    photo_id = value.get("photo_id")
    date = value.get("date")
    if not isinstance(photo_id, int) or isinstance(photo_id, bool) or photo_id <= 0:
        return None
    if date is not None and not isinstance(date, str):
        return None
    return ChatCurrentPhoto(photo_id, date)


def _profile_photo_patch(photo: ChatCurrentPhoto | None, snapshot: ProfileSemanticSnapshot | None) -> dict[str, object]:
    if photo is not None:
        evidence = snapshot.coverage.get("user.photo.photo_id") if snapshot else None
        if snapshot is None or (evidence is not None and evidence.authoritative):
            return {"current_photo": {"photo_id": photo.photo_id, "date": photo.date}}
    elif snapshot is not None:
        evidence = snapshot.coverage.get("user.photo")
        if evidence is not None and evidence.state is ProfileFieldState.CLEARED and evidence.authoritative:
            return {"current_photo": None}
    return {}


def _reconcile_history_payload(
    history: Sequence[object], current_photo: ChatCurrentPhoto | None, reported_count: int
) -> tuple[list[dict[str, object]], int]:
    photos = tuple(item for item in history if isinstance(item, ChatCurrentPhoto))
    merged, count = reconcile_chat_avatar_history(photos, current_photo, reported_count)
    return [{"photo_id": photo.photo_id, "date": photo.date} for photo in merged], count


def _project_current_photo(
    result: dict[str, object], sections: Mapping[str, Mapping[str, object]], current_photo: ChatCurrentPhoto | None
) -> None:
    if current_photo is None:
        return
    avatar_data = sections.get("avatar_history", {}).get("data")
    existing_values = _avatar_history_values(avatar_data)
    raw_count = result.get("avatar_count")
    prior_count = raw_count if isinstance(raw_count, int) else 0
    merged, merged_count = reconcile_chat_avatar_history(tuple(existing_values), current_photo, prior_count)
    result["avatar_history"] = [{"photo_id": item.photo_id, "date": item.date} for item in merged]
    result["avatar_count"] = merged_count


def _avatar_history_values(value: object) -> tuple[ChatCurrentPhoto, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        return ()
    output: list[ChatCurrentPhoto] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        photo_id = item.get("photo_id")
        if not isinstance(photo_id, int) or isinstance(photo_id, bool) or photo_id <= 0:
            continue
        date = item.get("date")
        output.append(ChatCurrentPhoto(photo_id, date if isinstance(date, str) else None))
    return tuple(output)


@dataclass(frozen=True)
class EntityInfoDeps:
    """Dependency container for entity-info orchestration."""

    conn: sqlite3.Connection
    writer: SQLiteWriter
    client: _EntityInfoClient
    dm_peer_ids: Callable[[], set[int]]
    self_id: int | None
    self_profile: Mapping[str, object] | None
    get_peer_id: Callable[[object], int]
    rid: Callable[[], str]
    logger: logging.Logger
    now_provider: Callable[[], float]
    detail_ttl_seconds: int
    group_profile_port: GroupProfilePort
    user_profile_port: UserProfilePort
    channel_profile_port: ChannelProfilePort
    channel_reference_provider: ChannelReferenceProvider
    common_chats_port: CommonChatsPort
    user_avatar_history_port: UserAvatarHistoryPort
    chat_avatar_history_port: ChatAvatarHistoryPort
    get_dialog_placement: Callable[[int], dict[str, object]] | None = None
    refresh_limits: RefreshLimits = field(default_factory=RefreshLimits)
    enable_full_user_pair: bool = False
    full_user_auth_scope: Callable[[], TelegramAuthScope | None] | None = None
    profile_observer: ProfilePairObservationHook | None = None


@dataclass(slots=True)
class _ProfileSectionContext:
    captured_scope: TelegramAuthScope | None
    attempt_start: int
    pair_section: bool
    pair_mode: str
    eligible_pair: bool
    linked_chat_fact_generation: int | None = None
    linked_chat_id: int | None = None
    linked_chat_fact_captured: bool = False
    linked_chat_observation_usable: bool = False


@dataclass(frozen=True, slots=True)
class _ProjectionCommitContext:
    payload: dict[str, object]
    evidence: ProfileAcquisitionEvidence | None
    observation_owner_account_id: int | None
    observation_auth_scope: Mapping[str, object] | None
    ownership_observed: bool


@dataclass(frozen=True, slots=True)
class _ProfileSectionFailure:
    cursor: EntityRefreshCursor
    entity_type: DialogType
    context: _ProfileSectionContext
    now: int
    reason: str
    retry_at: int


@dataclass(frozen=True, slots=True)
class _PairFailure:
    cursor: EntityRefreshCursor
    now: int
    pair_mode: str
    attempt_start: int
    reason: str
    retry_at: int
    reuse_rejection_reason: str | None


@dataclass(frozen=True, slots=True)
class _PairCommitRequest:
    cursor: EntityRefreshCursor
    full_profile: EntitySectionCommit
    personal_channel: EntitySectionCommit
    now: int
    pair_mode: str
    attempt_start: int
    reuse_rejection_reason: str | None


@dataclass(frozen=True, slots=True)
class _PairMeasurement:
    cursor: EntityRefreshCursor
    full_profile: EntitySectionCommit
    personal_channel: EntitySectionCommit
    now: int
    pair_mode: str
    actual_attempts: int
    committed: bool


@dataclass(frozen=True, slots=True)
class _PersonalChannelCard:
    outcome: ProjectionOutcome
    channel_id: int
    dialog_id: int
    metadata: Mapping[str, object]
    metadata_source: str
    local_preview: Mapping[str, object] | None
    local_reason: str | None


class DaemonEntityInfoService:
    """Entity-info extraction service used by ``DaemonAPIServer._get_entity_info``."""

    def __init__(self, deps: EntityInfoDeps, *, enable_refresh_coordinator: bool = True) -> None:
        self._deps = deps
        self._profiles = EntityProfileRepository(deps.conn, section_ttl_seconds=deps.detail_ttl_seconds)
        self._demand_sink: DemandOfferSink | None = None
        self._refresh = EntityRefreshCoordinator(limits=deps.refresh_limits) if enable_refresh_coordinator else None
        if self._refresh is not None:
            self._refresh.bind_durable_executor(self._durable_refresh_status, self._run_durable_refresh_slice)

    def bind_demand_sink(self, sink: DemandOfferSink) -> None:
        """Attach post-commit wakeups to the process-wide coordinator."""
        self._demand_sink = sink

    async def reconcile_history_profiles(self) -> int:
        """Recover one bounded batch of locally missing DM profile observations."""
        count = await self._write_profiles(
            lambda profiles: profiles.reconcile_history_profiles(
                now=int(self._deps.now_provider()),
                limit=32,
                pair_mode=self._current_pair_mode(),
                self_id=self._deps.self_id,
            )
        )
        if count:
            offer_durable_demand(self._require_demand_sink(), DemandKind.ENTITY_PROFILE_REFRESH)
        return count

    async def _write_profiles[T](self, operation: Callable[[EntityProfileRepository], T]) -> T:
        return await self._deps.writer.run(
            lambda conn: operation(EntityProfileRepository(conn, section_ttl_seconds=self._deps.detail_ttl_seconds))
        )

    def bind_profile_observer(self, observer: ProfilePairObservationHook) -> None:
        """Attach the best-effort profile telemetry hook after composition."""
        self._deps = dataclass_replace(self._deps, profile_observer=observer)

    def _current_pair_mode(self) -> str:
        return "enabled" if self._deps.enable_full_user_pair else "disabled"

    def _require_demand_sink(self) -> DemandOfferSink:
        sink = self._demand_sink
        if sink is None:
            raise RuntimeError("durable demand sink is not bound")
        return sink

    @property
    def refresh_coordinator(self) -> EntityRefreshCoordinator | None:
        """Return the service-owned coordinator used by demand composition."""
        return self._refresh

    async def get_entity_info(self, req: Mapping[str, object]) -> dict[str, object]:
        """Time a local-only projection of the durable entity profile."""
        started_at = time.perf_counter()
        try:
            return await self._get_entity_info(req)
        finally:
            self._deps.logger.info(
                "entity_profile.request duration_s=%.3f%s",
                time.perf_counter() - started_at,
                self._deps.rid(),
            )

    async def _get_entity_info(self, req: Mapping[str, object]) -> dict[str, object]:
        """Project persisted observations without initiating Telegram acquisition."""
        entity_id = self._extract_entity_id(req)
        if isinstance(entity_id, dict):
            return entity_id
        now = int(self._deps.now_provider())
        stored = self._profiles.read(entity_id, now=now)
        if stored is not None and stored.profile_owner_account_id is not None:
            if not self._profile_owner_matches(stored.profile_owner_account_id):
                return self._error(
                    "entity_profile_account_unavailable",
                    "The stored profile is not bound to the current account. Action: verify the daemon account binding.",
                )
            return await self._local_profile_result(entity_id, stored.detail, stored.sections, now=now, req=req)
        identity = self._local_profile_identity(entity_id)
        if not identity:
            return self._error(
                "entity_not_observed",
                "No local observations exist for this entity. Action: use list_dialogs to select a known local entity.",
            )
        sections = self._unobserved_sections(identity)
        return await self._local_profile_result(entity_id, identity, sections, now=now, req=req)

    async def _local_profile_result(
        self,
        entity_id: int,
        detail: Mapping[str, object],
        sections: dict[str, dict[str, object]],
        *,
        now: int,
        req: Mapping[str, object],
    ) -> dict[str, object]:
        from .entity_profile.reader import read_profile

        result = await self._progressive_result(entity_id, detail, sections, now=now)
        scope = self._capture_pair_scope()
        owner = self._deps.self_id or (scope.account_id if scope is not None else None)
        modern = cast(
            tuple[object, ...] | None,
            self._deps.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='entity_profile_semantic_snapshots'"
            ).fetchone(),
        )
        if owner is None or modern is None or entity_id <= 0:
            if req.get("cursor") is not None or req.get("section") is not None:
                return self._error(
                    "profile_selection_unavailable", "Account-bound local profile paging is unavailable."
                )
            return result
        try:
            projection = read_profile(self._deps.conn, owner, entity_id, now, req)
        except (ValueError, TypeError) as exc:
            return self._error(
                "invalid_profile_navigation", str(exc) + ". Action: restart the local selection without a cursor."
            )
        data = result.get("data")
        if isinstance(data, dict):
            data.update(projection)
        return result

    def _profile_owner_matches(self, owner_account_id: int) -> bool:
        scope = self._capture_pair_scope()
        account_id = self._deps.self_id
        if account_id is None and scope is not None:
            account_id = scope.account_id
        return (
            account_id is not None
            and owner_account_id == account_id
            and (scope is None or scope.account_id == account_id)
        )

    def _local_profile_identity(self, entity_id: int) -> dict[str, object]:
        profile = self._deps.self_profile
        if self._deps.self_id != entity_id or profile is None:
            return self._observed_directory_identity(entity_id)
        return {
            "id": entity_id,
            "type": DialogType.USER.value,
            "name": " ".join(str(profile[key]) for key in ("first_name", "last_name") if profile.get(key)) or None,
            "username": profile.get("username"),
            "my_membership": {"role": "self"},
        }

    def _observed_directory_identity(self, entity_id: int) -> dict[str, object]:
        identity = self._profiles.read_identity(entity_id)
        if identity.get("type") != DialogType.UNKNOWN.value:
            return identity
        if any(isinstance(value, str) and value.strip() for value in (identity.get("name"), identity.get("username"))):
            return identity
        return {}

    def _unobserved_sections(self, identity: Mapping[str, object]) -> dict[str, dict[str, object]]:
        raw_type = identity.get("type")
        entity_type = DialogType.parse(raw_type if isinstance(raw_type, str) else None)
        return {
            section: {
                "status": (
                    "unavailable"
                    if entity_type is DialogType.UNKNOWN or self._section_applies(entity_type, section)
                    else "not_applicable"
                ),
                "reason": (
                    "not_observed"
                    if entity_type is DialogType.UNKNOWN or self._section_applies(entity_type, section)
                    else "not_applicable"
                ),
            }
            for section in (
                PROFILE_SECTIONS if entity_type in {DialogType.USER, DialogType.BOT} else PROFILE_SECTIONS[:5]
            )
        }

    def _durable_refresh_status(self, now: float) -> DemandStatus | None:
        del now
        release_at = self._profiles.next_refresh_release_at()
        return DemandStatus(release_at=release_at) if release_at is not None else None

    async def _run_durable_refresh_slice(self, budget: RpcAttemptBudget) -> DurableRefreshSliceResult | None:
        """Execute and commit at most one profile acquisition for one entity."""
        if not isinstance(budget, RpcAttemptBudget):
            raise TypeError("budget must be an RpcAttemptBudget")
        now = int(self._deps.now_provider())
        await self._write_profiles(lambda profiles: profiles.recover_pair_measurements(now=now))
        cursor = self._profiles.next_due_refresh(now=now)
        if cursor is None:
            return None
        pair_mode = await self._capture_refresh_pair_mode(cursor)
        entity_type = self._stored_entity_type(cursor.entity_id, now=now)
        if entity_type is DialogType.UNKNOWN:
            return await self._run_unknown_entity_refresh(cursor, now=now)
        return await self._run_known_entity_refresh(cursor, entity_type, pair_mode=pair_mode, now=now)

    async def _capture_refresh_pair_mode(self, cursor: EntityRefreshCursor) -> str:
        return await self._write_profiles(
            lambda profiles: profiles.capture_pair_mode(
                cursor,
                "enabled" if self._deps.enable_full_user_pair else "disabled",
            )
        )

    async def _run_unknown_entity_refresh(
        self,
        cursor: EntityRefreshCursor,
        *,
        now: int,
    ) -> DurableRefreshSliceResult:
        terminal = await self._acquire_durable_refresh_core(cursor, now=now)
        return DurableRefreshSliceResult(cursor.entity_id, terminal)

    async def _run_known_entity_refresh(
        self,
        cursor: EntityRefreshCursor,
        entity_type: DialogType,
        *,
        pair_mode: str,
        now: int,
    ) -> DurableRefreshSliceResult:
        stored = self._profiles.read(cursor.entity_id, now=now)
        scope = self._capture_pair_scope()
        if (
            stored is not None
            and scope is not None
            and stored.profile_owner_account_id == scope.account_id
            and await self._write_profiles(lambda profiles: profiles.complete_unchanged_section(cursor, now=now))
        ):
            return DurableRefreshSliceResult(
                cursor.entity_id, self._success_if_refresh_finished(cursor, committed=True)
            )
        if entity_type is DialogType.GROUP and cursor.next_section == "full_profile":
            return await self._run_group_full_chat_pair(cursor, now=now)
        if self._pair_enabled(cursor, entity_type, pair_mode=pair_mode):
            return await self._run_full_user_pair_refresh(cursor, entity_type, now=now)
        if self._is_paired_personal_channel_cursor(cursor, entity_type=entity_type, pair_mode=pair_mode):
            return await self._complete_paired_personal_channel(cursor, entity_type, now=now)
        if not self._section_applies(entity_type, cursor.next_section):
            return await self._complete_not_applicable_section(cursor, now=now)
        terminal = await self._acquire_and_commit_profile_section(cursor, entity_type, now=now)
        return DurableRefreshSliceResult(cursor.entity_id, terminal)

    async def _run_group_full_chat_pair(
        self,
        cursor: EntityRefreshCursor,
        *,
        now: int,
    ) -> DurableRefreshSliceResult:
        """Acquire one legacy group envelope for independent profile projections."""
        context = self._profile_section_context(cursor, DialogType.GROUP)
        try:
            async with asyncio.timeout(self._deps.refresh_limits.per_rpc_seconds):
                full_profile, contact_overlap = await self._acquire_group_full_chat_pair(cursor)
        except RpcAttemptBudgetExhaustedError:
            self._observe_profile_section_failure(cursor, DialogType.GROUP, actual_attempts=0)
            raise
        except TelegramRpcThrottled as exc:
            _raise_if_latched(exc)
            terminal = await self._handle_profile_section_failure(
                _ProfileSectionFailure(
                    cursor=cursor,
                    entity_type=DialogType.GROUP,
                    context=context,
                    now=now,
                    reason="flood_wait",
                    retry_at=throttled_retry_at(now, exc.retry_after_seconds),
                )
            )
            return DurableRefreshSliceResult(cursor.entity_id, terminal)
        except (
            TimeoutError,
            OSError,
            RPCError,
            RuntimeError,
            TypeError,
            AttributeError,
            ValueError,
            sqlite3.Error,
        ) as exc:
            raise_if_flood_wait_error(exc)
            terminal = await self._handle_profile_section_failure(
                _ProfileSectionFailure(
                    cursor=cursor,
                    entity_type=DialogType.GROUP,
                    context=context,
                    now=now,
                    reason=(
                        "auth_scope_unavailable"
                        if isinstance(exc, _AuthScopeUnavailableError)
                        else type(exc).__name__.lower()
                    ),
                    retry_at=failure_retry_at(now),
                )
            )
            return DurableRefreshSliceResult(cursor.entity_id, terminal)

        if self._profile_scope_changed_during_acquisition(context.captured_scope):
            await self._write_profiles(
                lambda profiles: profiles.mark_section_failure(
                    cursor,
                    now=now,
                    reason="auth_scope_changed",
                    retry_at=scope_changed_retry_at(now),
                )
            )
            return DurableRefreshSliceResult(cursor.entity_id, DurableRefreshTerminal.FAILURE)

        full_profile = self._with_observation_metadata(full_profile, context.captured_scope)
        contact_overlap = self._with_observation_metadata(contact_overlap, context.captured_scope)
        committed = await self._write_profiles(
            lambda profiles: profiles.commit_group_full_chat_pair(
                cursor,
                full_profile,
                contact_overlap,
                now=now,
            )
        )
        self._observe_profile_section_commit(
            cursor,
            DialogType.GROUP,
            full_profile,
            committed=committed,
            actual_attempts=self._rpc_attempt_count() - context.attempt_start,
        )
        return DurableRefreshSliceResult(
            cursor.entity_id,
            self._success_if_refresh_finished(cursor, committed=committed),
        )

    async def _run_full_user_pair_refresh(
        self,
        cursor: EntityRefreshCursor,
        entity_type: DialogType,
        *,
        now: int,
    ) -> DurableRefreshSliceResult:
        identity = self._full_user_pair_identity(self._target_kind(entity_type))
        if identity is not None and await self._write_profiles(
            lambda profiles: profiles.reuse_full_user_pair(cursor, identity, now=now)
        ):
            self._observe_profile_pair(
                mode="enabled",
                eligible_pair=True,
                outcome="reused",
                pair_ready=True,
                local_satisfaction=True,
                prevented_request=True,
                reused_age_ms=self._pair_reused_age_ms(cursor, now=now),
            )
            terminal = self._success_if_refresh_finished(cursor, committed=True)
        else:
            reuse_rejection_reason = self._profiles.full_user_pair_reuse_rejection_reason(cursor, identity, now=now)
            terminal = await self._acquire_and_commit_full_user_pair(
                cursor,
                now=now,
                reuse_rejection_reason=reuse_rejection_reason,
            )
        return DurableRefreshSliceResult(cursor.entity_id, terminal)

    def _target_kind(self, entity_type: DialogType) -> TargetKind:
        return TargetKind.BOT if entity_type in {DialogType.BOT, DialogType.SERVICE} else TargetKind.USER

    async def _complete_paired_personal_channel(
        self,
        cursor: EntityRefreshCursor,
        entity_type: DialogType,
        *,
        now: int,
    ) -> DurableRefreshSliceResult:
        # The pair already captured the remote channel identity. Advancing this
        # ordered cursor is local work and must not repeat GetFullUser.
        identity = self._full_user_pair_identity(self._target_kind(entity_type))
        committed = await self._write_profiles(
            lambda profiles: profiles.complete_same_generation_section(cursor, identity, now=now)
        )
        if not committed:
            failed = await self._write_profiles(
                lambda profiles: profiles.mark_section_failure(
                    cursor,
                    now=now,
                    reason="pair_receipt_invalid",
                    retry_at=failure_retry_at(now),
                )
            )
            if not failed:
                return DurableRefreshSliceResult(cursor.entity_id, None)
            return DurableRefreshSliceResult(cursor.entity_id, DurableRefreshTerminal.FAILURE)
        return DurableRefreshSliceResult(
            cursor.entity_id,
            self._success_if_refresh_finished(cursor, committed=committed),
        )

    async def _complete_not_applicable_section(
        self,
        cursor: EntityRefreshCursor,
        *,
        now: int,
    ) -> DurableRefreshSliceResult:
        committed = await self._commit_not_applicable_section(cursor, now=now)
        return DurableRefreshSliceResult(
            cursor.entity_id,
            self._success_if_refresh_finished(cursor, committed=committed),
        )

    def _pair_enabled(
        self, cursor: EntityRefreshCursor, entity_type: DialogType, *, pair_mode: str | None = None
    ) -> bool:
        return (
            (pair_mode or cursor.pair_mode) == "enabled"
            and cursor.next_section == "full_profile"
            and cursor.pair_eligible
            and entity_type
            in {
                DialogType.USER,
                DialogType.BOT,
            }
        )

    def _is_paired_personal_channel_cursor(
        self,
        cursor: EntityRefreshCursor,
        *,
        entity_type: DialogType,
        pair_mode: str | None,
    ) -> bool:
        return (
            (pair_mode or cursor.pair_mode) == "enabled"
            and cursor.next_section == "personal_channel"
            and cursor.pair_eligible
            and entity_type in {DialogType.USER, DialogType.BOT}
        )

    def _stored_entity_type(self, entity_id: int, *, now: int) -> DialogType:
        stored = self._profiles.read(entity_id, now=now)
        raw_type = stored.detail.get("type") if stored is not None else None
        return DialogType.parse(raw_type if isinstance(raw_type, str) else None)

    async def _commit_not_applicable_section(self, cursor: EntityRefreshCursor, *, now: int) -> bool:
        return await self._write_profiles(
            lambda profiles: profiles.commit_section(
                cursor,
                EntitySectionCommit(
                    self._not_applicable_section_patch(cursor.next_section),
                    status="not_applicable",
                ),
                now=now,
            )
        )

    async def _acquire_and_commit_profile_section(
        self,
        cursor: EntityRefreshCursor,
        entity_type: DialogType,
        *,
        now: int,
    ) -> DurableRefreshTerminal | None:
        context = self._profile_section_context(cursor, entity_type)
        identity_baseline = capture_identity_baseline(self._deps.conn, cursor.entity_id)
        try:
            field_observation = (
                await self._reserve_profile_observation(context.captured_scope)
                if entity_type in {DialogType.USER, DialogType.BOT, DialogType.SERVICE}
                else None
            )
            async with asyncio.timeout(self._deps.refresh_limits.per_rpc_seconds):
                if entity_type is DialogType.CHANNEL and cursor.next_section == "full_profile":
                    result = await self._acquire_profile_section(cursor, entity_type, context=context)
                else:
                    result = await self._acquire_profile_section(cursor, entity_type, context=context)
        except RpcAttemptBudgetExhaustedError:
            self._observe_profile_section_failure(cursor, entity_type, actual_attempts=0)
            raise
        except TelegramRpcThrottled as exc:
            _raise_if_latched(exc)
            retry_at = throttled_retry_at(now, exc.retry_after_seconds)
            return await self._handle_profile_section_failure(
                _ProfileSectionFailure(
                    cursor=cursor,
                    entity_type=entity_type,
                    context=context,
                    now=now,
                    reason="flood_wait",
                    retry_at=retry_at,
                )
            )
        except (TimeoutError, RPCError, RuntimeError, TypeError, AttributeError, ValueError, sqlite3.Error) as exc:
            raise_if_flood_wait_error(exc)
            return await self._handle_profile_section_failure(
                _ProfileSectionFailure(
                    cursor=cursor,
                    entity_type=entity_type,
                    context=context,
                    now=now,
                    reason=_profile_section_failure_reason(cursor.next_section, exc),
                    retry_at=failure_retry_at(now),
                ),
                guard_scope=_scope_bound_profile_failure(cursor.next_section, exc),
            )
        if self._profile_scope_changed_during_acquisition(context.captured_scope):
            await self._write_profiles(
                lambda profiles: profiles.mark_section_failure(
                    cursor,
                    now=now,
                    reason="auth_scope_changed",
                    retry_at=scope_changed_retry_at(now),
                )
            )
            return DurableRefreshTerminal.FAILURE
        if result.dialog_identity_observation is not None:
            result = dataclass_replace(result, dialog_identity_baseline_revision=identity_baseline)
        result = self._with_observation_metadata(result, context.captured_scope)
        result = dataclass_replace(result, field_observation=field_observation)
        committed = await self._commit_profile_section_result(cursor, entity_type, result, context, now=now)
        return self._success_if_refresh_finished(cursor, committed=committed)

    def _profile_section_context(
        self,
        cursor: EntityRefreshCursor,
        entity_type: DialogType,
    ) -> _ProfileSectionContext:
        return _ProfileSectionContext(
            captured_scope=self._capture_pair_scope(),
            attempt_start=self._rpc_attempt_count(),
            pair_section=cursor.next_section in {"full_profile", "personal_channel"},
            pair_mode=cursor.pair_mode or ("enabled" if self._deps.enable_full_user_pair else "disabled"),
            eligible_pair=cursor.pair_eligible and entity_type in {DialogType.USER, DialogType.BOT},
        )

    async def _handle_profile_section_failure(
        self, failure: _ProfileSectionFailure, *, guard_scope: bool = False
    ) -> DurableRefreshTerminal | None:
        failed = await self._write_profiles(
            lambda profiles: self._persist_profile_section_failure(profiles, failure, guard_scope=guard_scope)
        )
        actual_attempts = self._rpc_attempt_count() - failure.context.attempt_start
        if failure.context.pair_section and failure.context.eligible_pair and failure.context.pair_mode == "disabled":
            await self._write_profiles(
                lambda profiles: profiles.record_pair_attempt(
                    failure.cursor,
                    failure.cursor.next_section,
                    actual_attempts=actual_attempts,
                )
            )
        self._observe_profile_section_failure(failure.cursor, failure.entity_type, actual_attempts=actual_attempts)
        return DurableRefreshTerminal.FAILURE if failed else None

    def _persist_profile_section_failure(
        self, profiles: EntityProfileRepository, failure: _ProfileSectionFailure, *, guard_scope: bool
    ) -> bool:
        changed = guard_scope and self._profile_scope_changed_during_acquisition(failure.context.captured_scope)
        return profiles.mark_section_failure(
            failure.cursor,
            now=failure.now,
            reason="auth_scope_changed" if changed else failure.reason,
            retry_at=scope_changed_retry_at(failure.now) if changed else failure.retry_at,
        )

    def _profile_scope_changed_during_acquisition(self, captured_scope: TelegramAuthScope | None) -> bool:
        return self._deps.full_user_auth_scope is not None and captured_scope != self._capture_pair_scope()

    async def _commit_profile_section_result(
        self,
        cursor: EntityRefreshCursor,
        entity_type: DialogType,
        result: EntitySectionCommit,
        context: _ProfileSectionContext,
        *,
        now: int,
    ) -> bool:
        if cursor.next_section == "contact_overlap" and result.reason == CHANNEL_CONTACT_UNSUPPORTED_REASON:
            return await self._write_profiles(
                lambda profiles: self._commit_unsupported_contact_overlap(profiles, cursor, result, context, now=now)
            )
        actual_attempts = self._rpc_attempt_count() - context.attempt_start
        if context.pair_section and context.eligible_pair and context.pair_mode == "disabled":
            committed, summary = await self._write_profiles(
                lambda profiles: profiles.commit_section_with_pair_measurement(
                    cursor,
                    result,
                    now=now,
                    outcome=self._section_commit_outcome(cursor.next_section, result),
                    actual_attempts=actual_attempts,
                )
            )
            if summary is not None:
                self._emit_pair_summary(summary)
            await self._publish_profile_linked_chat_fact(cursor, entity_type, context, committed, now=now)
            return committed
        committed = await self._write_profiles(lambda profiles: profiles.commit_section(cursor, result, now=now))
        await self._publish_profile_linked_chat_fact(cursor, entity_type, context, committed, now=now)
        self._observe_profile_section_commit(
            cursor,
            entity_type,
            result,
            committed=committed,
            actual_attempts=actual_attempts,
        )
        return committed

    def _commit_unsupported_contact_overlap(
        self,
        profiles: EntityProfileRepository,
        cursor: EntityRefreshCursor,
        result: EntitySectionCommit,
        context: _ProfileSectionContext,
        *,
        now: int,
    ) -> bool:
        if self._profile_scope_changed_during_acquisition(context.captured_scope):
            profiles.mark_section_failure(
                cursor, now=now, reason="auth_scope_changed", retry_at=scope_changed_retry_at(now)
            )
            return False
        return profiles.commit_unsupported_contact_overlap(cursor, result, now=now)

    async def _publish_profile_linked_chat_fact(
        self,
        cursor: EntityRefreshCursor,
        entity_type: DialogType,
        context: _ProfileSectionContext,
        committed: bool,
        *,
        now: int,
    ) -> None:
        if (
            committed
            and entity_type is DialogType.CHANNEL
            and cursor.next_section == "full_profile"
            and context.linked_chat_fact_captured
            and context.linked_chat_observation_usable
        ):

            def write(conn: sqlite3.Connection) -> bool:
                with write_transaction(conn):
                    return linked_chat_fact_owner.publish(
                        conn,
                        cursor.entity_id,
                        cast(int, context.linked_chat_fact_generation),
                        _raw_linked_chat_channel_id(context.linked_chat_id),
                        now,
                    )

            published = await self._deps.writer.run(write)
            self._offer_linked_chat_followups(published, context.linked_chat_id is not None)

    def _offer_linked_chat_followups(self, published: bool, has_link: bool) -> None:
        if not published or self._demand_sink is None:
            return
        if has_link:
            offer_durable_demand(
                self._demand_sink,
                DemandKind.SCHEDULED_DISCOVERY,
                DemandKind.COLD_PEER_PAGE,
            )
        else:
            offer_durable_demand(self._demand_sink, DemandKind.SCHEDULED_DISCOVERY)

    async def _acquire_and_commit_full_user_pair(
        self,
        cursor: EntityRefreshCursor,
        *,
        now: int,
        reuse_rejection_reason: str | None = None,
    ) -> DurableRefreshTerminal | None:
        """Acquire and atomically commit both User profile projections."""
        attempt_start = self._rpc_attempt_count()
        pair_mode = cursor.pair_mode or "enabled"
        try:
            async with asyncio.timeout(self._deps.refresh_limits.per_rpc_seconds):
                full_profile, personal_channel = await self._acquire_full_user_pair(cursor)
        except RpcAttemptBudgetExhaustedError:
            await self._record_full_user_pair_failure(cursor, pair_mode, attempt_start, reuse_rejection_reason)
            raise
        except TelegramRpcThrottled as exc:
            _raise_if_latched(exc)
            return await self._handle_full_user_pair_failure(
                _PairFailure(
                    cursor=cursor,
                    now=now,
                    pair_mode=pair_mode,
                    attempt_start=attempt_start,
                    reason="flood_wait",
                    retry_at=throttled_retry_at(now, exc.retry_after_seconds),
                    reuse_rejection_reason=reuse_rejection_reason,
                )
            )
        except (TimeoutError, OSError, RPCError, RuntimeError, TypeError, AttributeError, ValueError) as exc:
            raise_if_flood_wait_error(exc)
            return await self._handle_full_user_pair_failure(
                _PairFailure(
                    cursor=cursor,
                    now=now,
                    pair_mode=pair_mode,
                    attempt_start=attempt_start,
                    reason=type(exc).__name__.lower(),
                    retry_at=failure_retry_at(now),
                    reuse_rejection_reason=reuse_rejection_reason,
                )
            )
        if self._pair_scope_mismatch(cursor, full_profile, now=now):
            return await self._handle_full_user_pair_failure(
                _PairFailure(
                    cursor=cursor,
                    now=now,
                    pair_mode=pair_mode,
                    attempt_start=attempt_start,
                    reason="auth_scope_changed",
                    retry_at=scope_changed_retry_at(now),
                    reuse_rejection_reason="auth_scope_changed",
                )
            )
        committed = await self._commit_full_user_pair_or_raise(
            _PairCommitRequest(
                cursor=cursor,
                full_profile=full_profile,
                personal_channel=personal_channel,
                now=now,
                pair_mode=pair_mode,
                attempt_start=attempt_start,
                reuse_rejection_reason=reuse_rejection_reason,
            )
        )
        actual_attempts = self._rpc_attempt_count() - attempt_start
        summary = await self._record_full_user_pair_commit(
            _PairMeasurement(
                cursor=cursor,
                full_profile=full_profile,
                personal_channel=personal_channel,
                now=now,
                pair_mode=pair_mode,
                actual_attempts=actual_attempts,
                committed=committed,
            )
        )
        if summary is not None:
            summary["reuse_rejection_reason"] = reuse_rejection_reason
            self._emit_pair_summary(summary)
        elif not committed:
            self._observe_profile_pair(
                mode=pair_mode,
                eligible_pair=cursor.pair_eligible,
                outcome="stale_writer_rejected",
                actual_attempts=actual_attempts,
                reuse_rejection_reason=reuse_rejection_reason,
                stale_writer_rejected=True,
            )
        return self._success_if_refresh_finished(cursor, committed=committed)

    async def _record_full_user_pair_failure(
        self,
        cursor: EntityRefreshCursor,
        pair_mode: str,
        attempt_start: int,
        reuse_rejection_reason: str | None,
    ) -> None:
        actual_attempts = self._rpc_attempt_count() - attempt_start
        await self._write_profiles(
            lambda profiles: profiles.record_pair_attempt(cursor, "full_profile", actual_attempts=actual_attempts)
        )
        self._observe_profile_pair(
            mode=pair_mode,
            eligible_pair=cursor.pair_eligible,
            outcome="failed",
            actual_attempts=actual_attempts,
            reuse_rejection_reason=reuse_rejection_reason,
        )

    async def _handle_full_user_pair_failure(self, failure: _PairFailure) -> DurableRefreshTerminal | None:
        failed = await self._write_profiles(
            lambda profiles: profiles.mark_section_failure(
                failure.cursor,
                now=failure.now,
                reason=failure.reason,
                retry_at=failure.retry_at,
            )
        )
        await self._record_full_user_pair_failure(
            failure.cursor,
            failure.pair_mode,
            failure.attempt_start,
            failure.reuse_rejection_reason,
        )
        return DurableRefreshTerminal.FAILURE if failed else None

    def _pair_scope_mismatch(
        self,
        cursor: EntityRefreshCursor,
        full_profile: EntitySectionCommit,
        *,
        now: int,
    ) -> bool:
        if self._deps.full_user_auth_scope is None:
            return False
        current_scope = self._capture_pair_scope()
        expected_identity = full_profile.evidence.identity if full_profile.evidence is not None else None
        expected_identity_for_scope = self._full_user_pair_identity(
            self._target_kind(self._stored_entity_type(cursor.entity_id, now=now)),
            scope=current_scope,
        )
        return current_scope is None or expected_identity != expected_identity_for_scope

    async def _commit_full_user_pair_or_raise(self, request: _PairCommitRequest) -> bool:
        try:
            return await self._write_profiles(
                lambda profiles: profiles.commit_full_user_pair(
                    request.cursor,
                    request.full_profile,
                    request.personal_channel,
                    now=request.now,
                )
            )
        except Exception:
            await self._record_full_user_pair_failure(
                request.cursor,
                request.pair_mode,
                request.attempt_start,
                request.reuse_rejection_reason,
            )
            raise

    async def _record_full_user_pair_commit(self, measurement: _PairMeasurement) -> dict[str, object] | None:
        if not measurement.committed:
            return None
        latency = pair_readiness_latency_ms(measurement.full_profile, measurement.personal_channel)

        def record(profiles: EntityProfileRepository) -> dict[str, object] | None:
            full_summary = profiles.record_pair_section_outcome(
                measurement.cursor,
                "full_profile",
                outcome=self._evidence_outcome(measurement.full_profile) or "unavailable",
                actual_attempts=measurement.actual_attempts,
                ready_at=measurement.now,
                readiness_latency_ms=latency,
            )
            personal_summary = profiles.record_pair_section_outcome(
                measurement.cursor,
                "personal_channel",
                outcome=self._evidence_outcome(measurement.personal_channel) or "unavailable",
                actual_attempts=0,
                ready_at=measurement.now,
                readiness_latency_ms=latency,
            )
            return personal_summary or full_summary

        summary = await self._write_profiles(record)
        if summary is None and not self._profiles._refresh_has("pair_mode"):
            return self._historical_pair_summary(
                measurement,
                latency=latency,
            )
        return summary

    def _historical_pair_summary(self, measurement: _PairMeasurement, *, latency: float | None) -> dict[str, object]:
        return {
            "mode": measurement.pair_mode,
            "eligible_pair": measurement.cursor.pair_eligible,
            "outcome": "committed",
            "actual_attempts": measurement.actual_attempts,
            "retries": max(0, measurement.actual_attempts - 1),
            "full_profile_outcome": self._evidence_outcome(measurement.full_profile) or "unavailable",
            "personal_channel_outcome": self._evidence_outcome(measurement.personal_channel) or "unavailable",
            "pair_ready": True,
            "pair_readiness_latency_ms": latency,
        }

    async def _acquire_full_user_pair(
        self,
        cursor: EntityRefreshCursor,
    ) -> tuple[EntitySectionCommit, EntitySectionCommit]:
        captured_scope = self._capture_pair_scope()
        identity_baseline = capture_identity_baseline(self._deps.conn, cursor.entity_id)
        now = self._deps.now_provider()
        target_kind = (
            TargetKind.BOT
            if self._stored_entity_type(cursor.entity_id, now=int(now)) is DialogType.BOT
            else TargetKind.USER
        )
        field_observation = await self._reserve_profile_observation(captured_scope)
        normalized = await self._deps.user_profile_port.fetch_user_profile(
            cursor.entity_id, target_kind, owner_account_id=captured_scope.account_id if captured_scope else None
        )
        self._validate_full_user_acquisition(normalized, captured_scope)
        identity = self._full_user_pair_identity(target_kind, scope=captured_scope)
        full_profile = self._normalized_full_profile_commit(
            normalized.full_profile,
            cursor,
            identity,
            current_photo=normalized.current_photo,
            semantic_snapshot=normalized.semantic_snapshot,
        )
        folder_id = full_profile.detail_patch.get("folder_id")
        folder_name = await self._resolve_folder_name(folder_id if isinstance(folder_id, int) else None)
        identity_observation = normalized.full_profile.dialog_identity_observation
        full_profile = dataclass_replace(
            full_profile,
            detail_patch={**full_profile.detail_patch, "folder_name": folder_name},
            dialog_identity_observation=identity_observation,
            dialog_identity_baseline_revision=identity_baseline,
            field_observation=field_observation,
            semantic_snapshot=normalized.semantic_snapshot,
        )
        personal_channel = self._normalized_personal_channel_commit(
            normalized.personal_channel,
            cursor,
            identity,
        )
        personal_channel = dataclass_replace(personal_channel, field_observation=field_observation)
        return full_profile, personal_channel

    async def _reserve_profile_observation(self, scope: TelegramAuthScope | None) -> ProfileFieldObservation | None:
        if scope is None:
            if self._deps.full_user_auth_scope is not None:
                raise _AuthScopeUnavailableError("authenticated session scope is unavailable")
            return None
        order = await self._deps.writer.run(allocate_observation_order)
        return ProfileFieldObservation(scope.account_id, order, int(self._deps.now_provider()), "rpc")

    def _validate_full_user_acquisition(
        self,
        observation: UserProfileObservation,
        captured_scope: TelegramAuthScope | None,
    ) -> None:
        if observation.full_profile.status is ProjectionStatus.UNAVAILABLE:
            raise ValueError(observation.full_profile.reason or "full user payload is unavailable")
        if self._deps.full_user_auth_scope is not None:
            verified_scope = self._capture_pair_scope()
            if captured_scope is None or verified_scope != captured_scope:
                raise _AuthScopeUnavailableError("authenticated session scope changed during acquisition")

    def _capture_pair_scope(self) -> TelegramAuthScope | None:
        provider = self._deps.full_user_auth_scope
        if provider is None:
            return None
        scope = provider()
        return scope if isinstance(scope, TelegramAuthScope) else None

    def _with_observation_metadata(
        self, commit: EntitySectionCommit, scope: TelegramAuthScope | None
    ) -> EntitySectionCommit:
        """Attach ownership to every account-derived profile observation."""
        return dataclass_replace(
            commit,
            observation_owner_account_id=scope.account_id if scope is not None else None,
            observation_auth_scope=scope.as_private_mapping() if scope is not None else None,
            ownership_observed=self._deps.full_user_auth_scope is not None,
        )

    @staticmethod
    def _scope_account_id(identity: Mapping[str, object] | None) -> int | None:
        if not isinstance(identity, Mapping):
            return None
        raw_scope = identity.get("auth_scope")
        if not isinstance(raw_scope, Mapping):
            return None
        account_id = raw_scope.get("account_id")
        return account_id if isinstance(account_id, int) and not isinstance(account_id, bool) else None

    @staticmethod
    def _scope_mapping(identity: Mapping[str, object] | None) -> Mapping[str, object] | None:
        if not isinstance(identity, Mapping):
            return None
        raw_scope = identity.get("auth_scope")
        return dict(raw_scope) if isinstance(raw_scope, Mapping) else None

    def _full_user_pair_identity(
        self,
        target_kind: TargetKind,
        *,
        scope: TelegramAuthScope | None = None,
    ) -> dict[str, object] | None:
        """Return the private receipt identity for one paired projection."""
        if self._deps.full_user_auth_scope is not None:
            scope = self._capture_pair_scope() if scope is None else scope
        if scope is None:
            return None
        return {
            "auth_scope": scope.as_private_mapping(),
            "entity_type": target_kind.value,
            "endpoint": FULL_USER_ENDPOINT,
            "normalization_version": NORMALIZATION_VERSION,
            "declared_fields": {
                "full_profile": list(FULL_PROFILE_OWNED_FIELDS),
                "personal_channel": list(PERSONAL_CHANNEL_OWNED_FIELDS),
            },
        }

    @staticmethod
    def _projection_evidence(
        outcome: ProjectionOutcome,
        *,
        cursor: EntityRefreshCursor,
        identity: Mapping[str, object] | None,
    ) -> ProfileAcquisitionEvidence | None:
        provenance = outcome.provenance
        if provenance is None:
            return None
        observation = provenance.observation
        started = observation.started_at
        completed = observation.completed_at
        if started is None or completed is None:
            return None
        return ProfileAcquisitionEvidence(
            generation=cursor.generation,
            outcome=outcome.status.value,
            provenance={
                "endpoint": provenance.endpoint,
                "declared_fields": list(provenance.declared_fields),
                "materialized_fields": list(provenance.materialized_fields),
                "authoritative": provenance.authoritative,
            },
            normalization_version=provenance.normalization_version,
            observation_started_at=int(started),
            observation_completed_at=int(completed),
            identity=identity,
        )

    @staticmethod
    def _finalize_channel_evidence(
        evidence: ProfileAcquisitionEvidence | None,
        *,
        outcome: str,
        authoritative: bool,
    ) -> ProfileAcquisitionEvidence | None:
        if evidence is None:
            return None
        provenance = evidence.provenance
        if provenance is None:
            return dataclass_replace(evidence, outcome=outcome)
        final_provenance = dict(provenance)
        final_provenance["authoritative"] = authoritative
        return dataclass_replace(evidence, outcome=outcome, provenance=final_provenance)

    def _observe_profile_pair(  # noqa: PLR0913 - mirrors the bounded hook contract
        self,
        *,
        mode: str,
        eligible_pair: bool,
        outcome: str,
        actual_attempts: int = 0,
        retries: int = 0,
        full_profile_outcome: str | None = None,
        personal_channel_outcome: str | None = None,
        pair_ready: bool = False,
        pair_readiness_latency_ms: float | None = None,
        local_satisfaction: bool = False,
        prevented_request: bool = False,
        reuse_rejection_reason: str | None = None,
        reused_age_ms: float | None = None,
        stale_writer_rejected: bool = False,
    ) -> None:
        observer = self._deps.profile_observer
        if observer is None or outcome == "failed":
            return
        try:
            observer.observe_profile_pair(
                mode=mode,
                eligible_pair=eligible_pair,
                outcome=outcome,
                actual_attempts=actual_attempts,
                retries=retries,
                full_profile_outcome=full_profile_outcome,
                personal_channel_outcome=personal_channel_outcome,
                pair_ready=pair_ready,
                pair_readiness_latency_ms=pair_readiness_latency_ms,
                local_satisfaction=local_satisfaction,
                prevented_request=prevented_request,
                reuse_rejection_reason=reuse_rejection_reason,
                reused_age_ms=reused_age_ms,
                stale_writer_rejected=stale_writer_rejected,
            )
        except Exception:  # noqa: BLE001 - telemetry cannot affect profile correctness
            self._deps.logger.debug("entity_profile_pair_observation_failed")

    def _emit_pair_summary(self, summary: Mapping[str, object]) -> None:
        """Send a repository snapshot through the typed observation boundary."""
        self._observe_profile_pair(
            mode=cast(str, summary.get("mode")),
            eligible_pair=cast(bool, summary.get("eligible_pair")),
            outcome=cast(str, summary.get("outcome")),
            actual_attempts=cast(int, summary.get("actual_attempts", 0)),
            retries=cast(int, summary.get("retries", 0)),
            full_profile_outcome=cast(str | None, summary.get("full_profile_outcome")),
            personal_channel_outcome=cast(str | None, summary.get("personal_channel_outcome")),
            pair_ready=cast(bool, summary.get("pair_ready", False)),
            pair_readiness_latency_ms=cast(float | None, summary.get("pair_readiness_latency_ms")),
            local_satisfaction=cast(bool, summary.get("local_satisfaction", False)),
            prevented_request=cast(bool, summary.get("prevented_request", False)),
            reuse_rejection_reason=cast(str | None, summary.get("reuse_rejection_reason")),
            reused_age_ms=cast(float | None, summary.get("reused_age_ms")),
            stale_writer_rejected=cast(bool, summary.get("stale_writer_rejected", False)),
        )

    @staticmethod
    def _rpc_attempt_count() -> int:
        """Read dispatch accounting owned by the current RPC scope."""
        scope = current_rpc_scope()
        budget = scope.attempt_budget
        return 0 if budget is None else budget.attempts

    @staticmethod
    def _section_commit_outcome(section: str, result: EntitySectionCommit) -> str:
        """Classify the committed projection, after normalization is final."""
        if result.status != "fresh":
            return "unavailable"
        if section == "personal_channel":
            channel_id = result.detail_patch.get("personal_channel_id")
            return "absent" if channel_id is None else "usable"
        return "usable"

    @staticmethod
    def _evidence_outcome(commit: EntitySectionCommit) -> str | None:
        evidence = commit.evidence
        if evidence is None or evidence.outcome not in {"usable", "partial", "absent", "unavailable"}:
            return None
        return evidence.outcome

    def _pair_reused_age_ms(self, cursor: EntityRefreshCursor, *, now: int) -> float | None:
        starts: list[int] = []
        for section in ("full_profile", "personal_channel"):
            evidence = self._profiles.read_section_evidence(cursor.entity_id, section)
            started_at = evidence.get("observation_started_at") if evidence is not None else None
            if isinstance(started_at, int) and not isinstance(started_at, bool) and started_at >= 0:
                starts.append(started_at)
        if len(starts) != _PAIR_SECTION_COUNT:
            return None
        return max(0, now - min(starts)) * 1000.0

    def _observe_profile_section_failure(
        self,
        cursor: EntityRefreshCursor,
        entity_type: DialogType,
        *,
        actual_attempts: int,
    ) -> None:
        if cursor.next_section not in {"full_profile", "personal_channel"}:
            return
        if cursor.pair_eligible and entity_type in {DialogType.USER, DialogType.BOT}:
            # Pair measurement is a completed-generation denominator.  A
            # retryable section failure remains durable state and is folded
            # into the eventual summary once the projection commits.
            return
        self._observe_profile_pair(
            mode="enabled" if self._deps.enable_full_user_pair else "disabled",
            eligible_pair=cursor.pair_eligible and entity_type in {DialogType.USER, DialogType.BOT},
            outcome="failed",
            actual_attempts=actual_attempts,
        )

    def _observe_profile_section_commit(
        self,
        cursor: EntityRefreshCursor,
        entity_type: DialogType,
        result: EntitySectionCommit,
        *,
        committed: bool,
        actual_attempts: int,
    ) -> None:
        if cursor.next_section not in {"full_profile", "personal_channel"}:
            return
        section_outcome = "usable" if result.status == "fresh" else "unavailable"
        self._observe_profile_pair(
            mode="enabled" if self._deps.enable_full_user_pair else "disabled",
            eligible_pair=cursor.pair_eligible and entity_type in {DialogType.USER, DialogType.BOT},
            outcome="section_committed" if committed else "stale_writer_rejected",
            actual_attempts=actual_attempts,
            full_profile_outcome=section_outcome if cursor.next_section == "full_profile" else None,
            personal_channel_outcome=section_outcome if cursor.next_section == "personal_channel" else None,
            stale_writer_rejected=not committed,
        )

    def _normalized_full_profile_commit(
        self,
        outcome: ProjectionOutcome,
        cursor: EntityRefreshCursor,
        identity: Mapping[str, object] | None,
        *,
        current_photo: ChatCurrentPhoto | None = None,
        semantic_snapshot: ProfileSemanticSnapshot | None = None,
    ) -> EntitySectionCommit:
        payload = dict(outcome.payload or {})
        private_payload = _profile_photo_patch(current_photo, semantic_snapshot)
        payload.update(private_payload)
        return EntitySectionCommit(
            payload,
            identity_patch=outcome.identity_patch,
            status="fresh",
            payload=private_payload,
            evidence=self._projection_evidence(outcome, cursor=cursor, identity=identity),
            observation_owner_account_id=self._scope_account_id(identity),
            observation_auth_scope=self._scope_mapping(identity),
            ownership_observed=self._deps.full_user_auth_scope is not None,
        )

    def _normalized_personal_channel_commit(
        self,
        outcome: ProjectionOutcome,
        cursor: EntityRefreshCursor,
        identity: Mapping[str, object] | None,
    ) -> EntitySectionCommit:
        context = self._projection_commit_context(outcome, cursor=cursor, identity=identity)
        if outcome.status is ProjectionStatus.ABSENT:
            return self._absent_personal_channel_commit(context)
        channel_id = context.payload.get("personal_channel_id")
        if not isinstance(channel_id, int) or channel_id <= 0:
            return self._unavailable_personal_channel_commit(
                context,
                reason=outcome.reason or "personal_channel_unavailable",
                evidence_outcome="partial",
            )
        if outcome.status is ProjectionStatus.PARTIAL:
            return self._missing_channel_metadata_commit(
                context,
                channel_id=channel_id,
                reason=outcome.reason or "personal_channel_unavailable",
            )
        dialog_id = self._normalize_channel_dialog_id(channel_id)
        metadata, metadata_source = self._personal_channel_metadata(
            dialog_id=dialog_id,
            normalized=context.payload,
        )
        if metadata is None:
            return self._missing_channel_metadata_commit(
                context,
                channel_id=channel_id,
                reason=outcome.reason or "channel_metadata_unavailable",
            )
        local_preview, local_reason = self._latest_local_personal_channel_post(dialog_id)
        return self._usable_personal_channel_commit(
            context,
            _PersonalChannelCard(
                outcome=outcome,
                channel_id=channel_id,
                dialog_id=dialog_id,
                metadata=metadata,
                local_preview=local_preview,
                local_reason=local_reason,
                metadata_source=metadata_source,
            ),
        )

    def _projection_commit_context(
        self,
        outcome: ProjectionOutcome,
        *,
        cursor: EntityRefreshCursor,
        identity: Mapping[str, object] | None,
    ) -> _ProjectionCommitContext:
        return _ProjectionCommitContext(
            payload=dict(outcome.payload or {}),
            evidence=self._projection_evidence(outcome, cursor=cursor, identity=identity),
            observation_owner_account_id=self._scope_account_id(identity),
            observation_auth_scope=self._scope_mapping(identity),
            ownership_observed=self._deps.full_user_auth_scope is not None,
        )

    @staticmethod
    def _absent_personal_channel_commit(context: _ProjectionCommitContext) -> EntitySectionCommit:
        return EntitySectionCommit(
            {
                "personal_channel_id": None,
                "personal_channel_message": None,
                "personal_channel": None,
                "personal_channel_unavailable_reason": None,
            },
            status="fresh",
            payload=None,
            evidence=context.evidence,
            observation_owner_account_id=context.observation_owner_account_id,
            observation_auth_scope=context.observation_auth_scope,
            ownership_observed=context.ownership_observed,
        )

    def _unavailable_personal_channel_commit(
        self,
        context: _ProjectionCommitContext,
        *,
        reason: str,
        evidence_outcome: str,
    ) -> EntitySectionCommit:
        return EntitySectionCommit(
            {},
            status="unavailable",
            reason=reason,
            payload=context.payload,
            evidence=self._finalize_channel_evidence(
                context.evidence,
                outcome=evidence_outcome,
                authoritative=False,
            ),
            observation_owner_account_id=context.observation_owner_account_id,
            observation_auth_scope=context.observation_auth_scope,
            ownership_observed=context.ownership_observed,
        )

    def _missing_channel_metadata_commit(
        self,
        context: _ProjectionCommitContext,
        *,
        channel_id: int,
        reason: str,
    ) -> EntitySectionCommit:
        return EntitySectionCommit(
            {
                "personal_channel_id": channel_id,
                "personal_channel_message": context.payload.get("personal_channel_message"),
                "personal_channel": None,
                "personal_channel_unavailable_reason": reason,
            },
            status="unavailable",
            reason=reason,
            payload=context.payload,
            evidence=self._finalize_channel_evidence(context.evidence, outcome="unavailable", authoritative=False),
            observation_owner_account_id=context.observation_owner_account_id,
            observation_auth_scope=context.observation_auth_scope,
            ownership_observed=context.ownership_observed,
        )

    def _usable_personal_channel_commit(
        self,
        context: _ProjectionCommitContext,
        card_context: _PersonalChannelCard,
    ) -> EntitySectionCommit:
        card = _compact_dict(
            {
                "channel_id": card_context.channel_id,
                "dialog_id": card_context.dialog_id,
                "title": card_context.metadata.get("title"),
                "username": card_context.metadata.get("username"),
                "url": self._tme_url(cast(str | None, card_context.metadata.get("username"))),
                "metadata_source": card_context.metadata_source,
                "attached_message_id": context.payload.get("personal_channel_message"),
                "latest_or_attached_post": card_context.local_preview,
                "post_preview_unavailable_reason": (
                    card_context.local_reason if card_context.local_preview is None else None
                ),
            }
        )
        usable = card_context.outcome.status is ProjectionStatus.USABLE
        return EntitySectionCommit(
            {
                "personal_channel_id": card_context.channel_id,
                "personal_channel_message": context.payload.get("personal_channel_message"),
                "personal_channel": card,
                "personal_channel_unavailable_reason": None,
            },
            status="fresh" if usable else "unavailable",
            reason=None if usable else card_context.outcome.reason,
            payload=card,
            evidence=self._finalize_channel_evidence(
                context.evidence,
                outcome="usable" if usable else "partial",
                authoritative=usable,
            ),
            observation_owner_account_id=context.observation_owner_account_id,
            observation_auth_scope=context.observation_auth_scope,
            ownership_observed=context.ownership_observed,
        )

    async def _acquire_durable_refresh_core(
        self,
        cursor: EntityRefreshCursor,
        *,
        now: int,
    ) -> DurableRefreshTerminal | None:
        """Persist one resolved core so later raw section requests can resume by id."""
        identity_baseline = capture_identity_baseline(self._deps.conn, cursor.entity_id)
        identity_started_at = int(self._deps.now_provider())
        try:
            entity, error = await self._resolve_entity(cursor.entity_id)
        except RpcAttemptBudgetExhaustedError:
            raise
        except TelegramRpcThrottled as exc:
            _raise_if_latched(exc)
            retry_at = throttled_retry_at(now, exc.retry_after_seconds)
            await self._write_profiles(
                lambda profiles: profiles.mark_refresh_failure(
                    cursor.entity_id,
                    now=now,
                    reason="flood_wait",
                    retry_at=retry_at,
                )
            )
            return self._persisted_failure(cursor.entity_id, now=now)
        if entity is None or error is not None:
            if error is not None and error.get("error") == "entity_not_found":
                await self._write_profiles(lambda profiles: profiles.mark_refresh_not_found(cursor.entity_id, now=now))
                return DurableRefreshTerminal.FAILURE
            reason, retry_at = self._refresh_failure_details(error, now=now)
            await self._write_profiles(
                lambda profiles: profiles.mark_refresh_failure(
                    cursor.entity_id,
                    now=now,
                    reason=reason,
                    retry_at=retry_at,
                )
            )
            return self._persisted_failure(cursor.entity_id, now=now)
        core = self._core_from_entity(entity)
        core["id"] = cursor.entity_id
        committed = await self._write_profiles(
            lambda profiles: profiles.commit_core_acquisition(
                cursor,
                core,
                next_acquisition_cursor=cursor.acquisition_cursor + 1,
                now=now,
                dialog_identity_observation=(
                    observe_dialog_identity(
                        entity,
                        dialog_id=cursor.entity_id,
                        source="profile",
                        observed_at=identity_started_at,
                    )
                    if self._deps.self_id != cursor.entity_id
                    else None
                ),
                dialog_identity_baseline_revision=identity_baseline,
            )
        )
        if not committed:
            return None
        return None

    @staticmethod
    def _refresh_failure_details(error: Mapping[str, object] | None, *, now: int) -> tuple[str, int]:
        retry_after = error.get("_retry_after_seconds") if error is not None else None
        if isinstance(retry_after, int) and retry_after > 0:
            return "flood_wait", throttled_retry_at(now, retry_after)
        return "entity_unavailable", failure_retry_at(now)

    def _success_if_refresh_finished(
        self,
        cursor: EntityRefreshCursor,
        *,
        committed: bool,
    ) -> DurableRefreshTerminal | None:
        if committed and self._profiles.refresh_is_complete(cursor):
            return DurableRefreshTerminal.SUCCESS
        return None

    def _persisted_failure(self, entity_id: int, *, now: int) -> DurableRefreshTerminal | None:
        state = self._profiles.refresh_state(entity_id, now=now)
        return DurableRefreshTerminal.FAILURE if state is not None and state.get("status") == "failed" else None

    async def _acquire_profile_section(
        self,
        cursor: EntityRefreshCursor,
        entity_type: DialogType,
        *,
        context: _ProfileSectionContext | None = None,
    ) -> EntitySectionCommit:
        section = cursor.next_section
        if section in PROFILE_COLLECTION_SECTIONS and entity_type in {DialogType.USER, DialogType.BOT}:
            reference = self._collection_user_reference(cursor.entity_id, section)
            state = json.loads(cursor.page_cursor_json) if cursor.page_cursor_json else {}
            page = await cast(ProfileCollectionPort, self._deps.user_profile_port).fetch_profile_collection_page(
                reference, section, state.get("request", {})
            )
            return EntitySectionCommit({}, collection_page=page)
        if section == "full_profile":
            return await self._acquire_full_profile(cursor.entity_id, entity_type, context=context)
        if section == "common_chats":
            return await self._acquire_common_chats(cursor.entity_id)
        if section == "contact_overlap":
            return await self._acquire_contact_overlap(cursor, entity_type)
        if section == "avatar_history":
            return await self._acquire_avatar_history(cursor.entity_id, entity_type)
        if section == "personal_channel":
            return await self._acquire_personal_channel(cursor.entity_id, entity_type)
        raise ValueError(f"unsupported entity profile section: {section}")

    @staticmethod
    def _section_applies(entity_type: DialogType, section: str) -> bool:
        if section in {"saved_music", "saved_gifts", "current_stories", "pinned_stories"}:
            return entity_type in {DialogType.USER, DialogType.BOT}
        if section in {"full_profile", "avatar_history"}:
            return entity_type is not DialogType.UNKNOWN
        if section == "common_chats":
            return entity_type in {DialogType.USER, DialogType.BOT, DialogType.SERVICE}
        if section == "personal_channel":
            return entity_type in {DialogType.USER, DialogType.BOT}
        if section == "contact_overlap":
            return entity_type in {
                DialogType.CHANNEL,
                DialogType.SUPERGROUP,
                DialogType.FORUM,
                DialogType.GROUP,
            }
        return False

    @staticmethod
    def _not_applicable_section_patch(section: str) -> Mapping[str, object]:
        if section == "common_chats":
            return {"common_chats": []}
        if section == "contact_overlap":
            return {
                "contacts_subscribed": None,
                "contacts_subscribed_partial": False,
                "contacts_reason": "not_applicable",
            }
        if section == "avatar_history":
            return {"avatar_history": [], "avatar_count": 0}
        if section == "personal_channel":
            return {
                "personal_channel_id": None,
                "personal_channel": None,
                "personal_channel_unavailable_reason": None,
            }
        return {}

    async def _acquire_full_profile(
        self,
        entity_id: int,
        entity_type: DialogType,
        *,
        context: _ProfileSectionContext | None = None,
    ) -> EntitySectionCommit:
        if entity_type in {DialogType.USER, DialogType.BOT, DialogType.SERVICE}:
            return await self._acquire_user_full_profile(
                entity_id, entity_type, scope=context.captured_scope if context else None
            )

        if entity_type in {DialogType.CHANNEL, DialogType.SUPERGROUP, DialogType.FORUM}:
            return await self._acquire_channel_full_profile(entity_id, entity_type, context=context)

        return EntitySectionCommit({}, status="unavailable", reason="unsupported_entity_type")

    async def _acquire_user_full_profile(
        self, entity_id: int, entity_type: DialogType, *, scope: TelegramAuthScope | None = None
    ) -> EntitySectionCommit:
        target_kind = self._target_kind(entity_type)
        observation = await self._deps.user_profile_port.fetch_user_profile(
            entity_id, target_kind, owner_account_id=scope.account_id if scope else None
        )
        outcome = observation.full_profile
        if outcome.status is ProjectionStatus.UNAVAILABLE or outcome.payload is None:
            raise ValueError(outcome.reason or "full user payload is unavailable")
        patch = dict(outcome.payload)
        folder_id = patch.get("folder_id")
        patch["folder_name"] = await self._resolve_folder_name(folder_id if isinstance(folder_id, int) else None)
        current_photo = observation.current_photo
        private_payload = _profile_photo_patch(current_photo, observation.semantic_snapshot)
        patch.update(private_payload)
        raw_identity_patch = dict(outcome.identity_patch or {})
        identity_patch = {**raw_identity_patch, "type": entity_type.value}
        return EntitySectionCommit(
            patch,
            payload=private_payload,
            identity_patch=identity_patch,
            dialog_identity_observation=outcome.dialog_identity_observation,
            semantic_snapshot=observation.semantic_snapshot,
        )

    async def _acquire_channel_full_profile(
        self,
        entity_id: int,
        entity_type: DialogType,
        *,
        context: _ProfileSectionContext | None = None,
    ) -> EntitySectionCommit:
        port = self._deps.channel_profile_port
        reference = self._deps.channel_reference_provider.get_channel_reference(entity_id)
        if reference is None:
            return EntitySectionCommit({}, status="unavailable", reason="channel_reference_unavailable")
        is_broadcast_channel = entity_type is DialogType.CHANNEL
        await self._capture_linked_chat_fact_generation(entity_id, is_broadcast_channel, context)
        observation = await port.fetch_channel_profile(reference)
        unavailable = _channel_profile_unavailable_commit(reference, observation)
        if unavailable is not None:
            return unavailable
        self._record_linked_chat_profile_observation(is_broadcast_channel, context, observation.linked_chat_id)
        patch: dict[str, object] = {
            "about": observation.about,
            "pinned_msg_id": observation.pinned_msg_id,
            "slow_mode_seconds": observation.slow_mode_seconds,
        }
        patch.update(_channel_profile_kind_patch(entity_type, observation))
        patch.update(observation.chat_entity_fields or {})
        return EntitySectionCommit(
            patch,
            payload=_channel_profile_payload(observation),
            dialog_created=observation.created,
            identity_patch=observation.identity_patch,
            dialog_identity_observation=observation.dialog_identity_observation,
        )

    async def _capture_linked_chat_fact_generation(
        self,
        entity_id: int,
        is_broadcast_channel: bool,
        context: _ProfileSectionContext | None,
    ) -> None:
        if is_broadcast_channel and context is not None:

            def write(conn: sqlite3.Connection) -> int:
                with write_transaction(conn):
                    return linked_chat_fact_owner.capture_generation(conn, entity_id)

            context.linked_chat_fact_generation = await self._deps.writer.run(write)
            context.linked_chat_fact_captured = True

    @staticmethod
    def _record_linked_chat_profile_observation(
        is_broadcast_channel: bool,
        context: _ProfileSectionContext | None,
        linked_chat_id: int | None,
    ) -> None:
        if is_broadcast_channel and context is not None:
            context.linked_chat_id = linked_chat_id
            context.linked_chat_observation_usable = True

    async def _acquire_group_full_chat_pair(
        self,
        cursor: EntityRefreshCursor,
    ) -> tuple[EntitySectionCommit, EntitySectionCommit]:
        """Build full profile and contact overlap from one validated RPC result."""
        captured_scope = self._capture_pair_scope()
        identity_baseline = capture_identity_baseline(self._deps.conn, cursor.entity_id)
        if self._deps.full_user_auth_scope is not None and captured_scope is None:
            raise _AuthScopeUnavailableError("authenticated session scope is unavailable")
        observation = await self._deps.group_profile_port.fetch_group_profile(cursor.entity_id)
        if observation.group_id != cursor.entity_id:
            raise ValueError("group profile observation target does not match")
        completed_at = observation.observation_completed_at
        started_at = observation.observation_started_at
        participants = observation.participant_ids
        participants_reason = observation.participants_unavailable_reason
        identity = self._group_full_chat_identity(captured_scope)
        full_profile = self._group_full_profile_commit(
            observation,
            participants,
            evidence=self._group_full_chat_evidence(
                cursor,
                outcome="usable",
                started_at=started_at,
                completed_at=completed_at,
                identity=identity,
                authoritative=True,
                declared_fields=("about", "invite_link", "members_count", *(observation.chat_entity_fields or {})),
            ),
        )
        identity_observation = observation.dialog_identity_observation
        full_profile = dataclass_replace(
            full_profile,
            identity_patch=observation.identity_patch,
            dialog_identity_observation=identity_observation,
            dialog_identity_baseline_revision=identity_baseline,
        )
        if participants is None:
            contact_overlap = EntitySectionCommit(
                {
                    "contacts_subscribed": None,
                    "contacts_subscribed_partial": False,
                    "contacts_reason": participants_reason,
                },
                status="unavailable",
                reason=participants_reason,
                evidence=self._group_full_chat_evidence(
                    cursor,
                    outcome="unavailable",
                    started_at=started_at,
                    completed_at=completed_at,
                    identity=identity,
                    authoritative=False,
                    declared_fields=("contacts_subscribed", "contacts_subscribed_partial", "contacts_reason"),
                ),
            )
        else:
            try:
                contact_overlap = self._group_contact_overlap_commit(
                    observation,
                    evidence=self._group_full_chat_evidence(
                        cursor,
                        outcome="usable",
                        started_at=started_at,
                        completed_at=completed_at,
                        identity=identity,
                        authoritative=True,
                        declared_fields=("contacts_subscribed", "contacts_subscribed_partial", "contacts_reason"),
                    ),
                )
            except sqlite3.Error:
                # The valid full profile remains useful; retry only the failed
                # local overlap projection through its ordinary durable cursor.
                contact_overlap = EntitySectionCommit({}, status="pending", reason="enumeration_failed")
        return full_profile, contact_overlap

    def _group_full_profile_commit(
        self,
        observation: GroupProfileObservation,
        participants: tuple[int, ...] | None,
        *,
        evidence: ProfileAcquisitionEvidence | None = None,
    ) -> EntitySectionCommit:
        return EntitySectionCommit(
            {
                "about": observation.about,
                "invite_link": observation.invite_link,
                "members_count": len(participants) if participants is not None else observation.participants_count,
                **(observation.chat_entity_fields or {}),
            },
            dialog_created=observation.created,
            payload={
                "current_photo": (
                    {"photo_id": observation.current_photo.photo_id, "date": observation.current_photo.date}
                    if observation.current_photo is not None
                    else None
                )
            },
            evidence=evidence,
        )

    @staticmethod
    def _group_full_chat_identity(scope: TelegramAuthScope | None) -> Mapping[str, object] | None:
        if scope is None:
            return None
        return {
            "auth_scope": scope.as_private_mapping(),
            "entity_type": DialogType.GROUP.value,
            "endpoint": GROUP_PROFILE_ENDPOINT,
            "normalization_version": GROUP_PROFILE_NORMALIZATION_VERSION,
        }

    @staticmethod
    def _group_full_chat_evidence(  # noqa: PLR0913
        cursor: EntityRefreshCursor,
        *,
        outcome: str,
        started_at: int,
        completed_at: int,
        identity: Mapping[str, object] | None,
        authoritative: bool,
        declared_fields: Sequence[str],
    ) -> ProfileAcquisitionEvidence:
        return ProfileAcquisitionEvidence(
            generation=cursor.generation,
            outcome=outcome,
            provenance={
                "endpoint": GROUP_PROFILE_ENDPOINT,
                "declared_fields": list(declared_fields),
                "materialized_fields": list(declared_fields),
                "authoritative": authoritative,
            },
            normalization_version=GROUP_PROFILE_NORMALIZATION_VERSION,
            observation_started_at=started_at,
            observation_completed_at=completed_at,
            identity=identity,
        )

    async def _acquire_common_chats(self, entity_id: int) -> EntitySectionCommit:
        reference = self._user_reference(entity_id)
        if reference is None:
            return EntitySectionCommit({}, status="unavailable", reason="user_reference_unavailable")
        observation = await self._deps.common_chats_port.fetch_common_chats(reference)
        if observation.user_id != entity_id:
            raise ValueError("common chats observation target does not match")
        chats = [{"id": item.chat_id, "name": item.name, "type": item.kind} for item in observation.chats]
        if observation.status is ProjectionStatus.UNAVAILABLE:
            return EntitySectionCommit({}, status="unavailable", reason=observation.reason, payload=None)
        return EntitySectionCommit({"common_chats": chats}, payload=chats)

    async def _acquire_contact_overlap(
        self,
        cursor: EntityRefreshCursor,
        entity_type: DialogType,
    ) -> EntitySectionCommit:
        if entity_type is DialogType.GROUP:
            return await self._acquire_group_contact_overlap(cursor.entity_id)

        return await self._acquire_channel_contact_overlap(cursor.entity_id, entity_type, generation=cursor.generation)

    async def _acquire_group_contact_overlap(self, entity_id: int) -> EntitySectionCommit:
        observation = await self._deps.group_profile_port.fetch_group_profile(entity_id)
        if observation.group_id != entity_id:
            raise ValueError("group profile observation target does not match")
        return self._group_contact_overlap_commit(observation)

    def _group_contact_overlap_commit(
        self,
        observation: GroupProfileObservation,
        *,
        evidence: ProfileAcquisitionEvidence | None = None,
    ) -> EntitySectionCommit:
        if observation.participant_ids is None:
            return EntitySectionCommit(
                {
                    "contacts_subscribed": None,
                    "contacts_subscribed_partial": False,
                    "contacts_reason": observation.participants_unavailable_reason,
                },
                status="unavailable",
                reason=observation.participants_unavailable_reason,
                evidence=evidence,
            )
        contacts = self._enrich_contact_ids_with_names(set(observation.participant_ids) & self._deps.dm_peer_ids())
        return EntitySectionCommit(
            {
                "contacts_subscribed": contacts,
                "contacts_subscribed_partial": False,
                "contacts_reason": None,
            },
            payload=contacts,
            evidence=evidence,
        )

    async def _acquire_channel_contact_overlap(
        self,
        entity_id: int,
        entity_type: DialogType,
        *,
        generation: int,
    ) -> EntitySectionCommit:
        port = self._deps.channel_profile_port
        reference = self._deps.channel_reference_provider.get_channel_reference(entity_id)
        if reference is None:
            return EntitySectionCommit(
                {
                    "contacts_subscribed": None,
                    "contacts_subscribed_partial": False,
                    "contacts_reason": "channel_reference_unavailable",
                },
                status="unavailable",
                reason="channel_reference_unavailable",
            )
        observation = await port.fetch_channel_contact_overlap(reference)
        if observation.channel_id != reference.channel_id:
            raise ValueError("channel contact overlap observation target does not match")
        if (
            observation.status is ProjectionStatus.UNAVAILABLE
            and observation.reason == CHANNEL_CONTACT_UNSUPPORTED_REASON
        ):
            return self._unsupported_contact_overlap_commit(observation, generation=generation)
        reason = observation.reason
        if reason == "not_an_admin" and entity_type in {DialogType.SUPERGROUP, DialogType.FORUM}:
            reason = "hidden_by_admin"
        if observation.status is ProjectionStatus.UNAVAILABLE or observation.contact_ids is None:
            return EntitySectionCommit(
                {
                    "contacts_subscribed": None,
                    "contacts_subscribed_partial": False,
                    "contacts_reason": reason,
                },
                status="unavailable",
                reason=reason,
            )
        contacts = self._enrich_contact_ids_with_names(set(observation.contact_ids) & self._deps.dm_peer_ids())
        return EntitySectionCommit(
            {
                "contacts_subscribed": contacts,
                "contacts_subscribed_partial": True,
                "contacts_reason": reason,
            },
            payload=contacts,
        )

    @staticmethod
    def _unsupported_contact_overlap_commit(
        observation: ChannelContactOverlapObservation, *, generation: int
    ) -> EntitySectionCommit:
        if observation.observation_started_at is None or observation.observation_completed_at is None:
            raise ValueError("unsupported contact outcome requires its actual observation bounds")
        return EntitySectionCommit(
            {},
            status="unavailable",
            reason=CHANNEL_CONTACT_UNSUPPORTED_REASON,
            evidence=ProfileAcquisitionEvidence(
                generation=generation,
                outcome="unavailable",
                provenance={
                    "endpoint": observation.endpoint,
                    "rpc_code": CHANNEL_CONTACT_UNSUPPORTED_RPC_CODE,
                    "rpc_symbol": observation.reason,
                },
                normalization_version=observation.normalization_version,
                observation_started_at=int(observation.observation_started_at),
                observation_completed_at=int(observation.observation_completed_at),
            ),
        )

    async def _acquire_avatar_history(
        self,
        entity_id: int,
        entity_type: DialogType,
    ) -> EntitySectionCommit:
        if entity_type in {DialogType.USER, DialogType.BOT, DialogType.SERVICE}:
            return await self._acquire_user_avatar_history(entity_id)

        return await self._acquire_chat_avatar_history(entity_id)

    async def _acquire_user_avatar_history(self, entity_id: int) -> EntitySectionCommit:
        reference = self._user_reference(entity_id)
        if reference is None:
            return EntitySectionCommit({}, status="unavailable", reason="user_reference_unavailable")
        observation = await self._deps.user_avatar_history_port.fetch_user_avatar_history(reference)
        if observation.user_id != entity_id:
            raise ValueError("user avatar observation target does not match")
        if observation.status is ProjectionStatus.UNAVAILABLE:
            return EntitySectionCommit({}, status="unavailable", reason=observation.reason, payload=None)
        current = self._current_profile_photo(entity_id)
        history, count = _reconcile_history_payload(observation.photos, current, observation.reported_count)
        return EntitySectionCommit({"avatar_history": history, "avatar_count": count}, payload=history)

    async def _acquire_chat_avatar_history(self, entity_id: int) -> EntitySectionCommit:
        reference = self._chat_avatar_reference(entity_id)
        if reference is None:
            return EntitySectionCommit({}, status="unavailable", reason="chat_reference_unavailable", payload=None)
        observation = await self._deps.chat_avatar_history_port.fetch_chat_avatar_history(reference)
        if observation.reference != reference:
            raise ValueError("chat avatar observation reference does not match")
        if observation.status is ProjectionStatus.UNAVAILABLE:
            return EntitySectionCommit({}, status="unavailable", reason=observation.reason, payload=None)
        current = self._current_profile_photo(entity_id)
        merged, count = _reconcile_history_payload(observation.photos, current, observation.reported_count)
        return EntitySectionCommit({"avatar_history": merged, "avatar_count": count}, payload=merged)

    def _current_profile_photo(self, entity_id: int) -> ChatCurrentPhoto | None:
        stored = self._profiles.read(entity_id, now=int(self._deps.now_provider()))
        if stored is not None and "current_photo" in stored.detail:
            return _current_photo_from_payload(stored.detail)
        return _current_photo_from_payload(self._profiles.read_section_payload(entity_id, "full_profile"))

    def _user_reference(self, entity_id: int) -> UserReference | None:
        is_self = self._deps.self_id == entity_id
        reference = self._deps.user_profile_port.get_user_reference(entity_id, is_self=is_self)
        return reference if isinstance(reference, UserReference) else None

    def _collection_user_reference(self, entity_id: int, section: str) -> UserReference:
        reference = self._user_reference(entity_id)
        if reference is None:
            if section in {"current_stories", "pinned_stories"}:
                raise StoryProfileValidationError("reference_unavailable")
            raise ValueError("collection user reference unavailable")
        return reference

    async def _acquire_personal_channel(self, entity_id: int, entity_type: DialogType) -> EntitySectionCommit:
        target_kind = self._target_kind(entity_type)
        observation = await self._deps.user_profile_port.fetch_user_profile(entity_id, target_kind)
        return self._personal_channel_section_commit(observation.personal_channel)

    def _personal_channel_section_commit(self, outcome: ProjectionOutcome) -> EntitySectionCommit:
        payload = dict(outcome.payload or {})
        if outcome.status is ProjectionStatus.ABSENT:
            return EntitySectionCommit(
                {"personal_channel_id": None, "personal_channel": None, "personal_channel_unavailable_reason": None},
                payload=None,
            )
        channel_id = _positive_personal_channel_id(payload)
        if channel_id is None:
            return EntitySectionCommit(
                {}, status="unavailable", reason=outcome.reason or "personal_channel_unavailable"
            )
        if outcome.status is ProjectionStatus.PARTIAL:
            reason = outcome.reason or "personal_channel_unavailable"
            return EntitySectionCommit(
                {
                    "personal_channel_id": channel_id,
                    "personal_channel_message": payload.get("personal_channel_message"),
                    "personal_channel": None,
                    "personal_channel_unavailable_reason": reason,
                },
                status="unavailable",
                reason=reason,
                payload=payload,
            )
        dialog_id = self._normalize_channel_dialog_id(channel_id)
        metadata, metadata_source = self._personal_channel_metadata(dialog_id=dialog_id, normalized=payload)
        if metadata is None:
            return EntitySectionCommit(
                {
                    "personal_channel_id": channel_id,
                    "personal_channel": None,
                    "personal_channel_unavailable_reason": outcome.reason or "channel_metadata_unavailable",
                },
                status="unavailable",
                reason=outcome.reason or "channel_metadata_unavailable",
                payload=payload,
            )
        local_preview, local_reason = self._latest_local_personal_channel_post(dialog_id)
        return self._personal_channel_section_card_commit(
            _PersonalChannelCard(
                outcome=outcome,
                channel_id=channel_id,
                dialog_id=dialog_id,
                metadata=metadata,
                metadata_source=metadata_source,
                local_preview=local_preview,
                local_reason=local_reason,
            ),
            attached_message_id=payload.get("personal_channel_message"),
        )

    def _personal_channel_section_card_commit(
        self,
        card_context: _PersonalChannelCard,
        *,
        attached_message_id: object,
    ) -> EntitySectionCommit:
        card = _compact_dict(
            {
                "channel_id": card_context.channel_id,
                "dialog_id": card_context.dialog_id,
                "title": card_context.metadata.get("title"),
                "username": card_context.metadata.get("username"),
                "url": self._tme_url(cast(str | None, card_context.metadata.get("username"))),
                "metadata_source": card_context.metadata_source,
                "attached_message_id": attached_message_id,
                "latest_or_attached_post": card_context.local_preview,
                "post_preview_unavailable_reason": (
                    card_context.local_reason if card_context.local_preview is None else None
                ),
            }
        )
        usable = card_context.outcome.status is ProjectionStatus.USABLE
        return EntitySectionCommit(
            {
                "personal_channel_id": card_context.channel_id,
                "personal_channel_message": attached_message_id,
                "personal_channel": card,
                "personal_channel_unavailable_reason": None if usable else card_context.outcome.reason,
            },
            status="fresh" if usable else "unavailable",
            reason=None if usable else card_context.outcome.reason,
            payload=card,
        )

    def _core_from_entity(self, entity: object) -> dict[str, object]:
        entity_id = int(self._deps.get_peer_id(entity))
        dispatch_kind = classify_dialog_type(entity)
        name = _opt_str_attr(entity, "first_name") or _opt_str_attr(entity, "title")
        last_name = _opt_str_attr(entity, "last_name")
        if last_name:
            name = " ".join(part for part in (name, last_name) if part)
        return {
            "id": entity_id,
            "type": dispatch_kind.value,
            "name": name,
            "username": _opt_str_attr(entity, "username"),
            "dialog_placement": (
                self._deps.get_dialog_placement(entity_id)
                if self._deps.get_dialog_placement
                else dialog_placement(self._deps.conn, entity_id)
            ),
        }

    async def _progressive_result(
        self,
        entity_id: int,
        detail: Mapping[str, object],
        sections: dict[str, dict[str, object]],
        *,
        now: int,
    ) -> dict[str, object]:
        result = dict(detail)
        result["id"] = entity_id
        photo_payload = result if "current_photo" in result else sections.get("full_profile", {}).get("data")
        current_photo = _current_photo_from_payload(photo_payload)
        _project_current_photo(result, sections, current_photo)
        if result.get("type") == DialogType.CHANNEL.value:
            result["linked_chat_id"] = self._read_canonical_linked_chat_id(entity_id)
        result["dialog_placement"] = result.get(
            "dialog_placement",
            self._deps.get_dialog_placement(entity_id)
            if self._deps.get_dialog_placement
            else dialog_placement(self._deps.conn, entity_id),
        )
        result["completeness"] = completeness(sections)
        result["sections"] = self._section_summaries(sections)
        self._deps.logger.info(
            "entity_profile.projection outcome=%s entity_id=%r sections=%s%s",
            result["completeness"],
            entity_id,
            self._section_counts(sections),
            self._deps.rid(),
        )
        return {"ok": True, "data": result}

    @staticmethod
    def _section_summaries(sections: Mapping[str, Mapping[str, object]]) -> dict[str, dict[str, object]]:
        return {
            name: {
                key: value[key]
                for key in ("status", "observed_at", "reason")
                if key == "status" or isinstance(value.get(key), int | str)
            }
            for name, value in sections.items()
        }

    @staticmethod
    def _section_counts(sections: Mapping[str, Mapping[str, object]]) -> dict[str, int]:
        statuses = {"fresh", "stale", "pending", "unavailable", "not_applicable"}
        return {status: sum(value.get("status") == status for value in sections.values()) for status in statuses}

    async def shutdown(self) -> None:
        if self._refresh is not None:
            await self._refresh.shutdown()

    def _extract_entity_id(self, req: Mapping[str, object]) -> int | dict[str, object]:
        entity_id = req.get("entity_id")
        if type(entity_id) is not int:
            return self._error(
                "invalid_entity_id", "entity_id must be an integer. Action: use an exact entity id from ListDialogs."
            )
        if not _is_valid_telethon_peer_id(entity_id):
            return self._error(
                "invalid_entity_id",
                "Entity id is outside the valid Telegram peer-id range. Action: retry with an exact entity id from ListDialogs.",
            )
        return entity_id

    async def _resolve_entity(self, entity_id: int) -> tuple[object | None, dict[str, object] | None]:
        try:
            async with asyncio.timeout(self._deps.refresh_limits.per_rpc_seconds):
                entity = await self._deps.client.get_entity(entity_id)
        except (ValueError, KeyError, *_ENTITY_NOT_FOUND_ERRORS) as exc:
            self._deps.logger.warning(
                "entity_info entity_not_found entity_id=%r error=%s%s",
                entity_id,
                exc,
                self._deps.rid(),
            )
            return None, self._error(
                "entity_not_found",
                f"{exc}. Action: retry with an exact entity id from ListDialogs.",
            )
        except TelegramRpcThrottled, RpcAttemptBudgetExhaustedError, UnclassifiedTelegramDemandError:
            raise
        except (TimeoutError, RPCError, RuntimeError, TypeError, AttributeError) as exc:
            raise_if_flood_wait_error(exc)
            if isinstance(exc, ACCESS_LOST_ERRORS):
                return None, self._error("telegram_api_error", str(exc))
            self._deps.logger.warning(
                "entity_info get_entity_failed entity_id=%r error=%s%s",
                entity_id,
                exc,
                self._deps.rid(),
                exc_info=True,
            )
            error = self._error("telegram_api_error", str(exc))
            retry_after = getattr(exc, "retry_after_seconds", None)
            if not isinstance(retry_after, int):
                retry_after = getattr(exc, "seconds", None)
            if isinstance(retry_after, int) and retry_after > 0:
                error["_retry_after_seconds"] = retry_after
            return None, error
        try:
            canonical_id = int(self._deps.get_peer_id(entity))
        except (TypeError, ValueError, AttributeError) as exc:
            return None, self._error(
                "entity_not_found",
                f"resolved entity has no valid canonical id: {exc}. Action: retry with an exact entity id from ListDialogs.",
            )
        if canonical_id != entity_id:
            return None, self._error(
                "entity_not_found",
                f"requested entity id {entity_id} resolved to {canonical_id}. Action: retry with the canonical id from ListDialogs.",
            )
        return entity, None

    @staticmethod
    def _error(message_key: str, message: str) -> dict[str, object]:
        return {
            "ok": False,
            "error": message_key,
            "message": message,
            "data": None,
        }

    @staticmethod
    def _normalized_personal_channel_metadata(normalized: Mapping[str, object] | None) -> dict[str, object]:
        if normalized is None:
            return {}
        return _compact_dict({"title": normalized.get("title"), "username": normalized.get("username")})

    def _local_personal_channel_metadata(self, dialog_id: int) -> dict[str, object]:
        row = cast(
            tuple[object | None, object | None] | None,
            self._deps.conn.execute("SELECT name, username FROM entities WHERE id = ?", (dialog_id,)).fetchone(),
        )
        if row is None:
            return {}
        return _compact_dict(
            {
                "title": row[0] if isinstance(row[0], str) else None,
                "username": row[1] if isinstance(row[1], str) else None,
            }
        )

    @staticmethod
    def _merge_personal_channel_metadata(
        normalized: Mapping[str, object], local: Mapping[str, object]
    ) -> dict[str, object]:
        metadata: dict[str, object] = {}
        for key in ("title", "username"):
            value = normalized.get(key)
            if value is None:
                value = local.get(key)
            if value is not None:
                metadata[key] = value
        return metadata

    def _personal_channel_metadata(
        self,
        *,
        dialog_id: int,
        normalized: Mapping[str, object] | None = None,
    ) -> tuple[dict[str, object] | None, str]:
        normalized_metadata = self._normalized_personal_channel_metadata(normalized)
        local_metadata = self._local_personal_channel_metadata(dialog_id)
        metadata = self._merge_personal_channel_metadata(normalized_metadata, local_metadata)
        if not metadata:
            return None, "local_entities"
        return metadata, "user_full_chats" if normalized_metadata else "local_entities"

    def _latest_local_personal_channel_post(self, dialog_id: int) -> tuple[dict[str, object] | None, str]:
        try:
            row = cast(
                tuple[object, object, object] | None,
                self._deps.conn.execute(
                    """
                    SELECT message_id, sent_at, text
                    FROM messages
                    WHERE dialog_id = ?
                      AND is_deleted = 0
                      AND is_service = 0
                      AND text IS NOT NULL
                      AND TRIM(text) != ''
                    ORDER BY sent_at DESC, message_id DESC
                    LIMIT 1
                    """,
                    (dialog_id,),
                ).fetchone(),
            )
        except sqlite3.OperationalError:
            return None, "local_messages_unavailable"

        if row is None:
            return None, "no_synced_text_posts"
        preview = self._post_preview_from_text(
            source="local_latest_message",
            message_id=int(cast(int | str, row[0])),
            sent_at=int(cast(int | str, row[1])),
            text=row[2] if isinstance(row[2], str) else None,
        )
        return (preview, "") if preview is not None else (None, "local_latest_message_has_no_text")

    @staticmethod
    def _post_preview_from_text(
        *,
        source: str,
        message_id: int,
        sent_at: int | None,
        text: str | None,
    ) -> dict[str, object] | None:
        if text is None:
            return None
        stripped = text.strip()
        if not stripped:
            return None
        preview = stripped[:_PERSONAL_CHANNEL_PREVIEW_CHARS]
        return _compact_dict(
            {
                "source": source,
                "message_id": message_id,
                "sent_at": sent_at,
                "text_preview": preview,
                "char_count": len(stripped),
                "is_truncated": len(stripped) > _PERSONAL_CHANNEL_PREVIEW_CHARS,
            }
        )

    @staticmethod
    def _normalize_channel_dialog_id(raw_channel_id: int) -> int:
        if raw_channel_id > 0:
            return -_CHANNEL_DIALOG_ID_OFFSET - raw_channel_id
        return raw_channel_id

    @staticmethod
    def _tme_url(username: str | None) -> str | None:
        if not username:
            return None
        return f"https://t.me/{username}"

    async def _resolve_folder_name(self, folder_id: int | None) -> str | None:
        if folder_id is None:
            return None
        try:
            row = cast(
                tuple[object, ...] | None,
                self._deps.conn.execute(
                    "SELECT title FROM telegram_folder_rules "
                    "WHERE (namespace='filter' AND folder_id=?) "
                    "OR (namespace='default' AND ?=0) "
                    "ORDER BY CASE namespace WHEN 'filter' THEN 0 ELSE 1 END LIMIT 1",
                    (folder_id, folder_id),
                ).fetchone(),
            )
            if row is not None:
                return str(row[0])
        except (sqlite3.OperationalError, TypeError, ValueError) as exc:
            self._deps.logger.warning(
                "entity_info user folder_resolve_failed folder_id=%r error=%s%s",
                folder_id,
                exc,
                self._deps.rid(),
            )
        return None

    def _read_canonical_linked_chat_id(self, channel_id: int) -> int | None:
        try:
            row = cast(
                tuple[object, object] | None,
                self._deps.conn.execute(
                    "SELECT linked_chat_id, linked_chat_resolved_at FROM dialogs WHERE dialog_id=?",
                    (channel_id,),
                ).fetchone(),
            )
        except sqlite3.Error:
            return None
        if row is None or row[1] is None:
            return None
        return row[0] if isinstance(row[0], int) else None

    def _enrich_contact_ids_with_names(self, ids: set[int]) -> list[dict[str, object]]:
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        rows = cast(
            Sequence[tuple[object, object, object]],
            self._deps.conn.execute(
                f"SELECT id, name, username FROM entities WHERE id IN ({placeholders})",
                tuple(ids),
            ).fetchall(),
        )
        seen = {row[0] for row in rows}
        out = [{"id": row[0], "name": row[1], "username": row[2]} for row in rows]
        out.extend({"id": missing_id, "name": None, "username": None} for missing_id in ids - seen)
        return sorted(out, key=lambda d: ((d["name"] or ""), d["id"]))

    def _chat_avatar_reference(self, entity_id: int) -> ChatAvatarReference | None:
        if entity_id <= -CHANNEL_ID_MARKER - 1:
            channel_reference = self._deps.channel_reference_provider.get_channel_reference(entity_id)
            if isinstance(channel_reference, ChannelReference):
                return channel_reference
        reference = self._deps.chat_avatar_history_port.get_chat_avatar_reference(entity_id)
        return reference if isinstance(reference, (ChannelReference, GroupReference)) else None
