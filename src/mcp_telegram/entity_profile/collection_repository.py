"""Atomic retained collection facts and generation-scoped membership."""

# pyright: reportAny=false

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from typing import cast

from ..sync_transactions import require_write_transaction
from .contracts import (
    ObservationBoundary,
    ProfileCollectionItem,
    ProfileCollectionPage,
    ProfileFieldCoverage,
    ProfileFieldObservation,
    ProfileFieldState,
    ProfileSemanticSnapshot,
)


def encode(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


@dataclass(frozen=True, slots=True)
class EffectiveProfileMerge:
    before: Mapping[str, object]
    after: Mapping[str, object]
    accepted_paths: frozenset[str]
    provenance: Mapping[str, object]
    snapshot: ProfileSemanticSnapshot
    order: int
    previous_provenance: Mapping[str, object]
    previous_coverage: Mapping[str, object]


@dataclass(slots=True)
class _MergeAuthority:
    previous: Mapping[str, object]
    rejected: set[str] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class CollectionProgress:
    payload: dict[str, object]
    status: str
    reason: str | None
    cursor: dict[str, object] | None
    retry: bool = False


def collection_progress(
    page: ProfileCollectionPage, state: dict[str, object], summary: dict[str, object], previous: object
) -> CollectionProgress:
    first_count = state.get("first_count", page.reported_count)
    summary["changed_during_scan"] = bool(state.get("changed_count")) or first_count != page.reported_count
    summary["diagnostics"] = tuple(cast(list[str], state.get("diagnostics", []))) + page.diagnostics
    seen = cast(list[str], state.get("seen", []))
    request_key = encode(state.get("request", {}))
    stalled = page.next_cursor is not None and encode(page.next_cursor) in [*seen, request_key]
    complete = _collection_complete(page, summary, stalled)
    baseline = _published_baseline(state, previous)
    _page_summary(page, state, summary, complete, baseline)
    if complete or page.reason:
        return CollectionProgress(summary, "fresh" if complete else "unavailable", page.reason, None)
    if stalled or summary["diagnostics"] or page.terminal:
        return CollectionProgress(
            summary,
            "pending",
            _retry_reason(stalled, bool(summary["changed_during_scan"])),
            {"scan": int(cast(int, state.get("scan", 0))) + 1, "baseline": baseline},
            True,
        )
    next_state = {
        "request": dict(page.next_cursor or {}),
        "first_count": first_count,
        "changed_count": summary["changed_during_scan"],
        "started_at": summary["observation_started_at"],
        "diagnostics": summary["diagnostics"],
        "scan": state.get("scan", 0),
        "counts": summary["reported_counts"],
        "seen": [*seen, request_key],
        "baseline": baseline,
        "metadata": summary["metadata"],
        "metadata_provenance": summary["metadata_provenance"],
    }
    return CollectionProgress(summary, "pending", None, next_state)


def _collection_complete(page: ProfileCollectionPage, summary: Mapping[str, object], stalled: bool) -> bool:
    count_matches = (
        page.section == "saved_gifts" or page.reported_count is None or page.reported_count == summary["retained_count"]
    )
    return bool(
        page.terminal
        and summary["details_complete"]
        and count_matches
        and not summary["diagnostics"]
        and not summary["changed_during_scan"]
        and not stalled
        and not page.reason
    )


def _published_baseline(state: Mapping[str, object], previous: object) -> object:
    if "baseline" in state:
        return state["baseline"]
    if isinstance(previous, dict):
        return previous.get("published")
    return None


def _comparable_membership(complete: bool, summary: Mapping[str, object], baseline: object) -> bool:
    return bool(
        complete
        and summary["identifiable"]
        and isinstance(baseline, dict)
        and baseline.get("identifiable")
        and baseline.get("owner_account_id") == summary["owner_account_id"]
    )


def _identity(value: object) -> tuple[object, object] | None:
    if isinstance(value, dict):
        identifier = value.get("id", value.get("photo_id"))
        if identifier is not None:
            return value.get("constructor"), identifier
    return None


def merge_semantic_fields(
    old: object,
    new: object,
    coverage: Mapping[str, ProfileFieldCoverage],
    path: str = "",
    authority: _MergeAuthority | None = None,
) -> object:
    """Omissions retain facts; a changed constructor/ID never inherits old children."""
    evidence = coverage.get(path)
    if authority is not None and _reject_untrusted_value(old, new, coverage, path, authority.previous):
        authority.rejected.add(path)
        return old
    if _retains_previous_value(evidence):
        return old if old is not None else new
    if evidence is not None and evidence.state is ProfileFieldState.CLEARED and not evidence.authoritative:
        return old
    if isinstance(new, dict):
        return _merge_semantic_node(old, new, coverage, path, authority)
    if isinstance(new, list):
        return _merge_semantic_vector(old, new, coverage, path, authority)
    return new


def _retains_previous_value(evidence: ProfileFieldCoverage | None) -> bool:
    return evidence is not None and evidence.state in {ProfileFieldState.UNKNOWN, ProfileFieldState.API_NOT_RETURNED}


def store_semantic_snapshot(
    conn: sqlite3.Connection, snapshot: ProfileSemanticSnapshot, order: int
) -> EffectiveProfileMerge | None:
    require_write_transaction(conn)
    owner = snapshot.owner_account_id
    if owner is None:
        raise ValueError("semantic snapshot requires captured account")
    snapshot.require_account(owner)
    row = conn.execute(
        "SELECT fields_json,observation_order,provenance_json,coverage_json,complete FROM entity_profile_semantic_snapshots "
        "WHERE owner_account_id=? AND entity_id=?",
        (owner, snapshot.target_id),
    ).fetchone()
    if row is not None and int(row[1]) >= order:
        return None
    previous = json.loads(row[0]) if row else {}
    previous_provenance = json.loads(row[2]) if row else {}
    previous_paths = previous_provenance.get("fields", {})
    previous_coverage = json.loads(row[3]) if row else {}
    fields, accepted, paths, rejected = _merge_snapshot(
        conn, snapshot, (previous, previous_paths, previous_coverage), order
    )
    provenance = {
        "fields": paths,
        "endpoint": snapshot.endpoint,
        "layer": snapshot.schema_layer,
        "diagnostics": snapshot.diagnostics,
        "acquisition_coverage": {key: asdict(value) for key, value in snapshot.coverage.items()},
    }
    effective_coverage = _snapshot_coverage(snapshot, paths, order, previous_coverage, (previous, fields, rejected))
    complete = _effective_complete(snapshot, effective_coverage, bool(row and row[4]))
    conn.execute(
        "INSERT INTO entity_profile_semantic_snapshots(owner_account_id,entity_id,normalization_version,"
        "fields_json,coverage_json,provenance_json,complete,observation_order,observation_started_at,"
        "observation_completed_at,source) VALUES(?,?,?,?,?,?,?,?,?,?,'rpc') "
        "ON CONFLICT(owner_account_id,entity_id) DO UPDATE SET normalization_version=excluded.normalization_version,"
        "fields_json=excluded.fields_json,coverage_json=excluded.coverage_json,provenance_json=excluded.provenance_json,"
        "complete=excluded.complete,observation_order=excluded.observation_order,"
        "observation_started_at=excluded.observation_started_at,observation_completed_at=excluded.observation_completed_at,"
        "source=excluded.source",
        (
            owner,
            snapshot.target_id,
            snapshot.normalization_version,
            encode(fields),
            encode(effective_coverage),
            encode(provenance),
            int(complete),
            order,
            snapshot.observation.started_at,
            snapshot.observation.completed_at,
        ),
    )
    return EffectiveProfileMerge(
        previous, fields, accepted, provenance, snapshot, order, previous_provenance, previous_coverage
    )


def store_semantic_patch(
    conn: sqlite3.Connection, snapshot: ProfileSemanticSnapshot, observation: ProfileFieldObservation
) -> EffectiveProfileMerge:
    """Merge supplied paths without renewing a FullUser acquisition or coverage."""
    require_write_transaction(conn)
    snapshot.require_account(observation.owner_account_id)
    row = conn.execute(
        "SELECT fields_json,provenance_json,coverage_json FROM entity_profile_semantic_snapshots "
        "WHERE owner_account_id=? AND entity_id=?",
        (observation.owner_account_id, snapshot.target_id),
    ).fetchone()
    previous, provenance, old_coverage = (json.loads(value) for value in row) if row else ({}, {}, {})
    paths = provenance.get("fields", {})
    coverage = _ordered_item_coverage(snapshot.coverage, paths, observation.order)
    incoming = replace(snapshot, coverage=coverage)
    fields, accepted, effective_paths, _rejected = _merge_snapshot(
        conn, incoming, (previous, paths, old_coverage), observation.order
    )
    effective_coverage = dict(old_coverage)
    for path in accepted:
        effective_paths[path] = {
            "order": observation.order,
            "observed_at": observation.observed_at,
            "source": observation.source,
            "observation_started_at": snapshot.observation.started_at,
            "observation_completed_at": snapshot.observation.completed_at,
        }
        effective_coverage[path] = asdict(coverage[path])
    new_provenance = {
        **provenance,
        "fields": effective_paths,
        "supplied_observation": {
            "order": observation.order,
            "observed_at": observation.observed_at,
            "source": observation.source,
            "observation_started_at": snapshot.observation.started_at,
            "observation_completed_at": snapshot.observation.completed_at,
            "coverage": {path: asdict(value) for path, value in snapshot.coverage.items()},
        },
    }
    unmapped = any(
        value.state is ProfileFieldState.UNKNOWN and value.reason != "partial_value_unobserved"
        for value in snapshot.coverage.values()
    )
    conn.execute(
        "INSERT INTO entity_profile_semantic_snapshots(owner_account_id,entity_id,normalization_version,"
        "fields_json,coverage_json,provenance_json,complete,observation_order,source) VALUES(?,?,?,?,?,?,0,0,?) "
        "ON CONFLICT(owner_account_id,entity_id) DO UPDATE SET fields_json=excluded.fields_json,"
        "coverage_json=excluded.coverage_json,provenance_json=excluded.provenance_json,"
        "complete=CASE WHEN ? THEN 0 ELSE entity_profile_semantic_snapshots.complete END",
        (
            observation.owner_account_id,
            snapshot.target_id,
            snapshot.normalization_version,
            encode(fields),
            encode(effective_coverage),
            encode(new_provenance),
            observation.source,
            int(unmapped),
        ),
    )
    return EffectiveProfileMerge(
        previous, fields, accepted, new_provenance, incoming, observation.order, provenance, old_coverage
    )


def _merge_snapshot(
    conn: sqlite3.Connection,
    snapshot: ProfileSemanticSnapshot,
    prior: tuple[dict[str, object], dict[str, object], dict[str, object]],
    order: int,
) -> tuple[dict[str, object], frozenset[str], dict[str, object], set[str]]:
    previous, previous_paths, previous_coverage = prior
    authority = _MergeAuthority(previous_coverage)
    fences = {
        row[0]: row[1:]
        for row in conn.execute(
            "SELECT field,observation_order,observed_at,source FROM entity_profile_field_observations WHERE entity_id=? AND owner_account_id=?",
            (snapshot.target_id, snapshot.owner_account_id),
        )
    }
    row = conn.execute(
        "SELECT detail_json FROM entity_details WHERE entity_id=? AND profile_owner_account_id=?",
        (snapshot.target_id, snapshot.owner_account_id),
    ).fetchone()
    canonical = json.loads(row[0]) if row else {}
    blocked = _newer_aliases(snapshot.fields, fences, order)
    fields, paths = dict(previous), dict(previous_paths)
    retained_paths = dict(previous_paths)
    accepted: set[str] = set()
    for root, value in snapshot.fields.items():
        if root in blocked:
            _adopt_canonical_alias(fields, paths, (root, blocked[root]), canonical=canonical, fences=fences)
        else:
            fields[root] = merge_semantic_fields(previous.get(root), value, snapshot.coverage, root, authority)
            _retain_path_provenance(paths, previous.get(root), fields[root], root)
            _retain_path_provenance(retained_paths, previous.get(root), fields[root], root)
            accepted.update(_record_path_observation(snapshot.coverage, root, paths, snapshot.observation, order))
    _restore_rejected_observations(paths, retained_paths, accepted, authority.rejected)
    return fields, frozenset(accepted), paths, authority.rejected


def _retain_path_provenance(paths: dict[str, object], old: object, new: object, root: str) -> None:
    if isinstance(new, dict) and isinstance(old, dict) and _same_node(old, new):
        for key, value in new.items():
            _retain_path_provenance(paths, old.get(key), value, f"{root}.{key}")
    elif isinstance(new, list) and isinstance(old, list):
        _reindex_vector_provenance(paths, old, new, root)
    elif old != new:
        _drop_path_provenance(paths, root)


def _drop_path_provenance(paths: dict[str, object], root: str) -> None:
    for key in tuple(paths):
        if key == root or key.startswith((root + ".", root + "[")):
            del paths[key]


def store_collection_page(  # noqa: PLR0913 - one captured page transaction
    conn: sqlite3.Connection,
    page: ProfileCollectionPage,
    *,
    owner: int,
    generation: int,
    cursor: Mapping[str, object],
    order: int,
) -> dict[str, object]:
    require_write_transaction(conn)
    scan = int(cast(int, cursor.get("scan", 0)))
    request = encode(cursor.get("request", {}))
    page_key = hashlib.sha256(request.encode()).hexdigest()
    for index, item in enumerate(page.items):
        key = item.key if item.identity_kind == "remote" else f"observation:{generation}:{scan}:{page_key}:{index}"
        _store_item(conn, page, owner=owner, key=key, fields=item.fields, coverage=item.coverage, order=order)
        conn.execute(
            "INSERT OR IGNORE INTO entity_profile_collection_membership "
            "(owner_account_id,entity_id,section,generation,scan,item_key,identity_kind,page_key,ordinal,detail_complete) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                owner,
                page.entity_id,
                page.section,
                generation,
                scan,
                key,
                item.identity_kind,
                page_key,
                index,
                int(item.detail_complete),
            ),
        )
        if item.detail_complete:
            conn.execute(
                "UPDATE entity_profile_collection_membership SET detail_complete=1 WHERE owner_account_id=? "
                "AND entity_id=? AND section=? AND generation=? AND scan=? AND item_key=?",
                (owner, page.entity_id, page.section, generation, scan, key),
            )
    return _collection_summary(conn, page, owner, generation, scan)


def store_story_update(
    conn: sqlite3.Connection, entity_id: int, item: ProfileCollectionItem, observation: ProfileFieldObservation
) -> None:
    """Persist supplied story/tombstone facts without guessing catalog membership."""
    require_write_transaction(conn)
    for section in ("current_stories", "pinned_stories"):
        page = ProfileCollectionPage(
            entity_id, section, (), ObservationBoundary(observation.observed_at, observation.observed_at)
        )
        _store_item(
            conn,
            page,
            owner=observation.owner_account_id,
            key=item.key,
            fields=item.fields,
            coverage=item.coverage,
            order=observation.order,
            source=observation.source,
        )


def _store_item(  # noqa: PLR0913 - persisted composite identity
    conn: sqlite3.Connection,
    page: ProfileCollectionPage,
    *,
    owner: int,
    key: str,
    fields: Mapping[str, object],
    coverage: Mapping[str, ProfileFieldCoverage],
    order: int,
    source: str = "rpc",
) -> None:
    identity = (owner, page.entity_id, page.section, key)
    old = conn.execute(
        "SELECT fields_json,observation_order,provenance_json,coverage_json FROM entity_profile_collection_facts "
        "WHERE owner_account_id=? AND entity_id=? AND section=? AND item_key=?",
        identity,
    ).fetchone()
    prior_paths = json.loads(old[2]) if old else {}
    if old and old[1] >= order and not _has_later_delivered_leaf(prior_paths, order):
        return
    coverage = _ordered_item_coverage(coverage, prior_paths, order)
    previous_fields = json.loads(old[0]) if old else {}
    previous_coverage = json.loads(old[3]) if old else {}
    authority = _MergeAuthority(previous_coverage)
    merged = merge_semantic_fields(previous_fields, dict(fields), coverage, "item", authority)
    provenance = dict(prior_paths)
    _retain_path_provenance(provenance, previous_fields, merged, "item")
    retained_provenance = dict(provenance)
    accepted = _record_path_observation(coverage, "item", provenance, page.observation, order)
    if source != "rpc":
        provenance.update(
            {path: {"order": order, "observed_at": page.observation.started_at, "source": source} for path in accepted}
        )
    _restore_rejected_observations(provenance, retained_provenance, accepted, authority.rejected)
    effective_coverage = _item_coverage(coverage, previous_coverage, (previous_fields, merged), authority.rejected)
    conn.execute(
        "INSERT INTO entity_profile_collection_facts(owner_account_id,entity_id,section,item_key,fields_json,"
        "coverage_json,provenance_json,observation_order,observed_at) VALUES(?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(owner_account_id,entity_id,section,item_key) DO UPDATE SET fields_json=excluded.fields_json,"
        "coverage_json=excluded.coverage_json,provenance_json=excluded.provenance_json,observation_order=excluded.observation_order,observed_at=excluded.observed_at",
        (
            *identity,
            encode(merged),
            encode(effective_coverage),
            encode(provenance),
            max(order, old[1]) if old else order,
            page.observation.completed_at,
        ),
    )


def _has_later_delivered_leaf(paths: Mapping[str, object], order: int) -> bool:
    return any(
        isinstance(value, dict) and value.get("source") == "update" and value.get("order", 0) > order
        for value in paths.values()
    )


def _ordered_item_coverage(
    coverage: Mapping[str, ProfileFieldCoverage], previous: Mapping[str, object], order: int
) -> dict[str, ProfileFieldCoverage]:
    blocked = {path for path, old in previous.items() if isinstance(old, dict) and old.get("order", 0) >= order}
    return {
        path: ProfileFieldCoverage(value.classification, ProfileFieldState.UNKNOWN)
        if any(_path_under(path, old) for old in blocked)
        else value
        for path, value in coverage.items()
    }


def _collection_summary(
    conn: sqlite3.Connection, page: ProfileCollectionPage, owner: int, generation: int, scan: int
) -> dict[str, object]:
    rows = conn.execute(
        "SELECT m.item_key,m.identity_kind,m.detail_complete,f.fields_json FROM entity_profile_collection_membership m "
        "JOIN entity_profile_collection_facts f USING(owner_account_id,entity_id,section,item_key) "
        "WHERE m.owner_account_id=? AND m.entity_id=? AND m.section=? AND m.generation=? AND m.scan=? ORDER BY m.item_key",
        (owner, page.entity_id, page.section, generation, scan),
    ).fetchall()
    digest = hashlib.sha256(encode([(r[0], r[3]) for r in rows]).encode()).hexdigest()
    return {
        "owner_account_id": owner,
        "reported_count": page.reported_count,
        "retained_count": len(rows),
        "retained_unique_count": sum(r[1] == "remote" for r in rows),
        "details_complete": all(r[2] for r in rows),
        "membership_digest": hashlib.sha256(encode([r[0] for r in rows]).encode()).hexdigest(),
        "content_digest": digest,
        "identifiable": all(r[1] == "remote" for r in rows),
        "generation": generation,
        "scan": scan,
    }


def _same_node(old: Mapping[str, object], new: Mapping[str, object]) -> bool:
    return old.get("constructor") == new.get("constructor") and _identity(old) == _identity(new)


def _merge_semantic_node(
    old: object,
    new: dict[str, object],
    coverage: Mapping[str, ProfileFieldCoverage],
    path: str,
    authority: _MergeAuthority | None,
) -> dict[str, object]:
    previous = old if isinstance(old, dict) and _same_node(old, new) else {}
    if not previous and authority is not None:
        previous_coverage = dict(authority.previous)
        _drop_path_provenance(previous_coverage, path)
        authority = _MergeAuthority(previous_coverage, authority.rejected)
    result = dict(previous)
    for key, value in new.items():
        child_path = f"{path}.{key}" if path else key
        result[key] = merge_semantic_fields(previous.get(key), value, coverage, child_path, authority)
    return result


def _merge_semantic_vector(
    old: object,
    new: list[object],
    coverage: Mapping[str, ProfileFieldCoverage],
    path: str,
    authority: _MergeAuthority | None,
) -> list[object]:
    previous = old if isinstance(old, list) else []
    new = _retained_vector_members(previous, new, coverage.get(path))
    by_identity = {_identity(item): item for item in previous if _identity(item) is not None}
    if authority is not None:
        previous_coverage = dict(authority.previous)
        _reindex_vector_provenance(previous_coverage, previous, new, path)
        authority = _MergeAuthority(previous_coverage, authority.rejected)
    return [
        merge_semantic_fields(by_identity.get(_identity(item)), item, coverage, f"{path}[{index}]", authority)
        for index, item in enumerate(new)
    ]


def _reindex_vector_provenance(paths: dict[str, object], old: list[object], new: list[object], root: str) -> None:
    original = dict(paths)
    _drop_path_provenance(paths, root)
    positions = {_identity(value): index for index, value in enumerate(old) if _identity(value) is not None}
    for index, value in enumerate(new):
        old_index = positions.get(_identity(value))
        if old_index is not None:
            old_prefix, new_prefix = f"{root}[{old_index}]", f"{root}[{index}]"
            paths.update(
                {
                    new_prefix + key[len(old_prefix) :]: evidence
                    for key, evidence in original.items()
                    if key.startswith((old_prefix + ".", old_prefix + "[")) or key == old_prefix
                }
            )


def _newer_aliases(
    fields: Mapping[str, object], fences: Mapping[str, tuple[int, int, str]], order: int
) -> dict[str, str]:
    blocked: dict[str, str] = {}
    for path in fields:
        if path == "full_user.profile_photo":
            continue
        alias = {
            "user.photo": "current_photo",
            "user.usernames": "extra_usernames",
            "user.emoji_status": "emoji_status_id",
        }.get(path, path.split(".", 1)[-1])
        if alias in fences and fences[alias][0] > order:
            blocked[path] = alias
    return blocked


def _adopt_canonical_alias(
    fields: dict[str, object],
    paths: dict[str, object],
    binding: tuple[str, str],
    *,
    canonical: Mapping[str, object],
    fences: Mapping[str, tuple[int, int, str]],
) -> None:
    root, alias = binding
    existing = paths.get(root)
    if root in fields and _exact_newer_path(existing, fences[alias][0]):
        return
    replacement = canonical.get(alias)
    if alias in {"extra_usernames", "emoji_status_id"} or (
        isinstance(replacement, (dict, list)) and alias != "current_photo"
    ):
        _retain_unbridged_alias(paths, root, existing, fences[alias])
        return
    fields.pop(root, None)
    _drop_path_provenance(paths, root)
    if alias in canonical:
        replacement = canonical[alias]
        if alias == "current_photo" and isinstance(replacement, dict):
            replacement = {"constructor": "UserProfilePhoto", "photo_id": replacement["photo_id"]}
        fields[root] = replacement
    field_order, observed_at, source = fences[alias]
    paths[root] = {"order": field_order, "observed_at": observed_at, "source": source, "reason": "legacy_alias_bridge"}


def _retain_unbridged_alias(paths: dict[str, object], root: str, existing: object, fence: tuple[int, int, str]) -> None:
    order, observed_at, source = fence
    paths[root] = {
        **(existing if isinstance(existing, dict) else {}),
        "reason": "legacy_alias_bridge",
        "invalidated_by": {"order": order, "observed_at": observed_at, "source": source},
    }


def _record_path_observation(
    coverage: Mapping[str, ProfileFieldCoverage],
    root: str,
    paths: dict[str, object],
    observation: ObservationBoundary,
    order: int,
) -> set[str]:
    accepted: set[str] = set()
    for path, evidence in coverage.items():
        if _path_under(path, root) and _effective_value(evidence):
            accepted.add(path)
            paths[path] = {
                "order": order,
                "source": "rpc",
                "observed_at": observation.started_at,
                "observation_completed_at": observation.completed_at,
            }
    return accepted


def _path_under(path: str, root: str) -> bool:
    return path == root or path.startswith((root + ".", root + "["))


def _effective_value(evidence: ProfileFieldCoverage) -> bool:
    return evidence.state is ProfileFieldState.PRESENT or (
        evidence.state is ProfileFieldState.CLEARED and evidence.authoritative
    )


def _page_summary(
    page: ProfileCollectionPage,
    state: Mapping[str, object],
    summary: dict[str, object],
    complete: bool,
    baseline: object,
) -> None:
    summary.update(
        {
            "traversal_complete": page.terminal,
            "complete": complete,
            "reported_counts": [
                *cast(list[object], state.get("counts", [])),
                {
                    "count": page.reported_count,
                    "started_at": page.observation.started_at,
                    "completed_at": page.observation.completed_at,
                },
            ],
            "observation_started_at": state.get("started_at", page.observation.started_at),
            "observation_completed_at": page.observation.completed_at,
            "comparable_membership": _comparable_membership(complete, summary, baseline),
            "previous_published": baseline,
            "count_discrepancy": page.reported_count is not None and page.reported_count != summary["retained_count"],
        }
    )
    summary["published"] = (
        {
            key: summary[key]
            for key in ("owner_account_id", "generation", "scan", "identifiable", "membership_digest", "content_digest")
        }
        if complete
        else baseline
    )
    summary["metadata"] = {**cast(dict[str, object], state.get("metadata", {})), **page.metadata}
    provenance = dict(cast(dict[str, object], state.get("metadata_provenance", {})))
    for key in page.metadata:
        provenance[key] = {"started_at": page.observation.started_at, "completed_at": page.observation.completed_at}
    summary["metadata_provenance"] = provenance
    summary["supplied_target_user"] = asdict(page.supplied_user) if page.supplied_user else None
    summary["sidecar_identities"] = [_identity_receipt(item) for item in page.sidecar_identities]


def _identity_receipt(observation: object) -> dict[str, object]:
    result: dict[str, object] = {}
    for name in ("dialog_id", "name", "username", "dialog_type", "complete", "source", "observed_at"):
        value = getattr(observation, name)
        if value is None or isinstance(value, (str, int, bool)):
            result[name] = value
    return result


def _retry_reason(stalled: bool, changed: bool) -> str:
    if stalled:
        return "cursor_stalled"
    return "changed_during_scan" if changed else "collection_incomplete"


def _snapshot_coverage(
    snapshot: ProfileSemanticSnapshot,
    paths: Mapping[str, object],
    order: int,
    previous: Mapping[str, object],
    values: tuple[Mapping[str, object], Mapping[str, object], set[str]],
) -> dict[str, object]:
    coverage: dict[str, object] = {
        key: value for key, value in previous.items() if isinstance(value, dict) and "state" in value
    }
    for root in snapshot.fields:
        _retain_path_provenance(coverage, values[0].get(root), values[1].get(root), root)
    retained_coverage = dict(coverage)
    coverage.update({key: asdict(value) for key, value in snapshot.coverage.items()})
    _restore_rejected_coverage(coverage, retained_coverage, values[2])
    for root in snapshot.fields:
        evidence = paths.get(root)
        if isinstance(evidence, dict) and (
            evidence.get("order", 0) > order or evidence.get("reason") == "legacy_alias_bridge"
        ):
            _restore_newer_coverage(coverage, root, evidence, previous)
    return coverage


def _restore_rejected_coverage(coverage: dict[str, object], retained: Mapping[str, object], rejected: set[str]) -> None:
    for path in rejected:
        _drop_path_provenance(coverage, path)
        coverage.update({key: value for key, value in retained.items() if _path_under(key, path)})


def _restore_newer_coverage(
    coverage: dict[str, object], root: str, evidence: Mapping[str, object], previous: Mapping[str, object]
) -> None:
    _drop_path_provenance(coverage, root)
    if evidence.get("reason") == "legacy_alias_bridge":
        coverage[root] = {
            "classification": "profile",
            "state": "unknown",
            "authoritative": False,
            "reason": "newer_legacy_update_not_semantically_normalized",
        }
    else:
        coverage.update({path: value for path, value in previous.items() if _path_under(path, root)})


def _effective_complete(
    snapshot: ProfileSemanticSnapshot, coverage: Mapping[str, object], previous_complete: bool
) -> bool:
    full_observation = snapshot.complete and any(path.startswith("full_user.") for path in snapshot.coverage)
    return bool(
        (full_observation or previous_complete)
        and coverage
        and all(_known_coverage(value) for value in coverage.values())
        and not any(value.state is ProfileFieldState.UNKNOWN for value in snapshot.coverage.values())
    )


def _known_coverage(value: object) -> bool:
    return isinstance(value, dict) and value.get("state") not in {None, "unknown"}


def _item_coverage(
    incoming: Mapping[str, ProfileFieldCoverage],
    previous: Mapping[str, object],
    values: tuple[object, object],
    rejected: set[str],
) -> dict[str, object]:
    result = dict(previous)
    _retain_path_provenance(result, values[0], values[1], "item")
    retained_coverage = dict(result)
    result.update({key: asdict(value) for key, value in incoming.items()})
    _restore_rejected_coverage(result, retained_coverage, rejected)
    return result


def _reject_untrusted_value(
    old: object,
    new: object,
    incoming: Mapping[str, ProfileFieldCoverage],
    path: str,
    previous: Mapping[str, object],
) -> bool:
    evidence = incoming.get(path)
    if evidence is None or not _known_authority(previous.get(path)):
        return False
    if evidence.state in {ProfileFieldState.UNKNOWN, ProfileFieldState.API_NOT_RETURNED}:
        return True
    if evidence.authoritative:
        return False
    return _reject_container_value(old, new, incoming, path)


def _reject_container_value(old: object, new: object, incoming: Mapping[str, ProfileFieldCoverage], path: str) -> bool:
    if isinstance(new, dict) and isinstance(old, dict) and _same_node(old, new):
        return False
    if _identity(old) is not None:
        return not _identity_authority(new, incoming, path)
    return not _positive_child_authority(incoming, path)


def _known_authority(value: object) -> bool:
    return (
        isinstance(value, dict) and value.get("authoritative") is True and value.get("state") in {"present", "cleared"}
    )


def _retained_vector_members(
    old: list[object], new: list[object], evidence: ProfileFieldCoverage | None
) -> list[object]:
    if evidence is None or evidence.authoritative:
        return new
    identities = {_identity(item) for item in new if _identity(item) is not None}
    return [*new, *(item for item in old if _identity(item) is not None and _identity(item) not in identities)]


def _identity_authority(value: object, incoming: Mapping[str, ProfileFieldCoverage], path: str) -> bool:
    if isinstance(value, list):
        return any(_identity_authority(item, incoming, f"{path}[{index}]") for index, item in enumerate(value))
    if not isinstance(value, dict):
        return False
    key = "id" if "id" in value else "photo_id"
    evidence = incoming.get(f"{path}.{key}")
    return evidence is not None and evidence.authoritative


def _positive_child_authority(incoming: Mapping[str, ProfileFieldCoverage], path: str) -> bool:
    return any(
        evidence.authoritative and _effective_value(evidence)
        for child, evidence in incoming.items()
        if child != path and _path_under(child, path)
    )


def _restore_rejected_observations(
    paths: dict[str, object], previous: Mapping[str, object], accepted: set[str], rejected: set[str]
) -> None:
    for root in rejected:
        _drop_path_provenance(paths, root)
        paths.update({path: value for path, value in previous.items() if _path_under(path, root)})
        accepted.difference_update(path for path in tuple(accepted) if _path_under(path, root))


def _exact_newer_path(evidence: object, fence: int) -> bool:
    return (
        isinstance(evidence, dict)
        and evidence.get("order", 0) >= fence
        and evidence.get("reason") != "legacy_alias_bridge"
    )
