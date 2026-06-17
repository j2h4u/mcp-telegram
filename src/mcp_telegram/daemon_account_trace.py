"""Pure Account Trace helpers extracted from the daemon API server."""

import asyncio
import dataclasses
import json
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast

from telethon.errors import FloodWaitError, RPCError  # type: ignore[import-untyped]
from telethon.tl.functions.contacts import ResolveUsernameRequest  # type: ignore[import-untyped]

from .activity_peer_resolve import resolve_linked_chat_id
from .activity_peer_sweep import enroll_activity_dialog
from .dialog_sync import _ACCESS_LOST_ERRORS
from .models import DialogType
from .resolver import Candidates, Resolved, _parse_tme_link, latinize, resolve
from .sync_worker import ExtractedMessage, extract_message_row, insert_messages_with_fts

_TRACE_SCOPE_DIALOG_IDS_LEN = 2
USER_TTL = 2_592_000  # 30 days
GROUP_TTL = 604_800  # 7 days
_TRACE_ENRICHMENT_MAX_DIALOGS = 10
_TRACE_ENRICHMENT_MAX_PER_DIALOG = 100
_TRACE_ENRICHMENT_DEADLINE_MS = 15_000
_TRACE_ENRICHMENT_CONCURRENCY = 2


def _clamp(value: int, low: int, high: int) -> int:
    """Clamp integer values for safe request parameter handling."""
    return max(low, min(value, high))


_UPSERT_ENTITY_SQL = (
    "INSERT OR REPLACE INTO entities (id, type, name, username, name_normalized, updated_at) VALUES (?, ?, ?, ?, ?, ?)"
)
_ENTITY_BY_USERNAME_SQL = "SELECT id, name, username, name_normalized FROM entities WHERE username = ? COLLATE NOCASE"
_TRACE_ACCOUNT_BY_ID_SQL = "SELECT id, name, username, name_normalized FROM entities WHERE id = ?"
_TRACE_ACCOUNT_NAMES_SQL = (
    "SELECT id, name FROM entities "
    "WHERE id > 0 AND name IS NOT NULL "
    "AND ((type IN ('User', 'Bot', 'user', 'bot') AND updated_at >= ?) "
    "OR (type NOT IN ('User', 'Bot', 'user', 'bot') AND updated_at >= ?))"
)
_TRACE_ACCOUNT_NAMES_NORMALIZED_SQL = (
    "SELECT id, name_normalized FROM entities "
    "WHERE id > 0 AND name_normalized IS NOT NULL "
    "AND ((type IN ('User', 'Bot', 'user', 'bot') AND updated_at >= ?) "
    "OR (type NOT IN ('User', 'Bot', 'user', 'bot') AND updated_at >= ?))"
)


@dataclass(frozen=True)
class DaemonAccountTraceDeps:
    """Dependency container for account-trace orchestration."""

    conn: sqlite3.Connection
    client: Any
    resolve_dialog_id: Callable[[int, str | None], Awaitable[int | dict]]
    self_id: int | None
    logger: Any
    rid: Callable[[], str]


@dataclass(frozen=True, slots=True)
class _TraceAccountLookup:
    mode: str
    query: object


@dataclass(frozen=True, slots=True)
class _TraceAccountMessagesRequest:
    group_by: str
    coverage_goal: str
    exact_dialog_id: int | None
    exact_topic_id: int | None
    limit: int
    sent_after: object | None
    sent_before: object | None
    sent_after_ts: int | None
    sent_before_ts: int | None
    coverage_bounds: dict[str, object]


@dataclass(frozen=True, slots=True)
class _TraceAccountMessagesScope:
    exact_dialog_id: int | None
    exact_topic_id: int | None
    scope_dialog_ids: list[int] | None
    linked_chat_map: dict[int, int]
    navigation_payload: dict[str, int] | None


@dataclass(frozen=True, slots=True)
class _TraceAccountQueryResult:
    selected_rows: list[sqlite3.Row]
    evidence: list[dict]
    next_navigation: str | None


@dataclass(frozen=True, slots=True)
class _TraceVisibleEnrichmentRequest:
    max_per_dialog: int = _TRACE_ENRICHMENT_MAX_PER_DIALOG
    max_dialogs: int = _TRACE_ENRICHMENT_MAX_DIALOGS
    deadline_ms: int = _TRACE_ENRICHMENT_DEADLINE_MS
    concurrency: int = _TRACE_ENRICHMENT_CONCURRENCY


@dataclasses.dataclass(frozen=True, slots=True)
class _TraceAccountQueryContext:
    target_user_id: int
    request: _TraceAccountMessagesRequest
    scope: _TraceAccountMessagesScope
    post_author_aliases: list[str] | None
    conn: sqlite3.Connection
    self_id: int | None


@dataclasses.dataclass(frozen=True, slots=True)
class _TraceAccountPayloadContext:
    resolved_account: dict
    request: _TraceAccountMessagesRequest
    query_result: _TraceAccountQueryResult
    coverage: dict
    gaps: list[dict]
    enrichment: dict | None
    post_author_aliases: list[str]


@dataclasses.dataclass(frozen=True, slots=True)
class _TraceScopeResolveRequest:
    deps: DaemonAccountTraceDeps
    req: dict
    request: _TraceAccountMessagesRequest
    target_user_id: int


@dataclasses.dataclass(frozen=True, slots=True)
class _TraceEnrichPrecheckContext:
    conn: sqlite3.Connection
    target_user_id: int
    dialog_id: int
    topic_id: int | None
    strategy: str
    deadline_ms: int
    deadline_at: float
    now: int


@dataclasses.dataclass(frozen=True, slots=True)
class _TraceCandidateMessagesContext:
    client: Any
    conn: sqlite3.Connection
    dialog_id: int
    iter_kwargs: dict[str, object]
    target_user_id: int
    now: int
    deadline_at: float


@dataclasses.dataclass(frozen=True, slots=True)
class _TraceVisibleBudgetContext:
    target_user_id: int
    deadline_ms: int
    now: int
    result: dict


@dataclasses.dataclass(frozen=True, slots=True)
class _TraceVisibleCandidatesContext:
    service: Any
    target_user_id: int
    candidates: list[dict]
    max_per_dialog: int
    deadline_ms: int
    deadline_at: float
    concurrency: int


class DaemonAccountTraceService:
    """Account Trace orchestration for daemon-side enrichment and evidence extraction."""

    def __init__(self, deps: DaemonAccountTraceDeps) -> None:
        self._deps = deps

    async def _resolve_trace_account(self, req: dict) -> dict:
        """Resolve an Account Trace target without probing arbitrary numeric ids."""
        lookup = _extract_trace_account_lookup(req)
        if lookup.mode == "exact_id":
            assert isinstance(lookup.query, int)
            return _resolve_trace_account_by_id(
                self._deps.conn,
                lookup.query,
                resolution_source="entities_exact_id",
                unresolved_source="unresolved_numeric_id",
            )
        if lookup.mode == "numeric_account":
            assert isinstance(lookup.query, int)
            return _resolve_trace_account_by_id(
                self._deps.conn,
                lookup.query,
                resolution_source="entities_numeric_account",
                unresolved_source="unresolved_numeric_id",
            )
        if lookup.mode == "missing":
            return _unresolved_trace_account(query=lookup.query, resolution_source="missing_account")

        if lookup.mode == "username":
            username = str(lookup.query)
            row = _resolve_trace_account_by_username(self._deps.conn, username)
            if row is not None:
                return row
            return await self._resolve_trace_username(username)

        return _resolve_trace_account_by_fuzzy(self._deps.conn, str(lookup.query))

    async def _resolve_trace_username(self, username: str) -> dict:
        """Resolve an explicit username with one daemon-owned Telegram lookup."""
        try:
            result = await self._deps.client(ResolveUsernameRequest(username=username))
        except (RPCError, RuntimeError, TypeError, AttributeError, ValueError) as exc:
            self._deps.logger.info(
                "trace_account username_lookup_failed username=%r error_type=%s%s",
                username,
                type(exc).__name__,
                self._deps.rid(),
            )
            return _unresolved_trace_account(
                query=f"@{username}",
                resolution_source="telegram_username_lookup_failed",
            )

        users = list(getattr(result, "users", []) or [])
        if not users:
            return _unresolved_trace_account(
                query=f"@{username}",
                resolution_source="telegram_username_lookup_empty",
            )

        user = users[0]
        user_id = int(user.id)
        first_name = getattr(user, "first_name", None)
        last_name = getattr(user, "last_name", None)
        display_name = " ".join(part for part in (first_name, last_name) if part) or username
        resolved_username = getattr(user, "username", None) or username
        entity_type = DialogType.from_entity(user).value
        now = int(time.time())
        self._deps.conn.execute(
            _UPSERT_ENTITY_SQL,
            (
                user_id,
                entity_type,
                display_name,
                resolved_username,
                latinize(display_name),
                now,
            ),
        )
        self._deps.conn.commit()
        row = self._deps.conn.execute(_TRACE_ACCOUNT_BY_ID_SQL, (user_id,)).fetchone()
        if row is None:
            return _unresolved_trace_account(
                query=f"@{username}",
                resolution_source="telegram_username_lookup_write_failed",
            )
        return _trace_account_from_entity_row(row, resolution_source="telegram_username_lookup")

    @staticmethod
    def _build_trace_account_unresolved_payload(
        resolved_account: dict,
        request: _TraceAccountMessagesRequest,
    ) -> dict:
        gap = DaemonAccountTraceService._trace_account_resolution_gap(resolved_account)
        return {
            "ok": True,
            "data": DaemonAccountTraceService._empty_trace_result(
                resolved_account,
                gaps=[gap],
                coverage_goal=request.coverage_goal,
                coverage_bounds=request.coverage_bounds,
            ),
        }

    def _build_trace_account_query_result(
        self,
        request: _TraceAccountQueryContext,
    ) -> _TraceAccountQueryResult:
        from .pagination import AccountTraceNavigationRequest, encode_account_trace_navigation

        limit = request.request.limit
        scope = request.scope
        conn = request.conn
        sql, params = _build_trace_account_messages_query(
            _TraceMessageQueryRequest(
                target_user_id=request.target_user_id,
                self_id=request.self_id,
                limit=limit + 1,
                post_author_aliases=request.post_author_aliases,
                exact_dialog_id=scope.exact_dialog_id,
                exact_topic_id=scope.exact_topic_id,
                sent_after_ts=request.request.sent_after_ts,
                sent_before_ts=request.request.sent_before_ts,
                navigation=scope.navigation_payload,
                scope_dialog_ids=scope.scope_dialog_ids,
            )
        )
        rows = conn.execute(sql, params).fetchall()
        selected_rows = rows[:limit]
        evidence = [DaemonAccountTraceService._trace_row_to_evidence(row) for row in selected_rows]
        next_navigation: str | None = None
        if len(rows) > limit and selected_rows:
            last = selected_rows[-1]
            group_by: Literal["timeline", "dialog"] = "timeline"
            if request.request.group_by == "dialog":
                group_by = "dialog"
            next_navigation = encode_account_trace_navigation(
                AccountTraceNavigationRequest(
                    target_user_id=request.target_user_id,
                    sent_at=int(last["sent_at"]),
                    dialog_id=int(last["dialog_id"]),
                    message_id=int(last["message_id"]),
                    group_by=group_by,
                    exact_dialog_id=scope.exact_dialog_id,
                    exact_topic_id=scope.exact_topic_id,
                    sent_after=cast("str | None", request.request.sent_after),
                    sent_before=cast("str | None", request.request.sent_before),
                    scope_dialog_ids=scope.scope_dialog_ids,
                )
            )
        return _TraceAccountQueryResult(
            selected_rows=selected_rows,
            evidence=evidence,
            next_navigation=next_navigation,
        )

    def _build_trace_account_success_payload(
        self,
        request: _TraceAccountPayloadContext,
    ) -> dict:
        basis_counts: dict[str, int] = {}
        for item in request.query_result.evidence:
            basis = str(item["authorship_basis"])
            basis_counts[basis] = basis_counts.get(basis, 0) + 1

        data: dict[str, Any] = {
            "resolved_account": request.resolved_account,
            "groups": DaemonAccountTraceService._group_trace_evidence(
                request.query_result.evidence,
                request.request.group_by,
            ),
            "coverage": request.coverage,
            "gaps": request.gaps,
            "provenance": {
                "source": "sync_db",
                "query_basis": "effective_sender_id_or_post_author_signature",
                "coverage_goal": request.request.coverage_goal,
                "coverage_bounds": request.request.coverage_bounds,
                "authorship_basis_counts": basis_counts,
                "dialogs_considered_basis": request.coverage["dialogs_considered_basis"],
                "post_author_aliases_considered": request.post_author_aliases,
                "local_cache_writes": request.enrichment["messages_persisted"] if request.enrichment else 0,
            },
            "next_navigation": request.query_result.next_navigation,
        }
        if request.enrichment is not None:
            data["provenance"]["enrichment"] = request.enrichment
        return {"ok": True, "data": data}

    @staticmethod
    def _trace_row_to_evidence(row: sqlite3.Row) -> dict:
        """Convert one trace SQL row into a structured evidence item."""
        return {
            "source": "sync_db",
            "evidence_kind": "authored_message",
            "dialog_id": row["dialog_id"],
            "dialog_title": row["dialog_title"],
            "dialog_type": row["dialog_type"],
            "topic_id": row["topic_id"],
            "topic_title": row["topic_title"],
            "message_id": row["message_id"],
            "sent_at": row["sent_at"],
            "sender_id": row["sender_id"],
            "effective_sender_id": row["effective_sender_id"],
            "authorship_basis": row["authorship_basis"],
            "author_signature": row["author_signature"],
            "text": row["text"],
            "media_description": row["media_description"],
        }

    @staticmethod
    def _group_trace_evidence(evidence: list[dict], group_by: str) -> list[dict]:
        """Group the already-selected Account Trace page for presentation."""
        groups: dict[str, dict] = {}
        ordered_evidence = sorted(
            evidence,
            key=lambda item: (
                int(item.get("sent_at") or 0),
                int(item.get("dialog_id") or 0),
                int(item.get("message_id") or 0),
            ),
        )
        for item in ordered_evidence:
            if group_by == "dialog":
                topic_id = item.get("topic_id")
                topic_suffix = f":topic:{topic_id}" if topic_id is not None else ""
                key = f"dialog:{item['dialog_id']}{topic_suffix}"
                label = str(item.get("dialog_title") or item["dialog_id"])
                if item.get("topic_title"):
                    label = f"{label} / {item['topic_title']}"
            else:
                day = datetime.fromtimestamp(int(item["sent_at"]), tz=UTC).strftime("%Y-%m-%d")
                key = f"day:{day}"
                label = day
            if key not in groups:
                groups[key] = {"group_key": key, "group_label": label, "evidence": []}
            groups[key]["evidence"].append(item)
        return list(groups.values())

    @staticmethod
    def _trace_account_resolution_gap(resolved_account: dict) -> dict:
        """Build the baseline gap for unresolved or ambiguous trace targets."""
        if resolved_account.get("confidence") == "ambiguous":
            return {
                "kind": "account_ambiguous",
                "severity": "action_required",
                "detail": "Multiple visible accounts match this reference.",
                "next_action": {
                    "field": "exact_account_id",
                    "argument": "exact_account_id",
                    "candidate_ids": resolved_account.get("candidate_ids", []),
                },
            }
        return {
            "kind": "account_unresolved",
            "severity": "action_required",
            "detail": "No visible account matched this reference.",
        }

    @staticmethod
    def _empty_trace_result(
        resolved_account: dict,
        *,
        gaps: list[dict] | None = None,
        coverage_goal: str = "observed",
        coverage_bounds: dict | None = None,
    ) -> dict:
        """Return a structurally complete empty Account Trace payload."""
        as_of = int(time.time())
        return {
            "resolved_account": resolved_account,
            "groups": [],
            "coverage": {
                "state": "unknown",
                "observed_message_count": 0,
                "dialogs_considered": 0,
                "dialogs_considered_basis": "no_resolved_account",
                "dialogs_with_hits": 0,
                "dialogs_with_gaps": 0,
                "as_of": as_of,
            },
            "gaps": gaps or [],
            "provenance": {
                "source": "sync_db",
                "query_basis": "effective_sender_id_or_post_author_signature",
                "coverage_goal": coverage_goal,
                "coverage_bounds": coverage_bounds or {},
                "authorship_basis_counts": {},
                "dialogs_considered_basis": "no_resolved_account",
                "local_cache_writes": 0,
            },
            "next_navigation": None,
        }

    async def _trace_enrich_one_candidate(
        self,
        *,
        target_user_id: int,
        candidate: dict,
        max_per_dialog: int,
        deadline_ms: int,
        deadline_at: float,
    ) -> dict:
        """Fetch and persist one bounded Account Trace enrichment candidate."""
        dialog_id = int(candidate["dialog_id"])
        topic_id = candidate.get("topic_id")
        strategy = str(candidate.get("strategy", "unsupported"))
        result: dict[str, Any] = {
            "status": "complete",
            "attempted": 0,
            "skipped": 0,
            "messages_seen": 0,
            "messages_persisted": 0,
            "duplicates_skipped": 0,
        }

        now = int(time.time())
        status = _trace_enrich_candidate_precheck(
            _TraceEnrichPrecheckContext(
                conn=self._deps.conn,
                target_user_id=target_user_id,
                dialog_id=dialog_id,
                topic_id=topic_id,
                strategy=strategy,
                deadline_ms=deadline_ms,
                deadline_at=deadline_at,
                now=now,
            )
        )
        if status is not None:
            result["status"] = status
            result["skipped"] = 1
            return result

        iter_kwargs: dict[str, object] = {"limit": max_per_dialog}
        if topic_id is not None:
            iter_kwargs["reply_to"] = int(topic_id)
        if strategy == "author_search":
            iter_kwargs["from_user"] = target_user_id

        result["attempted"] = 1
        fetched, status = await _trace_enrich_candidate_messages(
            _TraceCandidateMessagesContext(
                client=self._deps.client,
                conn=self._deps.conn,
                dialog_id=dialog_id,
                iter_kwargs=iter_kwargs,
                target_user_id=target_user_id,
                now=now,
                deadline_at=deadline_at,
            )
        )
        if status is not None:
            result["status"] = status
            return result

        result["messages_seen"] = len(fetched)
        unique_messages, duplicate_count = _dedupe_trace_messages(fetched)
        result["duplicates_skipped"] += duplicate_count
        changed, persisted_duplicate_count = _split_trace_duplicate_messages(
            conn=self._deps.conn,
            messages=unique_messages,
        )
        result["duplicates_skipped"] += persisted_duplicate_count

        if changed:
            _persist_trace_messages(self._deps.conn, changed)
            result["messages_persisted"] = len(changed)

        status = _trace_candidate_status_after_fetch(
            fetched_count=len(fetched),
            max_per_dialog=max_per_dialog,
            deadline_ms=deadline_ms,
            deadline_at=deadline_at,
        )
        _upsert_trace_coverage_fragment(
            _TraceCoverageFragmentUpsertRequest(
                conn=self._deps.conn,
                target_user_id=target_user_id,
                dialog_id=dialog_id,
                topic_id=topic_id,
                status=status,
                fetched_at=now,
                last_error=f"BudgetExceeded:{deadline_ms}" if status == "budget_exceeded" else None,
                now=now,
            )
        )
        result["status"] = status
        return result

    async def _trace_enrich_visible_dialogs(
        self,
        target_user_id: int,
        candidate_dialogs: list[dict],
        *,
        request: _TraceVisibleEnrichmentRequest | None = None,
        **legacy_kwargs: int,
    ) -> dict:
        """Bounded best-effort visible Account Trace enrichment."""
        request = _resolve_trace_visible_enrichment_request(request, legacy_kwargs)
        result = _trace_enrichment_result(
            deadline_ms=request.deadline_ms,
            concurrency=request.concurrency,
            max_dialogs=request.max_dialogs,
            max_per_dialog=request.max_per_dialog,
        )
        now = int(time.time())
        selected = candidate_dialogs[: request.max_dialogs]
        overflow = candidate_dialogs[request.max_dialogs :]
        _mark_budget_exceeded_candidates(
            self._deps.conn,
            candidates=overflow,
            request=_TraceVisibleBudgetContext(
                target_user_id=target_user_id,
                deadline_ms=request.deadline_ms,
                now=now,
                result=result,
            ),
        )

        if not selected:
            self._deps.conn.commit()
            return result

        if request.deadline_ms <= 0:
            _mark_budget_exceeded_candidates(
                self._deps.conn,
                candidates=selected,
                request=_TraceVisibleBudgetContext(
                    target_user_id=target_user_id,
                    deadline_ms=request.deadline_ms,
                    now=now,
                    result=result,
                ),
            )
            self._deps.conn.commit()
            return result

        deadline_at = time.monotonic() + (request.deadline_ms / 1000)
        for item in await _run_trace_visible_candidates(
            _TraceVisibleCandidatesContext(
                service=self,
                target_user_id=target_user_id,
                candidates=selected,
                max_per_dialog=request.max_per_dialog,
                deadline_ms=request.deadline_ms,
                deadline_at=deadline_at,
                concurrency=request.concurrency,
            )
        ):
            result["dialogs_attempted"] += int(item["attempted"])
            result["dialogs_skipped"] += int(item["skipped"])
            result["messages_seen"] += int(item["messages_seen"])
            result["messages_persisted"] += int(item["messages_persisted"])
            result["duplicates_skipped"] += int(item["duplicates_skipped"])
            _trace_increment_status(result, str(item["status"]))
        self._deps.conn.commit()
        return result

    async def _trace_account_messages(self, req: dict) -> dict:
        request, request_error = _parse_trace_account_messages_request(req)
        if request_error is not None:
            return request_error
        assert request is not None

        resolved_account = await self._resolve_trace_account(req)
        if resolved_account.get("confidence") != "resolved" or resolved_account.get("account_id") is None:
            return self._build_trace_account_unresolved_payload(resolved_account, request)

        target_user_id = int(resolved_account["account_id"])
        scope, scope_error = await _resolve_trace_account_scope(
            _TraceScopeResolveRequest(
                deps=self._deps,
                req=req,
                request=request,
                target_user_id=target_user_id,
            )
        )
        if scope_error is not None:
            return scope_error
        assert scope is not None

        post_author_aliases = _trace_post_author_aliases(resolved_account)
        query_result = self._build_trace_account_query_result(
            _TraceAccountQueryContext(
                target_user_id=target_user_id,
                request=request,
                scope=scope,
                post_author_aliases=post_author_aliases,
                conn=self._deps.conn,
                self_id=self._deps.self_id,
            )
        )

        selected_rows = query_result.selected_rows
        enrichment: dict | None = None
        if request.coverage_goal == "best_effort_visible":
            candidates = _trace_candidate_dialogs(
                _TraceCandidateBuildRequest(
                    conn=self._deps.conn,
                    target_user_id=target_user_id,
                    observed_rows=selected_rows,
                    exact_dialog_id=scope.exact_dialog_id,
                    exact_topic_id=scope.exact_topic_id,
                    linked_chat_map=scope.linked_chat_map,
                )
            )
            enrichment = await self._trace_enrich_visible_dialogs(
                target_user_id,
                candidates,
            )
            query_result = self._build_trace_account_query_result(
                _TraceAccountQueryContext(
                    target_user_id=target_user_id,
                    request=request,
                    scope=scope,
                    post_author_aliases=post_author_aliases,
                    conn=self._deps.conn,
                    self_id=self._deps.self_id,
                )
            )

        selected_rows = query_result.selected_rows

        coverage = _build_trace_coverage(
            self._deps.conn,
            target_user_id,
            selected_rows,
            exact_dialog_id=scope.exact_dialog_id,
            exact_topic_id=scope.exact_topic_id,
        )
        gaps = _build_trace_gaps(
            _TraceGapBuildRequest(
                conn=self._deps.conn,
                target_user_id=target_user_id,
                evidence=query_result.evidence,
                coverage=coverage,
                exact_dialog_id=scope.exact_dialog_id,
                exact_topic_id=scope.exact_topic_id,
            )
        )
        return self._build_trace_account_success_payload(
            _TraceAccountPayloadContext(
                resolved_account=resolved_account,
                request=request,
                query_result=query_result,
                coverage=coverage,
                gaps=gaps,
                enrichment=enrichment,
                post_author_aliases=post_author_aliases,
            )
        )


def _resolve_trace_visible_enrichment_request(
    request: _TraceVisibleEnrichmentRequest | None,
    overrides: dict[str, int],
) -> _TraceVisibleEnrichmentRequest:
    result = request or _TraceVisibleEnrichmentRequest()
    if "deadline_ms" in overrides:
        result = dataclasses.replace(result, deadline_ms=overrides["deadline_ms"])
    if "max_dialogs" in overrides:
        result = dataclasses.replace(result, max_dialogs=overrides["max_dialogs"])
    if "max_per_dialog" in overrides:
        result = dataclasses.replace(result, max_per_dialog=overrides["max_per_dialog"])
    if "concurrency" in overrides:
        result = dataclasses.replace(result, concurrency=overrides["concurrency"])
    return result


def _mark_budget_exceeded_candidates(
    conn: sqlite3.Connection,
    candidates: list[dict],
    request: _TraceVisibleBudgetContext,
) -> None:
    for candidate in candidates:
        _upsert_trace_coverage_fragment(
            _TraceCoverageFragmentUpsertRequest(
                conn=conn,
                target_user_id=request.target_user_id,
                dialog_id=int(candidate["dialog_id"]),
                topic_id=candidate.get("topic_id"),
                status="budget_exceeded",
                last_error=f"BudgetExceeded:{request.deadline_ms}",
                now=request.now,
            )
        )
        request.result["dialogs_skipped"] += 1
        _trace_increment_status(request.result, "budget_exceeded")


async def _run_trace_visible_candidates(
    request: _TraceVisibleCandidatesContext,
) -> list[dict]:
    semaphore = asyncio.Semaphore(max(1, request.concurrency))

    async def run_candidate(candidate: dict) -> dict:
        async with semaphore:
            return await request.service._trace_enrich_one_candidate(
                target_user_id=request.target_user_id,
                candidate=candidate,
                max_per_dialog=request.max_per_dialog,
                deadline_ms=request.deadline_ms,
                deadline_at=request.deadline_at,
            )

    return await asyncio.gather(*(run_candidate(candidate) for candidate in request.candidates))


def _trace_post_author_aliases(resolved_account: dict) -> list[str]:
    """Return post_author aliases for lower-confidence channel signature evidence."""
    return _unique_trace_aliases(
        resolved_account.get("username"),
        f"@{resolved_account.get('username')}" if resolved_account.get("username") else None,
        resolved_account.get("display_name"),
        *resolved_account.get("display_aliases", []),
    )


def _extract_trace_account_lookup(req: dict) -> _TraceAccountLookup:
    exact_account_id = _parse_trace_int(req.get("exact_account_id"))
    if exact_account_id is not None:
        return _TraceAccountLookup(mode="exact_id", query=exact_account_id)

    account = req.get("account")
    numeric_account_id = _parse_trace_int(account)
    if numeric_account_id is not None:
        return _TraceAccountLookup(mode="numeric_account", query=numeric_account_id)

    if not isinstance(account, str) or not account.strip():
        return _TraceAccountLookup(mode="missing", query=account)

    query = account.strip()
    tme = _parse_tme_link(query)
    if tme is not None:
        return _TraceAccountLookup(mode="username", query=tme[0])
    if query.startswith("@"):
        return _TraceAccountLookup(mode="username", query=query[1:])

    return _TraceAccountLookup(mode="fuzzy", query=query)


def _resolve_trace_account_by_id(
    conn: sqlite3.Connection,
    account_id: int,
    *,
    resolution_source: str,
    unresolved_source: str,
) -> dict:
    row = conn.execute(_TRACE_ACCOUNT_BY_ID_SQL, (account_id,)).fetchone()
    if row is None:
        return _unresolved_trace_account(query=account_id, resolution_source=unresolved_source)
    return _trace_account_from_entity_row(row, resolution_source=resolution_source)


def _resolve_trace_account_by_username(conn: sqlite3.Connection, username: str) -> dict | None:
    row = conn.execute(_ENTITY_BY_USERNAME_SQL, (username,)).fetchone()
    if row is None:
        return None
    return _trace_account_from_entity_row(row, resolution_source="entities_username")


def _resolve_trace_account_by_fuzzy(conn: sqlite3.Connection, query: str) -> dict:
    now = int(time.time())
    display_name_map = dict(conn.execute(_TRACE_ACCOUNT_NAMES_SQL, (now - USER_TTL, now - GROUP_TTL)).fetchall())
    normalized = dict(conn.execute(_TRACE_ACCOUNT_NAMES_NORMALIZED_SQL, (now - USER_TTL, now - GROUP_TTL)).fetchall())
    result = resolve(query, display_name_map, None, normalized_name_map=normalized)
    if isinstance(result, Resolved):
        row = conn.execute(_TRACE_ACCOUNT_BY_ID_SQL, (result.entity_id,)).fetchone()
        if row is not None:
            return _trace_account_from_entity_row(row, resolution_source="entities_fuzzy")
        return {
            "confidence": "resolved",
            "account_id": result.entity_id,
            "display_name": result.display_name,
            "username": None,
            "candidate_ids": [],
            "display_aliases": _unique_trace_aliases(result.display_name, latinize(result.display_name)),
            "resolution_source": "entities_fuzzy",
        }

    if isinstance(result, Candidates):
        candidate_ids = [int(match["entity_id"]) for match in result.matches]
        display_aliases = [str(match["display_name"]) for match in result.matches if match.get("display_name")]
        return _unresolved_trace_account(
            query=query,
            resolution_source="entities_fuzzy_candidates",
            candidate_ids=candidate_ids,
            display_aliases=display_aliases,
            confidence="ambiguous",
        )

    return _unresolved_trace_account(query=query, resolution_source="entities_fuzzy_not_found")


def _parse_trace_account_messages_request(req: dict) -> tuple[_TraceAccountMessagesRequest | None, dict | None]:
    group_by = req.get("group_by", "timeline")
    if group_by not in ("timeline", "dialog"):
        return None, {
            "ok": False,
            "error": "invalid_group_by",
            "message": "group_by must be timeline or dialog",
        }

    coverage_goal = req.get("coverage_goal", "observed")
    if coverage_goal not in ("observed", "best_effort_visible"):
        return None, {
            "ok": False,
            "error": "invalid_coverage_goal",
            "message": "coverage_goal must be observed or best_effort_visible",
        }

    exact_dialog_id = _parse_trace_int(req.get("exact_dialog_id"))
    exact_topic_id = _parse_trace_int(req.get("exact_topic_id"))
    limit = _clamp(int(req.get("limit", 50)), 1, 200)
    sent_after = req.get("sent_after")
    sent_before = req.get("sent_before")
    sent_after_ts = _parse_trace_time_bound(sent_after)
    if sent_after is not None and sent_after_ts is None:
        return None, {"ok": False, "error": "invalid_time_bound", "message": "sent_after is invalid"}
    sent_before_ts = _parse_trace_time_bound(sent_before)
    if sent_before is not None and sent_before_ts is None:
        return None, {"ok": False, "error": "invalid_time_bound", "message": "sent_before is invalid"}

    coverage_bounds = {
        "limit": limit,
        "exact_dialog_id": exact_dialog_id,
        "exact_topic_id": exact_topic_id,
        "sent_after": sent_after,
        "sent_before": sent_before,
    }
    return (
        _TraceAccountMessagesRequest(
            group_by=group_by,
            coverage_goal=coverage_goal,
            exact_dialog_id=exact_dialog_id,
            exact_topic_id=exact_topic_id,
            limit=limit,
            sent_after=sent_after,
            sent_before=sent_before,
            sent_after_ts=sent_after_ts,
            sent_before_ts=sent_before_ts,
            coverage_bounds=coverage_bounds,
        ),
        None,
    )


async def _resolve_trace_account_scope(
    request: _TraceScopeResolveRequest,
) -> tuple[_TraceAccountMessagesScope | None, dict | None]:
    exact_dialog_id = request.request.exact_dialog_id
    exact_topic_id = request.request.exact_topic_id
    linked_chat_map: dict[int, int] = {}
    scope_dialog_ids: list[int] | None = None
    navigation_payload: dict[str, int] | None = None

    resolved_dialog_id, scope_error = await _resolve_trace_account_scope_dialog_id(
        request.deps,
        request.req,
        exact_dialog_id,
    )
    if scope_error is not None:
        return None, scope_error
    exact_dialog_id = resolved_dialog_id

    validation_error = _validate_trace_account_scope_exact_topic(exact_dialog_id, exact_topic_id)
    if validation_error is not None:
        return None, validation_error

    signature_scope = await _resolve_trace_account_signature_scope(
        request.deps,
        exact_dialog_id,
    )
    if signature_scope is not None:
        scope_dialog_ids, linked_chat_map = signature_scope

    navigation_scope = _parse_trace_account_navigation_scope(
        request,
        exact_dialog_id=exact_dialog_id,
        exact_topic_id=exact_topic_id,
        decode_navigation=request.req.get("navigation"),
    )
    if navigation_scope is not None:
        nav_error: dict | None = navigation_scope.error
        if nav_error is not None:
            return None, nav_error
        if navigation_scope.scope_dialog_ids is not None:
            scope_dialog_ids = navigation_scope.scope_dialog_ids
        if navigation_scope.navigation_payload is not None:
            navigation_payload = navigation_scope.navigation_payload
        if exact_dialog_id is not None and navigation_scope.linked_chat_id is not None:
            linked_chat_map[exact_dialog_id] = navigation_scope.linked_chat_id

    return _TraceAccountMessagesScope(
        exact_dialog_id=exact_dialog_id,
        exact_topic_id=exact_topic_id,
        scope_dialog_ids=scope_dialog_ids,
        linked_chat_map=linked_chat_map,
        navigation_payload=navigation_payload,
    ), None


@dataclasses.dataclass(frozen=True, slots=True)
class _TraceNavigationScopeResult:
    navigation_payload: dict[str, int] | None
    scope_dialog_ids: list[int] | None
    linked_chat_id: int | None
    error: dict[str, object] | None


async def _resolve_trace_account_scope_dialog_id(
    deps: DaemonAccountTraceDeps,
    req: dict,
    exact_dialog_id: int | None,
) -> tuple[int | None, dict | None]:
    if exact_dialog_id is not None:
        return exact_dialog_id, None

    dialog = req.get("dialog")
    if not isinstance(dialog, str) or not dialog.strip():
        return None, None

    resolved_dialog = await deps.resolve_dialog_id(0, dialog)
    if isinstance(resolved_dialog, dict):
        return None, resolved_dialog
    return resolved_dialog, None


def _validate_trace_account_scope_exact_topic(
    exact_dialog_id: int | None,
    exact_topic_id: int | None,
) -> dict | None:
    if exact_topic_id is not None and exact_dialog_id is None:
        return {
            "ok": False,
            "error": "invalid_topic_scope",
            "message": "exact_topic_id requires exact_dialog_id or dialog",
        }
    return None


async def _resolve_trace_account_signature_scope(
    deps: DaemonAccountTraceDeps,
    exact_dialog_id: int | None,
) -> tuple[list[int] | None, dict[int, int]] | None:
    if exact_dialog_id is None:
        return None

    meta = _trace_dialog_metadata(deps.conn, exact_dialog_id)
    if (
        _trace_strategy_for_dialog(meta["dialog_type"], status=meta["status"], hidden=bool(meta["hidden"]))
        != "signature_only"
    ):
        return None

    resolution = await resolve_linked_chat_id(deps.client, deps.conn, exact_dialog_id)
    if resolution.flood_wait_seconds is not None:
        return None
    if resolution.linked_chat_id is None:
        return None

    linked_chat_map: dict[int, int] = {exact_dialog_id: resolution.linked_chat_id}
    scope_dialog_ids = [exact_dialog_id, resolution.linked_chat_id]
    enroll_activity_dialog(deps.conn, resolution.linked_chat_id, source="linked_chat")
    return scope_dialog_ids, linked_chat_map


def _parse_trace_account_navigation_scope(
    request: _TraceScopeResolveRequest,
    *,
    exact_dialog_id: int | None,
    exact_topic_id: int | None,
    decode_navigation: object,
) -> _TraceNavigationScopeResult:
    from .pagination import AccountTraceNavigationContext, decode_account_trace_navigation

    if not isinstance(decode_navigation, str) or not decode_navigation:
        return _TraceNavigationScopeResult(None, None, None, None)

    try:
        expected_group_by: Literal["timeline", "dialog"] = "timeline"
        if request.request.group_by == "dialog":
            expected_group_by = "dialog"
        decoded = decode_account_trace_navigation(
            decode_navigation,
            AccountTraceNavigationContext(
                expected_target_user_id=request.target_user_id,
                expected_group_by=expected_group_by,
                expected_exact_dialog_id=exact_dialog_id,
                expected_exact_topic_id=exact_topic_id,
                expected_sent_after=cast("str | None", request.request.sent_after),
                expected_sent_before=cast("str | None", request.request.sent_before),
            ),
        )
    except ValueError as exc:
        return _TraceNavigationScopeResult(
            None, None, None, {"ok": False, "error": "invalid_navigation", "message": str(exc)}
        )

    linked_chat_id: int | None = None
    scope_dialog_ids = decoded.scope_dialog_ids
    if (
        exact_dialog_id is not None
        and decoded.scope_dialog_ids is not None
        and len(decoded.scope_dialog_ids) == _TRACE_SCOPE_DIALOG_IDS_LEN
    ):
        channel_id_from_token = decoded.scope_dialog_ids[0]
        linked_id_from_token = decoded.scope_dialog_ids[1]
        if channel_id_from_token == exact_dialog_id:
            linked_chat_id = linked_id_from_token

    return _TraceNavigationScopeResult(
        navigation_payload={
            "sent_at": decoded.sent_at,
            "dialog_id": decoded.dialog_id,
            "message_id": decoded.message_id,
        },
        scope_dialog_ids=scope_dialog_ids,
        linked_chat_id=linked_chat_id,
        error=None,
    )


def _trace_enrich_candidate_precheck(request: _TraceEnrichPrecheckContext) -> str | None:
    fragment = request.conn.execute(
        """
        SELECT next_retry_at FROM trace_coverage_fragments
        WHERE target_user_id = ? AND dialog_id = ? AND topic_id = ? AND coverage_kind = 'authored_message'
        """,
        (request.target_user_id, request.dialog_id, 0 if request.topic_id is None else int(request.topic_id)),
    ).fetchone()
    if fragment is not None and fragment[0] is not None and int(fragment[0]) > request.now:
        _upsert_trace_coverage_fragment(
            _TraceCoverageFragmentUpsertRequest(
                conn=request.conn,
                target_user_id=request.target_user_id,
                dialog_id=request.dialog_id,
                topic_id=request.topic_id,
                status="pending",
                fetched_at=request.now,
                last_error=None,
                now=request.now,
            )
        )
        return "pending"

    if request.strategy in {"hidden", "access_lost", "unsupported", "signature_only"}:
        status = {
            "hidden": "unsupported",
            "access_lost": "access_lost",
            "unsupported": "unsupported",
            "signature_only": "unsupported",
        }[request.strategy]
        _upsert_trace_coverage_fragment(
            _TraceCoverageFragmentUpsertRequest(
                conn=request.conn,
                target_user_id=request.target_user_id,
                dialog_id=request.dialog_id,
                topic_id=request.topic_id,
                status=status,
                fetched_at=request.now,
                last_error=f"{request.strategy}:no_author_search",
                now=request.now,
            )
        )
        return status

    if time.monotonic() >= request.deadline_at:
        _upsert_trace_coverage_fragment(
            _TraceCoverageFragmentUpsertRequest(
                conn=request.conn,
                target_user_id=request.target_user_id,
                dialog_id=request.dialog_id,
                topic_id=request.topic_id,
                status="budget_exceeded",
                last_error=f"BudgetExceeded:{request.deadline_ms}",
                now=request.now,
            )
        )
        return "budget_exceeded"

    return None


async def _trace_enrich_candidate_messages(
    request: _TraceCandidateMessagesContext,
) -> tuple[list[ExtractedMessage], str | None]:
    fetched: list[ExtractedMessage] = []
    try:
        async for msg in request.client.iter_messages(request.dialog_id, **request.iter_kwargs):
            if time.monotonic() >= request.deadline_at:
                break
            fetched.append(extract_message_row(request.dialog_id, msg, entity_name_map={}))
    except FloodWaitError as exc:
        seconds = int(getattr(exc, "seconds", 0))
        _upsert_trace_coverage_fragment(
            _TraceCoverageFragmentUpsertRequest(
                conn=request.conn,
                target_user_id=request.target_user_id,
                dialog_id=request.dialog_id,
                status="flood_wait",
                last_error=f"FloodWaitError:{seconds}",
                next_retry_at=request.now + seconds,
                now=request.now,
            )
        )
        return fetched, "flood_wait"
    except _ACCESS_LOST_ERRORS as exc:
        _upsert_trace_coverage_fragment(
            _TraceCoverageFragmentUpsertRequest(
                conn=request.conn,
                target_user_id=request.target_user_id,
                dialog_id=request.dialog_id,
                status="access_lost",
                last_error=type(exc).__name__,
                now=request.now,
            )
        )
        return fetched, "access_lost"
    except RPCError as exc:
        _upsert_trace_coverage_fragment(
            _TraceCoverageFragmentUpsertRequest(
                conn=request.conn,
                target_user_id=request.target_user_id,
                dialog_id=request.dialog_id,
                status="partial",
                last_error=type(exc).__name__,
                now=request.now,
            )
        )
        return fetched, "partial"
    except (RuntimeError, TypeError, AttributeError, ValueError, sqlite3.Error) as exc:
        _upsert_trace_coverage_fragment(
            _TraceCoverageFragmentUpsertRequest(
                conn=request.conn,
                target_user_id=request.target_user_id,
                dialog_id=request.dialog_id,
                status="partial",
                last_error=type(exc).__name__,
                now=request.now,
            )
        )
        return fetched, "partial"
    return fetched, None


def _trace_candidate_status_after_fetch(
    *,
    fetched_count: int,
    max_per_dialog: int,
    deadline_ms: int,
    deadline_at: float,
) -> str:
    del deadline_ms
    status = "partial" if fetched_count >= max_per_dialog else "complete"
    if time.monotonic() >= deadline_at:
        status = "budget_exceeded"
    return status


def _dedupe_trace_messages(fetched: list[ExtractedMessage]) -> tuple[list[ExtractedMessage], int]:
    unique: dict[tuple[int, int], ExtractedMessage] = {}
    for extracted in fetched:
        key = (extracted.message.dialog_id, extracted.message.message_id)
        unique[key] = extracted
    return list(unique.values()), len(fetched) - len(unique)


def _split_trace_duplicate_messages(
    *,
    conn: sqlite3.Connection,
    messages: list[ExtractedMessage],
) -> tuple[list[ExtractedMessage], int]:
    changed: list[ExtractedMessage] = []
    duplicates = 0
    for extracted in messages:
        existing = _trace_existing_message_bundle(
            conn,
            dialog_id=int(extracted.message.dialog_id),
            message_id=int(extracted.message.message_id),
        )
        if _messages_row_equal(existing, extracted):
            duplicates += 1
        else:
            changed.append(extracted)
    return changed, duplicates


def _persist_trace_messages(conn: sqlite3.Connection, messages: list[ExtractedMessage]) -> None:
    with conn:
        insert_messages_with_fts(conn, messages)


_TRACE_FRAGMENT_STATUSES = {
    "pending",
    "partial",
    "complete",
    "flood_wait",
    "access_lost",
    "unsupported",
    "budget_exceeded",
}
_TRACE_PARTIAL_SYNC_STATUSES = {"fragment", "own_only", "syncing", "access_lost"}
_TRACE_PARTIAL_FRAGMENT_STATUSES = {
    "pending",
    "partial",
    "flood_wait",
    "access_lost",
    "unsupported",
    "budget_exceeded",
}
_TRACE_GAP_SEVERITIES = {"info", "warning", "action_required"}

_TRACE_MESSAGE_BASE_FIELDS = (
    "dialog_id",
    "message_id",
    "sent_at",
    "text",
    "sender_id",
    "sender_first_name",
    "media_description",
    "reply_to_msg_id",
    "reply_count",
    "forum_topic_id",
    "edit_date",
    "grouped_id",
    "reply_to_peer_id",
    "out",
    "is_service",
    "post_author",
)
_TRACE_MESSAGE_COMPARE_FIELDS = (*_TRACE_MESSAGE_BASE_FIELDS, "is_deleted")


@dataclasses.dataclass(frozen=True, slots=True)
class _TraceCoverageFragmentUpsertRequest:
    conn: sqlite3.Connection
    target_user_id: int
    dialog_id: int
    status: str
    topic_id: int | None = None
    coverage_kind: str = "authored_message"
    fetched_at: int | None = None
    checkpoint: str | None = None
    last_error: str | None = None
    next_retry_at: int | None = None
    now: int | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class _TraceGapCandidate:
    kind: str
    severity: str
    detail: str
    dialog_id: int | None = None
    topic_id: int | None = None
    action: dict | None = None
    next_action: dict | None = None
    extra: dict | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class _TraceGapBuildRequest:
    conn: sqlite3.Connection
    target_user_id: int
    evidence: list[dict]
    coverage: dict
    exact_dialog_id: int | None = None
    exact_topic_id: int | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class _TraceCandidateBuildRequest:
    conn: sqlite3.Connection
    target_user_id: int
    observed_rows: list[sqlite3.Row] | list[dict]
    exact_dialog_id: int | None = None
    exact_topic_id: int | None = None
    max_dialogs: int = _TRACE_ENRICHMENT_MAX_DIALOGS
    linked_chat_map: dict[int, int] | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class _TraceCandidateBuildState:
    request: _TraceCandidateBuildRequest
    candidates: list[dict]
    seen: set[int]
    linked_chat_map: dict[int, int]


@dataclasses.dataclass(frozen=True, slots=True)
class _TraceMessageQueryRequest:
    target_user_id: int
    self_id: int | None
    limit: int
    post_author_aliases: list[str] | None = None
    exact_dialog_id: int | None = None
    exact_topic_id: int | None = None
    sent_after_ts: int | None = None
    sent_before_ts: int | None = None
    navigation: dict[str, int] | None = None
    scope_dialog_ids: list[int] | None = None


def _parse_trace_int(value: object) -> int | None:
    """Return an int for a signed numeric trace selector, otherwise None."""
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return None
    selector = value.strip()
    if not selector:
        return None
    if selector.isdigit():
        return int(selector)
    if selector[0] in "+-" and selector[1:].isdigit():
        return int(selector)
    return None


def _trace_account_from_entity_row(row: sqlite3.Row, *, resolution_source: str) -> dict:
    """Convert an entities row into the Account Trace resolution envelope."""
    account_id = int(row["id"])
    display_name = row["name"]
    username = row["username"]
    display_aliases = _unique_trace_aliases(
        display_name,
        username,
        f"@{username}" if username else None,
        row["name_normalized"],
    )
    return {
        "confidence": "resolved",
        "account_id": account_id,
        "display_name": display_name,
        "username": username,
        "candidate_ids": [],
        "display_aliases": display_aliases,
        "resolution_source": resolution_source,
    }


def _unresolved_trace_account(
    *,
    query: object,
    resolution_source: str,
    candidate_ids: list[int] | None = None,
    display_aliases: list[str] | None = None,
    confidence: str = "unresolved",
) -> dict:
    """Build a normal non-exception trace resolution failure envelope."""
    return {
        "confidence": confidence,
        "account_id": None,
        "display_name": str(query) if query is not None else None,
        "username": None,
        "candidate_ids": candidate_ids or [],
        "display_aliases": display_aliases or [],
        "resolution_source": resolution_source,
    }


def _parse_trace_time_bound(value: object) -> int | None:
    """Parse a trace time bound as unix seconds or ISO datetime string."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return None
    return _parse_trace_time_bound_from_string(value)


def _parse_trace_time_bound_from_string(value: str) -> int | None:
    text = value.strip()
    if not text:
        return None
    parsed_int = _parse_trace_int(text)
    if parsed_int is not None:
        return parsed_int
    iso_text = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(iso_text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp())


def _get_trace_coverage_fragments(
    conn: sqlite3.Connection,
    *,
    target_user_id: int,
    exact_dialog_id: int | None = None,
    exact_topic_id: int | None = None,
    coverage_kind: str = "authored_message",
) -> list[dict]:
    """Read target-specific Account Trace coverage fragment rows."""
    sql = (
        "SELECT target_user_id, dialog_id, topic_id, coverage_kind, status, "
        "fetched_at, checkpoint, last_error, next_retry_at, created_at, updated_at "
        "FROM trace_coverage_fragments "
        "WHERE target_user_id = :target_user_id AND coverage_kind = :coverage_kind"
    )
    params: dict[str, object] = {
        "target_user_id": target_user_id,
        "coverage_kind": coverage_kind,
    }
    if exact_dialog_id is not None:
        sql += " AND dialog_id = :exact_dialog_id"
        params["exact_dialog_id"] = exact_dialog_id
    if exact_topic_id is not None:
        sql += " AND topic_id = :exact_topic_id"
        params["exact_topic_id"] = exact_topic_id
    rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def _sanitize_trace_last_error(last_error: str | None) -> str | None:
    if last_error is None:
        return None
    compact = " ".join(last_error.split())
    return compact[:120]


def _upsert_trace_coverage_fragment(
    request: _TraceCoverageFragmentUpsertRequest,
) -> None:
    """Insert/update one target-specific coverage fragment."""
    conn = request.conn
    if request.status not in _TRACE_FRAGMENT_STATUSES:
        raise ValueError(f"invalid trace coverage status: {request.status}")
    timestamp = request.now if request.now is not None else int(time.time())
    conn.execute(
        """
        INSERT INTO trace_coverage_fragments
            (target_user_id, dialog_id, topic_id, coverage_kind, status,
             fetched_at, checkpoint, last_error, next_retry_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(target_user_id, dialog_id, topic_id, coverage_kind)
        DO UPDATE SET
            status = excluded.status,
            fetched_at = excluded.fetched_at,
            checkpoint = excluded.checkpoint,
            last_error = excluded.last_error,
            next_retry_at = excluded.next_retry_at,
            updated_at = excluded.updated_at
        """,
        (
            request.target_user_id,
            request.dialog_id,
            0 if request.topic_id is None else request.topic_id,
            request.coverage_kind,
            request.status,
            request.fetched_at,
            request.checkpoint,
            _sanitize_trace_last_error(request.last_error),
            request.next_retry_at,
            timestamp,
            timestamp,
        ),
    )


def _row_value(row: sqlite3.Row | dict, key: str) -> object:
    if isinstance(row, dict):
        return row.get(key)
    return row[key]


def _row_int(row: sqlite3.Row | dict, key: str) -> int:
    value = _row_value(row, key)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    msg = f"{key} must be an integer"
    raise ValueError(msg)


def _dialog_status_map(conn: sqlite3.Connection, dialog_ids: set[int]) -> dict[int, str | None]:
    if not dialog_ids:
        return {}
    placeholders = ",".join("?" * len(dialog_ids))
    rows = conn.execute(
        f"SELECT dialog_id, status FROM synced_dialogs WHERE dialog_id IN ({placeholders})",
        tuple(dialog_ids),
    ).fetchall()
    result: dict[int, str | None] = {int(row[0]): str(row[1]) for row in rows}
    for dialog_id in dialog_ids:
        result.setdefault(dialog_id, None)
    return result


def _build_trace_coverage(
    conn: sqlite3.Connection,
    target_user_id: int,
    rows: list[sqlite3.Row] | list[dict],
    *,
    exact_dialog_id: int | None = None,
    exact_topic_id: int | None = None,
) -> dict:
    """Build bounded Account Trace coverage semantics for the current response."""
    observed_dialogs = {_row_int(row, "dialog_id") for row in rows}
    fragments = _get_trace_coverage_fragments(
        conn,
        target_user_id=target_user_id,
        exact_dialog_id=exact_dialog_id,
        exact_topic_id=exact_topic_id,
    )
    fragment_dialogs = {int(fragment["dialog_id"]) for fragment in fragments}

    if exact_dialog_id is not None:
        considered_dialogs = {exact_dialog_id}
        basis = "exact_dialog_scope"
    else:
        access_lost_dialogs = {
            int(row[0])
            for row in conn.execute("SELECT dialog_id FROM synced_dialogs WHERE status = 'access_lost'").fetchall()
        }
        considered_dialogs = observed_dialogs | fragment_dialogs | access_lost_dialogs
        basis = "evidence_or_fragments_or_access_lost" if considered_dialogs else "none"

    status_by_dialog = _dialog_status_map(conn, considered_dialogs)
    gap_dialogs: set[int] = set()
    for dialog_id, status in status_by_dialog.items():
        if status is None or status in _TRACE_PARTIAL_SYNC_STATUSES:
            gap_dialogs.add(dialog_id)
    for fragment in fragments:
        if str(fragment["status"]) in _TRACE_PARTIAL_FRAGMENT_STATUSES:
            gap_dialogs.add(int(fragment["dialog_id"]))

    if not considered_dialogs:
        state = "unknown"
    elif gap_dialogs:
        state = "partial"
    else:
        state = "complete"

    return {
        "state": state,
        "observed_message_count": len(rows),
        "dialogs_considered": len(considered_dialogs),
        "dialogs_considered_basis": basis,
        "dialogs_with_hits": len(observed_dialogs),
        "dialogs_with_gaps": len(gap_dialogs),
        "as_of": int(time.time()),
    }


def _trace_gap_for_dialog_status(
    *,
    status: str | None,
    dialog_id: int,
    exact_dialog_id: int | None,
    exact_topic_id: int | None,
) -> _TraceGapCandidate | None:
    topic_id = exact_topic_id if dialog_id == exact_dialog_id else None
    if status is None or status == "not_synced":
        return _TraceGapCandidate(
            kind="dialog_not_synced",
            severity="action_required",
            detail="This dialog has not been synced for Account Trace evidence.",
            dialog_id=dialog_id,
            topic_id=topic_id,
            action={"tool": "mark_dialog_for_sync", "arguments": {"dialog_id": dialog_id}},
        )
    if status == "access_lost":
        return _TraceGapCandidate(
            kind="access_lost",
            severity="warning",
            detail="The local archive has no current access to this dialog.",
            dialog_id=dialog_id,
            topic_id=topic_id,
        )
    if status in {"fragment", "own_only"}:
        return _TraceGapCandidate(
            kind="fragment_only",
            severity="warning",
            detail=f"Dialog coverage is {status}; Account Trace may be incomplete.",
            dialog_id=dialog_id,
            topic_id=topic_id,
        )
    if status == "syncing":
        return _TraceGapCandidate(
            kind="history_incomplete",
            severity="warning",
            detail="Dialog sync is still in progress.",
            dialog_id=dialog_id,
            topic_id=topic_id,
        )
    return None


def _trace_gap_for_fragment_status(
    status: str,
    dialog_id: int,
    topic_id: int | None,
) -> _TraceGapCandidate | None:
    if status == "flood_wait":
        return _TraceGapCandidate(
            kind="flood_wait",
            severity="warning",
            detail="Targeted trace enrichment is waiting for Telegram rate-limit cooldown.",
            dialog_id=dialog_id,
            topic_id=topic_id,
            extra={},
        )
    if status == "budget_exceeded":
        return _TraceGapCandidate(
            kind="budget_exceeded",
            severity="warning",
            detail="Bounded trace enrichment exhausted its request budget.",
            dialog_id=dialog_id,
            topic_id=topic_id,
        )
    if status == "unsupported":
        return _TraceGapCandidate(
            kind="history_incomplete",
            severity="warning",
            detail="This dialog type is not supported for targeted enrichment.",
            dialog_id=dialog_id,
            topic_id=topic_id,
        )
    return None


def _trace_gap(request: _TraceGapCandidate) -> dict:
    if request.severity not in _TRACE_GAP_SEVERITIES:
        raise ValueError(f"invalid trace gap severity: {request.severity}")
    gap: dict[str, object] = {
        "kind": request.kind,
        "severity": request.severity,
        "detail": request.detail,
    }
    if request.dialog_id is not None:
        gap["dialog_id"] = request.dialog_id
    if request.topic_id is not None:
        gap["topic_id"] = request.topic_id
    if request.action is not None:
        gap["action"] = request.action
    if request.next_action is not None:
        gap["next_action"] = request.next_action
    if request.extra:
        gap.update(request.extra)
    return gap


def _build_trace_gaps(
    request: _TraceGapBuildRequest,
) -> list[dict]:
    """Build controlled Account Trace coverage gaps and actions."""
    fragment_rows = _get_trace_coverage_fragments(
        request.conn,
        target_user_id=request.target_user_id,
        exact_dialog_id=request.exact_dialog_id,
        exact_topic_id=request.exact_topic_id,
    )
    considered_dialogs = _collect_trace_gap_dialogs(request, fragment_rows=fragment_rows)
    status_by_dialog = _dialog_status_map(request.conn, considered_dialogs)
    gaps = _collect_trace_gaps_for_dialog_statuses(
        request=request,
        status_by_dialog=status_by_dialog,
        considered_dialogs=considered_dialogs,
    )
    gaps.extend(_collect_trace_gaps_for_hidden_dialogs(request=request, considered_dialogs=considered_dialogs))
    gaps.extend(_collect_trace_gaps_for_fragment_rows(request=request, fragment_rows=fragment_rows))
    gaps.extend(_collect_trace_gaps_for_evidence(request.evidence))
    if not request.evidence and not gaps:
        gaps.append(_build_observed_zero_trace_gap())
    return gaps


def _collect_trace_gap_dialogs(
    request: _TraceGapBuildRequest,
    fragment_rows: list[dict],
) -> set[int]:
    dialog_ids = {int(item["dialog_id"]) for item in request.evidence}
    dialog_ids.update(int(row["dialog_id"]) for row in fragment_rows)
    if request.exact_dialog_id is not None:
        dialog_ids.add(request.exact_dialog_id)
    elif request.coverage.get("dialogs_considered", 0):
        dialog_ids.update(
            int(row[0])
            for row in request.conn.execute(
                "SELECT dialog_id FROM synced_dialogs WHERE status = 'access_lost'"
            ).fetchall()
        )
    return dialog_ids


def _collect_trace_gaps_for_dialog_statuses(
    request: _TraceGapBuildRequest,
    status_by_dialog: dict[int, str | None],
    *,
    considered_dialogs: set[int],
) -> list[dict]:
    gaps: list[dict] = []
    for dialog_id in sorted(considered_dialogs):
        candidate = _trace_gap_for_dialog_status(
            status=status_by_dialog.get(dialog_id),
            dialog_id=dialog_id,
            exact_dialog_id=request.exact_dialog_id,
            exact_topic_id=request.exact_topic_id,
        )
        if candidate is not None:
            gaps.append(_trace_gap(candidate))
    return gaps


def _collect_trace_gaps_for_hidden_dialogs(
    request: _TraceGapBuildRequest,
    *,
    considered_dialogs: set[int],
) -> list[dict]:
    hidden_rows = request.conn.execute("SELECT dialog_id FROM dialogs WHERE hidden = 1").fetchall()
    hidden_dialogs = {int(row[0]) for row in hidden_rows}
    return [
        _trace_gap(
            _TraceGapCandidate(
                kind="hidden_dialog",
                severity="warning",
                detail="Dialog is hidden in the local mirror.",
                dialog_id=dialog_id,
            )
        )
        for dialog_id in sorted(considered_dialogs & hidden_dialogs)
    ]


def _collect_trace_gaps_for_fragment_rows(
    request: _TraceGapBuildRequest,
    *,
    fragment_rows: list[dict],
) -> list[dict]:
    gaps: list[dict] = []
    for fragment in fragment_rows:
        candidate = _trace_gap_for_fragment(request=request, fragment=fragment)
        if candidate is None:
            continue
        gaps.append(_trace_gap(candidate))
    return gaps


def _trace_gap_for_fragment(
    request: _TraceGapBuildRequest,
    *,
    fragment: dict,
) -> _TraceGapCandidate | None:
    dialog_id = int(fragment["dialog_id"])
    topic_id = int(fragment["topic_id"])
    status = str(fragment["status"])
    candidate = _trace_gap_for_fragment_status(
        status,
        dialog_id=dialog_id,
        topic_id=None if topic_id == 0 else topic_id,
    )
    if candidate is None:
        return None
    if status == "flood_wait":
        return dataclasses.replace(
            candidate,
            extra={"next_retry_at": fragment.get("next_retry_at"), **(candidate.extra or {})},
        )
    return candidate


def _collect_trace_gaps_for_evidence(evidence: list[dict]) -> list[dict]:
    if not any(item.get("authorship_basis") == "post_author_signature" for item in evidence):
        return []
    return [
        _trace_gap(
            _TraceGapCandidate(
                kind="channel_signature_ambiguous",
                severity="info",
                detail="Channel post signatures are author text, not numeric Telegram user identity proof.",
            )
        )
    ]


def _build_observed_zero_trace_gap() -> dict:
    return _trace_gap(
        _TraceGapCandidate(
            kind="observed_zero",
            severity="info",
            detail="No authored-message evidence was observed in the considered local coverage.",
        )
    )


def _trace_strategy_for_dialog(dialog_type: str, *, status: str | None, hidden: bool) -> str:
    if hidden:
        return "hidden"
    if status == "access_lost":
        return "access_lost"
    dt = DialogType.parse(dialog_type)
    if dt in (DialogType.USER, DialogType.BOT):
        return "dialog_scan"
    if dt in (DialogType.SUPERGROUP, DialogType.FORUM, DialogType.GROUP):
        return "author_search"
    if dt == DialogType.CHANNEL:
        return "signature_only"
    return "unsupported"


def _trace_dialog_metadata(conn: sqlite3.Connection, dialog_id: int) -> dict:
    row = conn.execute(
        """
        SELECT
            COALESCE(d.type, e.type, 'Unknown') AS dialog_type,
            COALESCE(sd.status, 'not_synced') AS status,
            COALESCE(d.hidden, 0) AS hidden
        FROM (SELECT ? AS dialog_id) x
        LEFT JOIN dialogs d ON d.dialog_id = x.dialog_id
        LEFT JOIN entities e ON e.id = x.dialog_id
        LEFT JOIN synced_dialogs sd ON sd.dialog_id = x.dialog_id
        """,
        (dialog_id,),
    ).fetchone()
    return {
        "dialog_type": str(row[0]) if row else "Unknown",
        "status": str(row[1]) if row else "not_synced",
        "hidden": bool(row[2]) if row else False,
    }


def _trace_common_chat_ids(conn: sqlite3.Connection, target_user_id: int) -> list[int]:
    row = conn.execute(
        "SELECT detail_json FROM entity_details WHERE entity_id = ?",
        (target_user_id,),
    ).fetchone()
    if row is None:
        return []
    try:
        detail = json.loads(row[0])
    except TypeError, json.JSONDecodeError:
        return []
    common_chats = detail.get("common_chats", [])
    if not isinstance(common_chats, list):
        return []
    ids: list[int] = []
    for item in common_chats:
        if not isinstance(item, dict):
            continue
        raw_id = item.get("id")
        if raw_id is None:
            continue
        try:
            ids.append(int(raw_id))
        except TypeError, ValueError:
            continue
    return ids


def _trace_candidate_dialogs(
    request: _TraceCandidateBuildRequest,
) -> list[dict]:
    """Select deterministic bounded Account Trace enrichment candidates."""
    now = int(time.time())
    state = _TraceCandidateBuildState(
        request=request,
        candidates=[],
        seen=set(),
        linked_chat_map=request.linked_chat_map or {},
    )
    _collect_trace_candidate_dialogs(
        state=state,
        now=now,
    )
    return state.candidates


def _collect_trace_candidate_dialogs(
    *,
    state: _TraceCandidateBuildState,
    now: int,
) -> None:
    request = state.request
    if request.exact_dialog_id is not None:
        _add_trace_candidate_dialog(
            state=state,
            dialog_id=request.exact_dialog_id,
            origin="exact_dialog",
            include_inaccessible=True,
        )
    for row in request.observed_rows:
        _add_trace_candidate_dialog(
            state=state,
            dialog_id=_row_int(row, "dialog_id"),
            origin="observed_evidence",
        )
    _add_trace_candidate_fragments(
        state=state,
        now=now,
    )
    _add_trace_candidate_common_chats(
        state=state,
    )
    _add_trace_candidate_visible_synced(
        state=state,
    )


def _add_trace_candidate_dialog(
    state: _TraceCandidateBuildState,
    *,
    dialog_id: int,
    origin: str,
    include_inaccessible: bool = False,
) -> None:
    request = state.request
    if dialog_id in state.seen or len(state.candidates) >= request.max_dialogs:
        return
    meta = _trace_dialog_metadata(request.conn, dialog_id)
    if not include_inaccessible and (meta["status"] == "access_lost" or meta["hidden"]):
        return
    strategy = _trace_strategy_for_dialog(
        meta["dialog_type"],
        status=meta["status"],
        hidden=bool(meta["hidden"]),
    )
    state.candidates.append(
        {
            "dialog_id": dialog_id,
            "dialog_type": meta["dialog_type"],
            "status": meta["status"],
            "hidden": bool(meta["hidden"]),
            "strategy": strategy,
            "origin": origin,
            "topic_id": request.exact_topic_id if request.exact_dialog_id == dialog_id else None,
        }
    )
    state.seen.add(dialog_id)

    linked_chat_map = state.linked_chat_map
    if strategy == "signature_only" and dialog_id in linked_chat_map:
        linked_id = linked_chat_map[dialog_id]
        if linked_id not in state.seen:
            enroll_activity_dialog(request.conn, linked_id, source="linked_chat")
            _add_trace_candidate_dialog(
                state=state,
                dialog_id=linked_id,
                origin="linked_chat",
            )


def _add_trace_candidate_fragments(
    state: _TraceCandidateBuildState,
    *,
    now: int,
) -> None:
    request = state.request
    fragment_rows = request.conn.execute(
        """
        SELECT dialog_id
        FROM trace_coverage_fragments
        WHERE target_user_id = ?
          AND status != 'complete'
          AND (next_retry_at IS NULL OR next_retry_at <= ?)
        ORDER BY updated_at ASC, dialog_id ASC
        """,
        (request.target_user_id, now),
    ).fetchall()
    for row in fragment_rows:
        _add_trace_candidate_dialog(
            state=state,
            dialog_id=int(row[0]),
            origin="trace_fragment_retry",
        )


def _add_trace_candidate_common_chats(
    state: _TraceCandidateBuildState,
) -> None:
    request = state.request
    for dialog_id in _trace_common_chat_ids(request.conn, request.target_user_id):
        _add_trace_candidate_dialog(
            state=state,
            dialog_id=dialog_id,
            origin="cached_common_chat",
        )


def _add_trace_candidate_visible_synced(
    state: _TraceCandidateBuildState,
) -> None:
    request = state.request
    visible_rows = request.conn.execute(
        """
        SELECT sd.dialog_id
        FROM synced_dialogs sd
        LEFT JOIN dialogs d ON d.dialog_id = sd.dialog_id
        WHERE sd.status != 'access_lost'
          AND COALESCE(d.hidden, 0) = 0
        ORDER BY sd.dialog_id ASC
        """
    ).fetchall()
    for row in visible_rows:
        _add_trace_candidate_dialog(
            state=state,
            dialog_id=int(row[0]),
            origin="visible_synced",
        )


def _trace_existing_message_bundle(
    conn: sqlite3.Connection,
    *,
    dialog_id: int,
    message_id: int,
) -> dict | None:
    columns = ", ".join(_TRACE_MESSAGE_COMPARE_FIELDS)
    row = conn.execute(
        f"SELECT {columns} FROM messages WHERE dialog_id = ? AND message_id = ?",
        (dialog_id, message_id),
    ).fetchone()
    if row is None:
        return None
    return {
        "message": {field: row[index] for index, field in enumerate(_TRACE_MESSAGE_COMPARE_FIELDS)},
        "reactions": sorted(
            tuple(item)
            for item in conn.execute(
                """
                SELECT emoji, count FROM message_reactions
                WHERE dialog_id = ? AND message_id = ?
                ORDER BY emoji, count
                """,
                (dialog_id, message_id),
            ).fetchall()
        ),
        "entities": sorted(
            tuple(item)
            for item in conn.execute(
                """
                SELECT offset, length, type, value FROM message_entities
                WHERE dialog_id = ? AND message_id = ?
                ORDER BY offset, length, type, value
                """,
                (dialog_id, message_id),
            ).fetchall()
        ),
        "forward": (
            tuple(forward_row)
            if (
                forward_row := conn.execute(
                    """
                    SELECT fwd_from_peer_id, fwd_from_name, fwd_date, fwd_channel_post
                    FROM message_forwards
                    WHERE dialog_id = ? AND message_id = ?
                    """,
                    (dialog_id, message_id),
                ).fetchone()
            )
            else None
        ),
    }


def _messages_row_equal(existing: dict | None, candidate: ExtractedMessage) -> bool:
    """Compare existing base/child rows with one extracted candidate bundle."""
    if existing is None:
        return False

    existing_message = existing.get("message", {})
    if existing_message.get("is_deleted") != 0:
        return False

    candidate_message = dataclasses.asdict(candidate.message)
    candidate_message["is_deleted"] = 0
    for field in _TRACE_MESSAGE_COMPARE_FIELDS:
        existing_value = existing_message.get(field, 0 if field == "reply_count" else None)
        candidate_value = candidate.reply_count if field == "reply_count" else candidate_message.get(field)
        if existing_value != candidate_value:
            return False

    candidate_reactions = sorted((item.emoji, item.count) for item in candidate.reactions)
    if existing.get("reactions", []) != candidate_reactions:
        return False

    candidate_entities = sorted((item.offset, item.length, item.type, item.value) for item in candidate.entities)
    if existing.get("entities", []) != candidate_entities:
        return False

    candidate_forward = (
        None
        if candidate.forward is None
        else (
            candidate.forward.fwd_from_peer_id,
            candidate.forward.fwd_from_name,
            candidate.forward.fwd_date,
            candidate.forward.fwd_channel_post,
        )
    )
    return existing.get("forward") == candidate_forward


def _trace_enrichment_result(
    *,
    deadline_ms: int,
    concurrency: int,
    max_dialogs: int,
    max_per_dialog: int,
) -> dict:
    return {
        "dialogs_attempted": 0,
        "dialogs_skipped": 0,
        "messages_seen": 0,
        "messages_persisted": 0,
        "duplicates_skipped": 0,
        "deadline_ms": deadline_ms,
        "concurrency": concurrency,
        "coverage_bounds": {
            "max_dialogs": max_dialogs,
            "max_per_dialog": max_per_dialog,
            "deadline_ms": deadline_ms,
        },
        "fragment_status_counts": {},
    }


def _trace_increment_status(result: dict, status: str) -> None:
    counts = result.setdefault("fragment_status_counts", {})
    counts[status] = counts.get(status, 0) + 1


def _build_trace_account_messages_query(
    request: _TraceMessageQueryRequest,
) -> tuple[str, dict]:
    """Build the baseline Account Trace query over canonical message rows."""
    params: dict[str, object] = {
        "target_user_id": request.target_user_id,
        "self_id": request.self_id,
        "limit": request.limit,
    }
    sql = (
        "SELECT "
        "m.dialog_id, "
        "m.message_id, "
        "m.sent_at, "
        "m.text, "
        "m.sender_id, "
        "m.media_description, "
        "m.forum_topic_id AS topic_id, "
        "COALESCE(d.name, e_dialog.name, CAST(m.dialog_id AS TEXT)) AS dialog_title, "
        "COALESCE(d.type, e_dialog.type) AS dialog_type, "
        "tm.title AS topic_title, "
        "m.post_author AS author_signature, "
        f"{EFFECTIVE_SENDER_ID_SQL}, "
        "CASE "
        f"WHEN {_EFFECTIVE_SENDER_ID_EXPR} = :target_user_id THEN 'effective_sender_id' "
        "ELSE 'post_author_signature' "
        "END AS authorship_basis "
        "FROM messages m "
        "LEFT JOIN dialogs d ON d.dialog_id = m.dialog_id "
        "LEFT JOIN entities e_dialog ON e_dialog.id = m.dialog_id "
        "LEFT JOIN topic_metadata tm "
        "  ON tm.dialog_id = m.dialog_id AND tm.topic_id = m.forum_topic_id "
        "WHERE m.is_deleted = 0 AND m.is_service = 0"
    )

    authorship_predicates = [f"{_EFFECTIVE_SENDER_ID_EXPR} = :target_user_id"]
    aliases = request.post_author_aliases or []
    if aliases:
        placeholders: list[str] = []
        for idx, alias in enumerate(aliases):
            param_name = f"post_author_alias_{idx}"
            placeholders.append(f":{param_name}")
            params[param_name] = alias
        authorship_predicates.append(f"m.post_author IN ({', '.join(placeholders)})")
    sql += f" AND ({' OR '.join(authorship_predicates)})"

    if request.scope_dialog_ids:
        scope_placeholders = [f":scope_{i}" for i in range(len(request.scope_dialog_ids))]
        sql += f" AND m.dialog_id IN ({', '.join(scope_placeholders)})"
        for i, sid in enumerate(request.scope_dialog_ids):
            params[f"scope_{i}"] = sid
    elif request.exact_dialog_id is not None:
        sql += " AND m.dialog_id = :exact_dialog_id"
        params["exact_dialog_id"] = request.exact_dialog_id

    if request.exact_topic_id is not None:
        sql += " AND m.forum_topic_id = :exact_topic_id"
        params["exact_topic_id"] = request.exact_topic_id

    if request.sent_after_ts is not None:
        sql += " AND m.sent_at >= :sent_after"
        params["sent_after"] = request.sent_after_ts

    if request.sent_before_ts is not None:
        sql += " AND m.sent_at <= :sent_before"
        params["sent_before"] = request.sent_before_ts

    if request.navigation is not None:
        sql += (
            " AND ("
            "m.sent_at < :nav_sent_at "
            "OR (m.sent_at = :nav_sent_at AND m.dialog_id < :nav_dialog_id) "
            "OR (m.sent_at = :nav_sent_at AND m.dialog_id = :nav_dialog_id "
            "AND m.message_id < :nav_message_id)"
            ")"
        )
        params["nav_sent_at"] = request.navigation["sent_at"]
        params["nav_dialog_id"] = request.navigation["dialog_id"]
        params["nav_message_id"] = request.navigation["message_id"]

    sql += " ORDER BY m.sent_at DESC, m.dialog_id DESC, m.message_id DESC LIMIT :limit"
    return sql, params


def _unique_trace_aliases(*values: object) -> list[str]:
    """Build a stable de-duplicated non-empty alias list for post_author matching."""
    aliases: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        alias = value.strip()
        if not alias:
            continue
        for candidate in (alias, alias.removeprefix("@")):
            if candidate and candidate not in seen:
                seen.add(candidate)
                aliases.append(candidate)
    return aliases


# Keep these SQL aliases in this module to avoid an import cycle.
_EFFECTIVE_SENDER_ID_EXPR = (
    "COALESCE("
    "m.sender_id, "
    "CASE "
    "WHEN m.is_service = 1 THEN NULL "
    "WHEN m.dialog_id > 0 AND m.out = 1 THEN :self_id "
    "WHEN m.dialog_id > 0 AND m.out = 0 THEN m.dialog_id "
    "ELSE NULL "
    "END"
    ")"
)
EFFECTIVE_SENDER_ID_SQL = _EFFECTIVE_SENDER_ID_EXPR + " AS effective_sender_id"
_SENDER_FIRST_NAME_SQL = "COALESCE(e_raw.name, e_eff.name, m.sender_first_name) AS sender_first_name"
_SENDER_ENTITY_JOINS_SQL = (
    "LEFT JOIN entities e_raw ON e_raw.id = m.sender_id "
    f"LEFT JOIN entities e_eff ON e_eff.id = {_EFFECTIVE_SENDER_ID_EXPR} "
)
