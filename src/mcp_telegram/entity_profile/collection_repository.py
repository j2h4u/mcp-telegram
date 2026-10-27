"""Atomic retained collection facts and generation-scoped membership."""

# pyright: reportAny=false

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from typing import Literal, cast

from ..sync_transactions import require_write_transaction
from .contracts import (
    PROFILE_NORMALIZATION_VERSION,
    ObservationBoundary,
    ProfileCollectionItem,
    ProfileCollectionPage,
    ProfileFieldClass,
    ProfileFieldCoverage,
    ProfileFieldObservation,
    ProfileFieldState,
    ProfileSemanticSnapshot,
    profile_value_paths,
)
from .field_manifest import PROFILE_FIELD_MANIFEST


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
    observations: Mapping[str, object] = field(default_factory=dict)
    order: int | None = None
    retained: set[str] = field(default_factory=set)


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
    legacy = _retained_legacy_evidence(state)
    summary.update(legacy)
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
            {
                "scan": int(cast(int, state.get("scan", 0))) + 1,
                "baseline": baseline,
                **legacy,
            },
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
        **legacy,
    }
    return CollectionProgress(summary, "pending", None, next_state)


def _retained_legacy_evidence(state: Mapping[str, object]) -> dict[str, object]:
    return {"legacy_evidence": state["legacy_evidence"]} if "legacy_evidence" in state else {}


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
            constructor = value.get("constructor")
            family = "story" if constructor in {"StoryItem", "StoryItemSkipped", "StoryItemDeleted"} else constructor
            return family, identifier
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
    if authority is not None and (
        _reject_stale_value(old, new, path, authority)
        or _reject_untrusted_value(old, new, coverage, path, authority.previous)
    ):
        authority.rejected.add(path)
        return old
    if _retains_previous_value(evidence):
        return old if old is not None else new
    if _unauthorized_clear(evidence):
        return old
    if isinstance(new, dict):
        return _merge_semantic_node(old, new, coverage, path, authority)
    if isinstance(new, list):
        return _merge_semantic_vector(old, new, coverage, path, authority)
    return new


def _unauthorized_clear(evidence: ProfileFieldCoverage | None) -> bool:
    return evidence is not None and evidence.state is ProfileFieldState.CLEARED and not evidence.authoritative


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
    fields, accepted, paths, authority = _merge_snapshot(
        conn, snapshot, (previous, previous_paths, previous_coverage), order
    )
    provenance = {
        "fields": paths,
        "endpoint": snapshot.endpoint,
        "layer": snapshot.schema_layer,
        "diagnostics": snapshot.diagnostics,
        "acquisition_coverage": {key: asdict(value) for key, value in snapshot.coverage.items()},
    }
    effective_coverage = _snapshot_coverage(snapshot, paths, order, previous_coverage, (previous, fields, authority))
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
    coverage = snapshot.coverage
    incoming = snapshot
    fields, accepted, effective_paths, _authority = _merge_snapshot(
        conn, incoming, (previous, paths, old_coverage), observation.order
    )
    effective_coverage = dict(old_coverage)
    for root in snapshot.fields:
        _retain_path_provenance(effective_coverage, previous.get(root), fields.get(root), root)
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
) -> tuple[dict[str, object], frozenset[str], dict[str, object], _MergeAuthority]:
    previous, previous_paths, previous_coverage = prior
    authority = _MergeAuthority(previous_coverage, observations=previous_paths, order=order)
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
    _restore_parent_observations(paths, retained_paths, accepted, authority.retained)
    return fields, frozenset(accepted), paths, authority


def _retain_path_provenance(paths: dict[str, object], old: object, new: object, root: str) -> None:
    if old == new:
        return
    if isinstance(new, dict) and isinstance(old, dict) and _same_node(old, new):
        for key, value in new.items():
            _retain_path_provenance(paths, old.get(key), value, f"{root}.{key}")
    elif isinstance(new, list) and isinstance(old, list):
        _reconcile_vector_provenance(paths, old, new, root)
    elif old != new:
        _drop_path_provenance(paths, root)


def _reconcile_vector_provenance(paths: dict[str, object], old: list[object], new: list[object], root: str) -> None:
    """Post-merge structural cleanup, after pre-merge identity/order association."""
    _reindex_vector_provenance(paths, old, new, root)
    previous = {_identity(item): item for item in old if _identity(item) is not None}
    for index, item in enumerate(new):
        _retain_path_provenance(paths, previous.get(_identity(item)), item, f"{root}[{index}]")


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


def reconcile_embedded_stories(
    conn: sqlite3.Connection,
    page: ProfileCollectionPage,
    observation: ProfileFieldObservation,
    merge: EffectiveProfileMerge | None = None,
) -> EffectiveProfileMerge | None:
    """Fan out effective facts only for confirmed IDs already in the embedded list."""
    require_write_transaction(conn)
    if page.section != "current_stories":
        return merge
    row = conn.execute(
        "SELECT fields_json,coverage_json,provenance_json FROM entity_profile_semantic_snapshots "
        "WHERE owner_account_id=? AND entity_id=?",
        (observation.owner_account_id, page.entity_id),
    ).fetchone()
    if row is None:
        return merge
    fields, coverage, provenance = (json.loads(value) for value in row)
    peer = fields.get("full_user.stories")
    stories = peer.get("stories") if isinstance(peer, dict) else None
    if not isinstance(stories, list):
        return merge
    paths = dict(provenance.get("fields", {}))
    confirmed = {item.key for item in page.items if item.identity_kind == "remote"}
    patched = _reconcile_story_nodes(
        conn, (observation.owner_account_id, page.entity_id), stories, confirmed, coverage=coverage, paths=paths
    )
    fields["full_user.stories"] = {**cast(dict[str, object], peer), "stories": patched}
    provenance["fields"] = paths
    conn.execute(
        "UPDATE entity_profile_semantic_snapshots SET fields_json=?,coverage_json=?,provenance_json=?,"
        "complete=CASE WHEN ? THEN complete ELSE 0 END WHERE owner_account_id=? AND entity_id=?",
        (
            encode(fields),
            encode(coverage),
            encode(provenance),
            int(all(_known_coverage(value) for value in coverage.values())),
            observation.owner_account_id,
            page.entity_id,
        ),
    )
    return _reconciled_merge(merge, fields, provenance, paths)


def _reconciled_merge(
    merge: EffectiveProfileMerge | None,
    fields: Mapping[str, object],
    provenance: Mapping[str, object],
    paths: Mapping[str, object],
) -> EffectiveProfileMerge | None:
    if merge is None:
        return None
    accepted = frozenset(
        path
        for path in merge.accepted_paths
        if isinstance((evidence := paths.get(path)), dict) and evidence.get("order", 0) <= merge.order
    )
    return replace(merge, after=fields, provenance=provenance, accepted_paths=accepted)


def _reconcile_story_nodes(  # noqa: PLR0913 - one account-scoped embedded-list fanout
    conn: sqlite3.Connection,
    owner_entity: tuple[int, int],
    stories: list[object],
    confirmed: set[str],
    *,
    coverage: dict[str, object],
    paths: dict[str, object],
) -> list[object]:
    patched = list(stories)
    for index, story in enumerate(stories):
        if not isinstance(story, dict) or str(story.get("id")) not in confirmed:
            continue
        key = str(story["id"])
        prefix = f"full_user.stories.stories[{index}]"
        effective = _copy_story_fact(conn, (*owner_entity, key), prefix, coverage, paths)
        if effective is not None:
            patched[index] = effective
    return patched


def _copy_story_fact(
    conn: sqlite3.Connection,
    identity: tuple[int, int, str],
    prefix: str,
    coverage: dict[str, object],
    paths: dict[str, object],
) -> dict[str, object] | None:
    row = conn.execute(
        "SELECT fields_json,coverage_json,provenance_json FROM entity_profile_collection_facts "
        "WHERE owner_account_id=? AND entity_id=? AND item_key=? AND section='current_stories'",
        identity,
    ).fetchone()
    if row is None:
        return None
    fields, item_coverage, observations = (json.loads(value) for value in row)
    if str(fields.get("id")) != identity[2] or fields.get("constructor") not in {
        "StoryItem",
        "StoryItemSkipped",
        "StoryItemDeleted",
    }:
        return None
    _drop_path_provenance(coverage, prefix)
    _drop_path_provenance(paths, prefix)
    coverage.update(
        {prefix + path[len("item") :]: value for path, value in item_coverage.items() if _path_under(path, "item")}
    )
    paths.update(
        {prefix + path[len("item") :]: value for path, value in observations.items() if _path_under(path, "item")}
    )
    return fields


def patch_embedded_story(
    conn: sqlite3.Connection,
    entity_id: int,
    item: ProfileCollectionItem,
    observation: ProfileFieldObservation,
) -> EffectiveProfileMerge | None:
    """Patch only matching retained story identities, never synthetic membership."""
    require_write_transaction(conn)
    row = conn.execute(
        "SELECT fields_json FROM entity_profile_semantic_snapshots WHERE owner_account_id=? AND entity_id=?",
        (observation.owner_account_id, entity_id),
    ).fetchone()
    fields = json.loads(row[0]) if row else {}
    root = "full_user.stories"
    peer_stories = fields.get(root)
    stories = peer_stories.get("stories") if isinstance(peer_stories, dict) else None
    if not isinstance(stories, list):
        return None
    patched, coverage = _embedded_story_patch(stories, item, root)
    if not coverage:
        return None
    snapshot = ProfileSemanticSnapshot(
        entity_id,
        observation.owner_account_id,
        {root: {**cast(dict[str, object], peer_stories), "stories": patched}},
        coverage,
        ObservationBoundary(),
        endpoint="delivered.Story",
    )
    return store_semantic_patch(conn, snapshot, observation)


def _embedded_story_patch(
    stories: list[object], item: ProfileCollectionItem, root: str
) -> tuple[list[object], dict[str, ProfileFieldCoverage]]:
    patched = list(stories)
    coverage: dict[str, ProfileFieldCoverage] = {}
    for index, old in enumerate(stories):
        if not isinstance(old, dict) or str(old.get("id")) != item.key:
            continue
        if old.get("constructor") not in {"StoryItem", "StoryItemSkipped", "StoryItemDeleted"}:
            continue
        prefix = f"{root}.stories[{index}]"
        patched[index] = (
            dict(item.fields) if item.fields.get("constructor") == "StoryItemDeleted" else {**old, **item.fields}
        )
        coverage.update({prefix + path[len("item") :]: value for path, value in item.coverage.items()})
    return patched, coverage


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
    previous_fields = json.loads(old[0]) if old else {}
    previous_coverage = json.loads(old[3]) if old else {}
    authority = _MergeAuthority(previous_coverage, observations=prior_paths, order=order)
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
    _restore_parent_observations(provenance, retained_provenance, accepted, authority.retained)
    effective_coverage = _item_coverage(
        coverage,
        previous_coverage,
        (previous_fields, merged),
        replace(authority, observations=provenance),
        _attributed_boundary(page.observation, source),
    )
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
    if old.get("constructor") == new.get("constructor"):
        return _identity(old) == _identity(new)
    old_id, new_id = old.get("id"), new.get("id")
    return (
        old.get("constructor") in {"StoryItem", "StoryItemSkipped"}
        and new.get("constructor") in {"StoryItem", "StoryItemSkipped"}
        and type(old_id) is int
        and old_id > 0
        and type(new_id) is int
        and new_id == old_id
    )


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
        previous_observations = dict(authority.observations)
        _drop_path_provenance(previous_observations, path)
        authority = replace(authority, previous=previous_coverage, observations=previous_observations)
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
        previous_observations = dict(authority.observations)
        _reindex_vector_provenance(previous_observations, previous, new, path)
        authority = replace(authority, previous=previous_coverage, observations=previous_observations)
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
    values: tuple[Mapping[str, object], Mapping[str, object], _MergeAuthority],
) -> dict[str, object]:
    coverage: dict[str, object] = {
        key: value for key, value in previous.items() if isinstance(value, dict) and "state" in value
    }
    for root in snapshot.fields:
        _retain_path_provenance(coverage, values[0].get(root), values[1].get(root), root)
    retained_coverage = dict(coverage)
    _accept_value_coverage(
        coverage,
        snapshot.coverage,
        profile_value_paths(values[1]),
        replace(values[2], observations=paths),
        _attributed_boundary(snapshot.observation),
    )
    _restore_rejected_coverage(coverage, retained_coverage, values[2].rejected)
    for root in snapshot.fields:
        evidence = paths.get(root)
        if isinstance(evidence, dict) and (evidence.get("reason") == "legacy_alias_bridge"):
            _restore_newer_coverage(coverage, root, evidence, previous)
    _restore_parent_coverage(coverage, retained_coverage, values[2].retained)
    return coverage


def _accept_value_coverage(
    retained: dict[str, object],
    incoming: Mapping[str, ProfileFieldCoverage],
    values: Mapping[str, object],
    authority: _MergeAuthority,
    attributed: bool,
) -> None:
    """Non-return preserves facts but may clarify a newer observed absence."""
    for path, evidence in incoming.items():
        if (
            evidence.state in {ProfileFieldState.UNKNOWN, ProfileFieldState.API_NOT_RETURNED}
            and path in retained
            and not (attributed and _refines_unknown_absence(path, retained[path], evidence, values, authority))
        ):
            continue
        retained[path] = asdict(evidence)


def _attributed_boundary(observation: ObservationBoundary, source: str = "rpc") -> bool:
    return source == "rpc" and observation.valid


def _refines_unknown_absence(
    path: str,
    previous: object,
    incoming: ProfileFieldCoverage,
    values: Mapping[str, object],
    authority: _MergeAuthority,
) -> bool:
    if incoming.state is not ProfileFieldState.API_NOT_RETURNED:
        return False
    if not isinstance(previous, dict) or previous.get("state") != "unknown" or path in values:
        return False
    if authority.order is None or any(_path_under(key, path) for key in authority.observations):
        return False
    return not any(
        _conflicting_ancestor(path, key, value, authority.order) for key, value in authority.observations.items()
    )


def _conflicting_ancestor(path: str, ancestor: str, value: object, order: int) -> bool:
    return _path_under(path, ancestor) and (
        not isinstance(value, dict) or type(value.get("order")) is not int or value["order"] > order
    )


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
    authority: _MergeAuthority,
    attributed: bool,
) -> dict[str, object]:
    result = dict(previous)
    _retain_path_provenance(result, values[0], values[1], "item")
    retained_coverage = dict(result)
    _accept_value_coverage(result, incoming, profile_value_paths({"item": values[1]}), authority, attributed)
    _restore_rejected_coverage(result, retained_coverage, authority.rejected)
    _restore_parent_coverage(result, retained_coverage, authority.retained)
    return result


def _reject_stale_value(old: object, new: object, path: str, authority: _MergeAuthority) -> bool:
    previous = authority.observations.get(path)
    if authority.order is None or not isinstance(previous, dict) or previous.get("order", 0) < authority.order:
        return False
    same_container = isinstance(old, dict) and isinstance(new, dict) and _same_node(old, new)
    if same_container or (isinstance(old, list) and isinstance(new, list)):
        authority.retained.add(path)
        return False
    authority.rejected.add(path)
    return True


def _restore_parent_observations(
    paths: dict[str, object], previous: Mapping[str, object], accepted: set[str], retained: set[str]
) -> None:
    for path in retained:
        if path in previous:
            paths[path] = previous[path]
        accepted.discard(path)


def _restore_parent_coverage(coverage: dict[str, object], previous: Mapping[str, object], retained: set[str]) -> None:
    for path in retained:
        if path in previous:
            coverage[path] = previous[path]


def _reject_untrusted_value(
    old: object,
    new: object,
    incoming: Mapping[str, ProfileFieldCoverage],
    path: str,
    previous: Mapping[str, object],
) -> bool:
    evidence = incoming.get(path)
    if not _known_authority(previous.get(path)):
        return False
    if evidence is None:
        return _reject_container_value(old, new, incoming, path)
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


_STORY_ABSENCE_FIELDS = {
    "StoryItem": frozenset(
        {
            "albums",
            "caption",
            "entities",
            "from_id",
            "fwd_from",
            "media_areas",
            "music",
            "privacy",
            "sent_reaction",
            "views",
        }
    ),
    "MessageMediaPhoto": frozenset({"ttl_seconds", "video"}),
    "Photo": frozenset({"video_sizes"}),
}
_STORY_CONSTRUCTORS = {"item": "StoryItem", "item.media": "MessageMediaPhoto", "item.media.photo": "Photo"}
_STORY_ANCHORS = {
    "item.id": int,
    "item.date": str,
    "item.pinned": bool,
    "item.media.photo.id": int,
    "item.media.photo.has_stickers": bool,
}


def _positive_bounds(start: object, end: object) -> tuple[float, float] | None:
    if not all(
        isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0
        for value in (start, end)
    ):
        return None
    if cast(float, end) < cast(float, start):
        return None
    return float(cast(float, start)), float(cast(float, end))


def _rpc_path_bounds(value: object, order: int) -> tuple[float, float] | None:
    if (
        not isinstance(value, dict)
        or value.get("source") != "rpc"
        or type(value.get("order")) is not int
        or value["order"] != order
    ):
        return None
    return _positive_bounds(
        value.get("observation_started_at", value.get("observed_at")), value.get("observation_completed_at")
    )


def _story_receipt(
    conn: sqlite3.Connection, owner: int, peer: int, section: Literal["current_stories", "pinned_stories"]
) -> tuple[str, dict[str, object]] | None:
    row = conn.execute(
        "SELECT s.payload_json,s.normalization_version,s.status,s.reason,d.profile_owner_account_id "
        "FROM entity_detail_sections s JOIN entity_details d ON d.entity_id=s.entity_id "
        "WHERE s.entity_id=? AND s.section=?",
        (peer, section),
    ).fetchone()
    if row is None or row[1:] != (PROFILE_NORMALIZATION_VERSION, "fresh", None, owner):
        return None
    payload = _coverage_json_mapping(row[0])
    if payload is None or not all(
        payload.get(key) is True for key in ("complete", "details_complete", "traversal_complete")
    ):
        return None
    published = payload.get("published")
    if (
        payload.get("diagnostics") != []
        or not isinstance(published, dict)
        or published.get("owner_account_id") != owner
    ):
        return None
    if not _published_story_identity(published, payload, owner):
        return None
    return str(row[0]), payload


def _published_story_identity(published: Mapping[str, object], payload: Mapping[str, object], owner: int) -> bool:
    if type(payload.get("owner_account_id")) is not int or payload["owner_account_id"] != owner:
        return False
    return all(
        type(published.get(key)) is int
        and cast(int, published[key]) >= 0
        and type(payload.get(key)) is int
        and payload[key] == published[key]
        for key in ("generation", "scan")
    )


def _coverage_json_mapping(encoded: object) -> dict[str, object] | None:
    if not isinstance(encoded, str):
        return None
    try:
        value = json.loads(encoded)
    except json.JSONDecodeError:
        return None
    return cast(dict[str, object], value) if isinstance(value, dict) else None


def _story_anchor(path: str, values: Mapping[str, object], coverage: Mapping[str, object]) -> bool:
    value, evidence = values.get(path), coverage.get(path)
    return bool(
        type(value) is _STORY_ANCHORS[path]
        and isinstance(evidence, dict)
        and evidence.get("state") == "present"
        and evidence.get("authoritative") is True
    )


def _complete_story_bounds(
    values: Mapping[str, object], coverage: Mapping[str, object], paths: Mapping[str, object], order: int
) -> tuple[float, float] | None:
    if not all(
        isinstance(values.get(path), dict) and cast(dict[str, object], values[path]).get("constructor") == kind
        for path, kind in _STORY_CONSTRUCTORS.items()
    ):
        return None
    if not all(_story_anchor(path, values, coverage) for path in _STORY_ANCHORS) or cast(int, values["item.id"]) <= 0:
        return None
    bounds = _rpc_path_bounds(paths.get("item.id"), order)
    if bounds is None:
        return None
    if not all(
        _rpc_path_bounds(paths.get(path), order) == bounds
        for path in (*_STORY_ANCHORS, "item.media", "item.media.photo")
    ):
        return None
    return bounds


def _optional_story_slot(path: str, values: Mapping[str, object]) -> bool:
    parent, separator, name = path.rpartition(".")
    node = values.get(parent)
    if not separator or not isinstance(node, dict):
        return False
    constructor = _STORY_CONSTRUCTORS.get(parent)
    if constructor is None or node.get("constructor") != constructor:
        return False
    if name not in _STORY_ABSENCE_FIELDS[constructor]:
        return False
    rule = PROFILE_FIELD_MANIFEST[constructor].fields.get(name)
    return (
        rule is not None
        and rule.classification is ProfileFieldClass.PROFILE
        and rule.optional
        and not rule.flag
        and not rule.clear_on_none
    )


def _story_coverage_refinement(row: tuple[object, ...], payload: Mapping[str, object]) -> dict[str, object] | None:
    key, fields_json, coverage_json, paths_json, order, observed_at = row
    fields, coverage, paths = (_coverage_json_mapping(value) for value in (fields_json, coverage_json, paths_json))
    if fields is None or coverage is None or paths is None or type(order) is not int or order <= 0:
        return None
    values = profile_value_paths({"item": fields})
    if not _linked_story_observation(
        key, values, coverage, paths, order=order, observed_at=observed_at, payload=payload
    ):
        return None
    return _refine_story_absent_slots(coverage, values, paths, order)


def _refine_story_absent_slots(
    coverage: Mapping[str, object], values: Mapping[str, object], paths: Mapping[str, object], order: int
) -> dict[str, object]:
    result = dict(coverage)
    authority = _MergeAuthority(coverage, observations=paths, order=order)
    incoming = ProfileFieldCoverage(
        ProfileFieldClass.PROFILE, ProfileFieldState.API_NOT_RETURNED, False, "endpoint_did_not_return_value"
    )
    for path, evidence in coverage.items():
        if not isinstance(evidence, dict) or evidence.get("reason") != "partial_value_unobserved":
            continue
        if _optional_story_slot(path, values) and _refines_unknown_absence(path, evidence, incoming, values, authority):
            result[path] = asdict(incoming)
    return result


def _linked_story_observation(  # noqa: PLR0913 - one persisted receipt proof
    key: object,
    values: Mapping[str, object],
    coverage: Mapping[str, object],
    paths: Mapping[str, object],
    *,
    order: int,
    observed_at: object,
    payload: Mapping[str, object],
) -> bool:
    if str(values.get("item.id")) != key:
        return False
    bounds = _complete_story_bounds(values, coverage, paths, order)
    scan_bounds = _positive_bounds(payload.get("observation_started_at"), payload.get("observation_completed_at"))
    return (
        bounds is not None
        and scan_bounds is not None
        and observed_at == bounds[1]
        and scan_bounds[0] <= bounds[0] <= bounds[1] <= scan_bounds[1]
    )


def refine_published_story_coverage(
    conn: sqlite3.Connection,
    owner: int,
    peer: int,
    *,
    section: Literal["current_stories", "pinned_stories"] = "pinned_stories",
) -> tuple[int, int, int]:
    """Refine proven past optional omissions without creating a new observation."""
    require_write_transaction(conn)
    if section not in {"current_stories", "pinned_stories"}:
        raise ValueError("unsupported story coverage section")
    receipt = _story_receipt(conn, owner, peer, section)
    if receipt is None:
        return 0, 0, 0
    encoded_receipt, payload = receipt
    published = cast(dict[str, object], payload["published"])
    rows = conn.execute(
        "SELECT f.item_key,f.fields_json,f.coverage_json,f.provenance_json,f.observation_order,f.observed_at "
        "FROM entity_profile_collection_facts f JOIN entity_profile_collection_membership m "
        "USING(owner_account_id,entity_id,section,item_key) WHERE f.owner_account_id=? AND f.entity_id=? "
        "AND f.section=? AND m.generation=? AND m.scan=? AND m.detail_complete=1 "
        "AND m.identity_kind='remote' AND f.coverage_json LIKE '%partial_value_unobserved%' ORDER BY f.item_key",
        (owner, peer, section, published["generation"], published["scan"]),
    ).fetchall()
    items, slots, unresolved = 0, 0, 0
    for row in rows:
        coverage = _story_coverage_refinement(row, payload)
        previous = _coverage_json_mapping(row[2])
        changed = (
            sum(previous.get(path) != value for path, value in coverage.items())
            if coverage is not None and previous is not None
            else 0
        )
        if not changed:
            unresolved += 1
            continue
        assert coverage is not None
        _write_story_coverage_refinement(
            conn, (owner, peer, section), row, (encoded_receipt, published), encode(coverage)
        )
        items += 1
        slots += changed
        if _unresolved_story_absence(coverage):
            unresolved += 1
    return items, slots, unresolved


def _unresolved_story_absence(coverage: Mapping[str, object]) -> bool:
    return any(
        isinstance(value, dict)
        and value.get("state") == "unknown"
        and value.get("reason") == "partial_value_unobserved"
        for value in coverage.values()
    )


def _write_story_coverage_refinement(
    conn: sqlite3.Connection,
    identity: tuple[int, int, str],
    row: tuple[object, ...],
    receipt: tuple[str, Mapping[str, object]],
    coverage: str,
) -> None:
    owner, peer, section = identity
    encoded_receipt, published = receipt
    changed = conn.execute(
        "UPDATE entity_profile_collection_facts SET coverage_json=? WHERE owner_account_id=? AND entity_id=? "
        "AND section=? AND item_key=? AND fields_json=? AND coverage_json=? AND provenance_json=? "
        "AND observation_order=? AND observed_at=? AND EXISTS(SELECT 1 FROM entity_detail_sections s "
        "JOIN entity_details d ON d.entity_id=s.entity_id WHERE s.entity_id=? AND s.section=? "
        "AND s.payload_json=? AND s.normalization_version=? AND s.status='fresh' AND s.reason IS NULL "
        "AND d.profile_owner_account_id=?) AND EXISTS(SELECT 1 FROM entity_profile_collection_membership m "
        "WHERE m.owner_account_id=? AND m.entity_id=? AND m.section=? AND m.item_key=? "
        "AND m.generation=? AND m.scan=? AND m.detail_complete=1 AND m.identity_kind='remote')",
        (
            coverage,
            owner,
            peer,
            section,
            *row,
            peer,
            section,
            encoded_receipt,
            PROFILE_NORMALIZATION_VERSION,
            owner,
            owner,
            peer,
            section,
            row[0],
            published["generation"],
            published["scan"],
        ),
    ).rowcount
    if changed != 1:
        raise sqlite3.OperationalError("published story coverage refinement changed")
