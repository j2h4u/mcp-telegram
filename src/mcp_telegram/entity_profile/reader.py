"""Account-fenced, bounded SELECT projections over retained profile evidence."""

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import cast

from ..pagination import ProfileNavigationToken, decode_profile_navigation, encode_profile_navigation
from .contracts import PROFILE_COLLECTION_SECTIONS
from .output_schema import (
    project_collection_item,
    project_history_value,
    project_legacy_collection_item,
    project_profile_fields,
)

_MAX_PAGE_SIZE = 100


@dataclass(frozen=True)
class _ReadScope:
    conn: sqlite3.Connection
    owner: int
    entity: int
    now: int


def _object(encoded: object) -> dict[str, object]:
    value: object = json.loads(encoded) if isinstance(encoded, str) else encoded
    return _mapping(value)


def _mapping(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def _fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _age(now: int, value: object) -> int | None:
    return max(0, now - int(value)) if isinstance(value, (int, float)) else None


def semantic_profile(conn: sqlite3.Connection, owner: int, entity: int, now: int) -> dict[str, object]:
    row = cast(
        tuple[object, ...] | None,
        conn.execute(
            "SELECT fields_json,coverage_json,provenance_json,normalization_version,complete,"
            "observation_started_at,observation_completed_at FROM entity_profile_semantic_snapshots "
            "WHERE owner_account_id=? AND entity_id=?",
            (owner, entity),
        ).fetchone(),
    )
    if row is None:
        return {
            "fields": {},
            "field_evidence": [],
            "availability": "not_observed",
            "complete": False,
            "acquisition": {"availability": "not_observed"},
        }
    provenance = _object(row[2])
    paths = provenance.get("fields", {})
    paths = cast(dict[str, object], paths) if isinstance(paths, dict) else {}
    coverage = _object(row[1])
    evidence = _field_evidence(coverage, paths, now, _profile_values(_object(row[0])))
    return {
        "fields": project_profile_fields(_object(row[0])),
        "field_evidence": evidence,
        "availability": "retained" if _object(row[0]) else "not_observed",
        "complete": bool(row[4]),
        "normalization_version": row[3],
        "acquisition": _acquisition_evidence(provenance, row[5:7], now),
    }


def _field_evidence(
    coverage: dict[str, object], paths: dict[str, object], now: int, values: dict[str, object]
) -> list[dict[str, object]]:
    return [
        {
            "path": path,
            "value": _value_evidence(path, _mapping(coverage.get(path)), _mapping(paths.get(path)), values, now),
        }
        for path in sorted(set(coverage) | set(paths))
        if isinstance(coverage.get(path), dict) or isinstance(paths.get(path), dict)
        if _public_evidence(path, _mapping(coverage.get(path)))
    ]


def _public_evidence(path: str, field: dict[str, object]) -> bool:
    return not path.startswith("__invalidation__:") and field.get("classification") not in {
        "capability",
        "inapplicable",
    }


def _value_evidence(
    path: str, field: dict[str, object], observed: dict[str, object], values: dict[str, object], now: int
) -> dict[str, object]:
    known = field.get("state") in {"present", "cleared"}
    state = "present" if path in values and values[path] is not None else "unknown"
    if path in values and field.get("state") == "cleared" and field.get("authoritative"):
        state = "cleared"
    return {
        "availability": "retained" if path in values else "not_observed",
        "state": state,
        "authoritative": field.get("authoritative") if known else None,
        "observed_at": observed.get("observed_at"),
        "observation_started_at": observed.get(
            "observation_started_at", observed.get("observed_at") if observed.get("source") == "rpc" else None
        ),
        "observation_completed_at": observed.get("observation_completed_at"),
        "source": observed.get("source"),
        "age_seconds": _age(now, observed.get("observed_at", observed.get("observation_completed_at"))),
    }


def _profile_values(fields: dict[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    for path, value in fields.items():
        _collect_values(result, path, value)
    return result


def _collect_values(result: dict[str, object], path: str, value: object) -> None:
    result[path] = value
    if isinstance(value, dict):
        for key, child in cast(dict[str, object], value).items():
            _collect_values(result, path + "." + key, child)
    elif isinstance(value, list):
        for index, child in enumerate(cast(list[object], value)):
            _collect_values(result, f"{path}[{index}]", child)


def _acquisition_evidence(provenance: dict[str, object], bounds: tuple[object, ...], now: int) -> dict[str, object]:
    coverage = provenance.get("acquisition_coverage")
    if not isinstance(coverage, dict) or any(value is None for value in bounds):
        return {"availability": "not_observed"}
    return {
        "availability": "observed",
        "source": "rpc",
        "endpoint": provenance.get("endpoint"),
        "layer": provenance.get("layer"),
        "diagnostics": provenance.get("diagnostics", []),
        "observation_started_at": bounds[0],
        "observation_completed_at": bounds[1],
        "age_seconds": _age(now, bounds[1]),
        "field_coverage": [
            {
                "path": path,
                "state": field.get("state", "unknown"),
                "authoritative": field.get("authoritative", False),
                "reason": field.get("reason"),
            }
            for path, field in cast(dict[str, dict[str, object]], coverage).items()
            if _public_evidence(path, field)
        ],
    }


def history_coverage(conn: sqlite3.Connection, owner: int, entity: int) -> dict[str, object]:
    first = cast(
        tuple[object, ...] | None,
        conn.execute(
            "SELECT event_id FROM entity_profile_change_events WHERE owner_account_id=? AND entity_id=? "
            "ORDER BY event_id LIMIT 1",
            (owner, entity),
        ).fetchone(),
    )
    last = cast(
        tuple[object, ...] | None,
        conn.execute(
            "SELECT event_id FROM entity_profile_change_events WHERE owner_account_id=? AND entity_id=? "
            "ORDER BY event_id DESC LIMIT 1",
            (owner, entity),
        ).fetchone(),
    )
    return {
        "mode": "observed_changes_only",
        "complete_past_history": False,
        "tracking_started_at": None,
        "first_retained_event_id": first[0] if first else None,
        "latest_retained_event_id": last[0] if last else None,
        "gaps": ["unobserved_past"],
    }


def _request(req: Mapping[str, object]) -> tuple[str | None, str | None, int]:
    section, cursor, limit = req.get("section"), req.get("cursor"), req.get("limit", 50)
    if section is not None and section not in {*PROFILE_COLLECTION_SECTIONS, "history"}:
        raise ValueError("Unsupported profile section")
    if cursor is not None and (not isinstance(cursor, str) or section is None):
        raise ValueError("A profile cursor requires a selected section")
    if type(limit) is not int or not 1 <= limit <= _MAX_PAGE_SIZE:
        raise ValueError("Profile limit must be between 1 and 100")
    return cast(str | None, section), cast(str | None, cursor), limit


def read_profile(
    conn: sqlite3.Connection, owner: int, entity: int, now: int, req: Mapping[str, object]
) -> dict[str, object]:
    section, cursor, limit = _request(req)
    result: dict[str, object] = {
        "semantic": semantic_profile(conn, owner, entity, now),
        "history_coverage": history_coverage(conn, owner, entity),
    }
    summaries = {name: section_summary(conn, owner, entity, name) for name in sorted(PROFILE_COLLECTION_SECTIONS)}
    result["collections"] = summaries
    if section:
        result["page"] = (
            _history_page(_ReadScope(conn, owner, entity, now), cursor, limit)
            if section == "history"
            else _collection_page(_ReadScope(conn, owner, entity, now), section, cursor, limit)
        )
    return result


def section_summary(conn: sqlite3.Connection, owner: int, entity: int, section: str) -> dict[str, object]:
    row = cast(
        tuple[object, ...] | None,
        conn.execute(
            "SELECT s.status,s.observed_at,s.reason,s.retry_at,s.payload_json FROM entity_detail_sections s "
            "JOIN entity_details d ON d.entity_id=s.entity_id WHERE s.entity_id=? AND s.section=? "
            "AND d.profile_owner_account_id=?",
            (entity, section, owner),
        ).fetchone(),
    )
    payload = _object(row[4]) if row else {}
    published = payload.get("published")
    published = cast(dict[str, object], published) if isinstance(published, dict) else {}
    return {
        **_summary_metadata(payload),
        "status": row[0] if row else "not_observed",
        "observed_at": row[1] if row else None,
        "reason": row[2] if row else "not_observed",
        "retry_at": row[3] if row else None,
        "complete": payload.get("complete", False),
        "traversal_complete": payload.get("traversal_complete", False),
        "reported_count": payload.get("reported_count"),
        "retained_count": payload.get("retained_count"),
        "retained_unique_count": payload.get("retained_unique_count"),
        "details_complete": payload.get("details_complete"),
        "comparable_membership": payload.get("comparable_membership"),
        "generation": payload.get("generation"),
        "scan": payload.get("scan"),
        "changed_during_scan": payload.get("changed_during_scan", False),
        "published_generation": published.get("generation"),
        "published_scan": published.get("scan"),
        "observation_started_at": payload.get("observation_started_at"),
        "observation_completed_at": payload.get("observation_completed_at"),
    }


def _summary_metadata(payload: dict[str, object]) -> dict[str, object]:
    names = ("pinned_to_top", "max_read_id", "chat_notifications_enabled")
    metadata = _mapping(payload.get("metadata"))
    provenance = _mapping(payload.get("metadata_provenance"))
    return {
        "metadata": {key: metadata[key] for key in names if key in metadata},
        "metadata_evidence": [
            {
                "name": key,
                "observation_started_at": _mapping(provenance.get(key)).get("started_at"),
                "observation_completed_at": _mapping(provenance.get(key)).get("completed_at"),
            }
            for key in names
            if key in metadata
        ],
        "reported_counts": [
            {
                "count": value.get("count"),
                "observation_started_at": value.get("started_at"),
                "observation_completed_at": value.get("completed_at"),
            }
            for value in cast(list[dict[str, object]], payload.get("reported_counts", []))
        ],
        "diagnostics": payload.get("diagnostics", []),
        "count_discrepancy": payload.get("count_discrepancy", False),
        "identifiable": payload.get("identifiable", False),
    }


def _collection_source(
    conn: sqlite3.Connection, owner: int, entity: int, section: str
) -> tuple[dict[str, ProfileNavigationToken], dict[str, object], list[object], dict[str, object]]:
    row = cast(
        tuple[object, ...] | None,
        conn.execute(
            "SELECT s.payload_json,d.detail_json,s.observed_at,s.status,s.reason FROM entity_details d "
            "LEFT JOIN entity_detail_sections s ON s.entity_id=d.entity_id AND s.section=? "
            "WHERE d.entity_id=? AND d.profile_owner_account_id=?",
            (section, entity, owner),
        ).fetchone(),
    )
    raw = json.loads(cast(str, row[0])) if row and row[0] else None
    payload = cast(dict[str, object], raw) if isinstance(raw, dict) else {}
    legacy = _object(row[1]).get(section) if row else None
    legacy = cast(list[object], legacy if isinstance(legacy, list) else raw if isinstance(raw, list) else [])
    receipt = _legacy_receipt(owner, section, payload, raw, row)
    sources = _v2_sources(owner, entity, section, payload)
    if legacy or isinstance(raw, list):
        sources["legacy"] = ProfileNavigationToken(
            owner, entity, section, "legacy", fingerprint=_fingerprint([legacy, receipt])
        )
    if not sources:
        sources["legacy"] = ProfileNavigationToken(
            owner, entity, section, "legacy", fingerprint=_fingerprint([legacy, receipt])
        )
    return sources, payload, legacy, receipt


def _v2_sources(owner: int, entity: int, section: str, payload: dict[str, object]) -> dict[str, ProfileNavigationToken]:
    sources = {}
    published = payload.get("published")
    if isinstance(published, dict) and published.get("owner_account_id") == owner:
        sources["published"] = ProfileNavigationToken(
            owner,
            entity,
            section,
            "published",
            int(cast(int, published["generation"])),
            int(cast(int, published["scan"])),
        )
    if isinstance(payload.get("generation"), int) and not payload.get("complete"):
        sources["active"] = ProfileNavigationToken(
            owner,
            entity,
            section,
            "active",
            int(cast(int, payload["generation"])),
            int(cast(int, payload.get("scan", 0))),
            _fingerprint(payload),
        )
    return sources


def _legacy_receipt(
    owner: int, section: str, payload: dict[str, object], raw: object, row: tuple[object, ...] | None
) -> dict[str, object]:
    receipt = payload.get("legacy_evidence", {})
    receipt = (
        cast(dict[str, object], receipt)
        if isinstance(receipt, dict) and receipt.get("owner_account_id") == owner
        else {}
    )
    if isinstance(raw, list) and row:
        receipt = {
            "owner_account_id": owner,
            "section": section,
            "observed_at": row[2],
            "stored_status": row[3],
            "stored_reason": row[4],
            "traversal_coverage": "unknown",
        }
    return receipt


def _collection_page(scope: _ReadScope, section: str, cursor: str | None, limit: int) -> dict[str, object]:
    conn, owner, entity, now = scope.conn, scope.owner, scope.entity, scope.now
    sources, _payload, legacy, receipt = _collection_source(conn, owner, entity, section)
    source = sources.get("published", sources.get("legacy", sources.get("active")))
    assert source is not None
    token = _collection_token(cursor, source, sources)
    if token.source_kind == "legacy":
        result = _legacy_page(token, legacy, receipt, limit, now)
        result["available_sources"] = _source_descriptors(sources)
        return result
    rows = cast(
        list[tuple[object, ...]],
        conn.execute(
            "SELECT m.item_key,m.identity_kind,f.fields_json,f.coverage_json,f.provenance_json,f.observed_at "
            "FROM entity_profile_collection_membership m JOIN entity_profile_collection_facts f "
            "USING(owner_account_id,entity_id,section,item_key) WHERE m.owner_account_id=? AND m.entity_id=? "
            "AND m.section=? AND m.generation=? AND m.scan=? AND m.item_key>? ORDER BY m.item_key LIMIT ?",
            (owner, entity, section, token.generation, token.scan, token.last_key, limit + 1),
        ).fetchall(),
    )
    items = [_collection_item(section, row, now) for row in rows[:limit]]
    next_token = (
        encode_profile_navigation(replace(token, last_key=str(rows[limit - 1][0]))) if len(rows) > limit else None
    )
    return {
        "section": section,
        "source": token.source_kind,
        "generation": token.generation,
        "scan": token.scan,
        "value_semantics": "latest_retained_values",
        "membership_semantics": "fixed_published" if token.source_kind == "published" else "active_scan",
        "coverage": "published_traversal" if token.source_kind == "published" else "partial_scan",
        "items": items,
        "next_cursor": next_token,
        "has_more": next_token is not None,
        "returned_count": len(items),
        "returned_active_count": sum(item["state"] == "retained" for item in items),
        "available_sources": _source_descriptors(sources),
    }


def _collection_token(
    cursor: str | None, source: ProfileNavigationToken, sources: dict[str, ProfileNavigationToken]
) -> ProfileNavigationToken:
    token = (
        decode_profile_navigation(cursor, account=source.account, entity=source.entity, section=source.section)
        if cursor
        else source
    )
    if token.source_kind not in {"published", "active", "legacy"}:
        raise ValueError("Invalid collection navigation source")
    if token.source_kind != "published" and replace(token, last_key="") != sources.get(token.source_kind):
        raise ValueError("Local profile selection changed; restart this section without a cursor")
    return token


def _collection_item(section: str, row: tuple[object, ...], now: int) -> dict[str, object]:
    fields = _object(row[2])
    state = "deleted" if fields.get("constructor") == "StoryItemDeleted" else "retained"
    try:
        value = project_collection_item(section, fields)
    except ValueError:
        state, value = "unsupported", None
    return {
        "item_key": row[0],
        "identity_kind": row[1],
        "state": state,
        "value": value,
        "observed_at": row[5],
        "age_seconds": _age(now, row[5]),
        "field_evidence": _field_evidence(_object(row[3]), _object(row[4]), now, _profile_values({"item": fields})),
        "acquisition": {"availability": "unknown"},
    }


def _source_descriptors(sources: dict[str, ProfileNavigationToken]) -> list[dict[str, object]]:
    return [
        {
            "source": kind,
            "cursor": encode_profile_navigation(token),
            **({"generation": token.generation, "scan": token.scan} if kind != "legacy" else {}),
        }
        for kind, token in sources.items()
    ]


def _legacy_page(
    token: ProfileNavigationToken, values: list[object], receipt: dict[str, object], limit: int, now: int
) -> dict[str, object]:
    offset = int(token.last_key or 0)
    if offset < 0:
        raise ValueError("Invalid legacy navigation offset")
    page = values[offset : offset + limit]
    items = []
    for value in page:
        try:
            projected, state = project_legacy_collection_item(token.section, value), "retained"
        except ValueError:
            projected, state = None, "unsupported"
        items.append(
            {
                "item_key": None,
                "identity_kind": "legacy",
                "acquisition": {"availability": "unknown"},
                "state": state,
                "value": projected,
                "observed_at": None,
                "age_seconds": None,
            }
        )
    has_more = offset + limit < len(values)
    return {
        "section": token.section,
        "source": "legacy",
        "coverage": "legacy_unknown" if receipt or values else "not_observed",
        "value_semantics": "retained_legacy_values",
        "membership_semantics": "fingerprinted_legacy",
        "items": items,
        "legacy_evidence": {
            **receipt,
            "observation_scope": "legacy_section",
            "age_seconds": _age(now, receipt.get("observed_at")),
        },
        "next_cursor": encode_profile_navigation(replace(token, last_key=str(offset + limit))) if has_more else None,
        "has_more": has_more,
        "returned_count": len(items),
        "returned_active_count": sum(item["state"] == "retained" for item in items),
    }


def _history_page(scope: _ReadScope, cursor: str | None, limit: int) -> dict[str, object]:
    conn, owner, entity, now = scope.conn, scope.owner, scope.entity, scope.now
    latest = cast(
        tuple[object, ...] | None,
        conn.execute(
            "SELECT event_id FROM entity_profile_change_events WHERE owner_account_id=? AND entity_id=? "
            "ORDER BY event_id DESC LIMIT 1",
            (owner, entity),
        ).fetchone(),
    )
    initial = ProfileNavigationToken(
        owner, entity, "history", "history", watermark=cast(int, latest[0]) if latest else 0
    )
    token = decode_profile_navigation(cursor, account=owner, entity=entity, section="history") if cursor else initial
    if token.source_kind != "history":
        raise ValueError("Invalid history navigation source")
    rows = cast(
        list[tuple[object, ...]],
        conn.execute(
            "SELECT event_id,change_key,category,before_json,after_json,observed_at,source,"
            "observation_started_at,observation_completed_at,server_time,previous_provenance_json FROM entity_profile_change_events "
            "WHERE owner_account_id=? AND entity_id=? AND event_id<=? AND event_id<? ORDER BY event_id DESC LIMIT ?",
            (owner, entity, token.watermark, int(token.last_key) if token.last_key else token.watermark + 1, limit + 1),
        ).fetchall(),
    )
    items = [_history_item(row, now) for row in rows[:limit]]
    next_cursor = (
        encode_profile_navigation(replace(token, last_key=str(rows[limit - 1][0]))) if len(rows) > limit else None
    )
    return {
        "section": "history",
        "source": "history",
        "value_semantics": "observed_changes_only",
        "membership_semantics": "event_id_watermark",
        "watermark": token.watermark,
        "items": items,
        "next_cursor": next_cursor,
        "has_more": next_cursor is not None,
        "returned_count": len(items),
    }


def _history_item(row: tuple[object, ...], now: int) -> dict[str, object]:
    supported = True
    try:
        before = project_history_value(cast(str, row[1]), cast(object, json.loads(cast(str, row[3]))))
        after = project_history_value(cast(str, row[1]), cast(object, json.loads(cast(str, row[4]))))
    except ValueError:
        before, after, supported = None, None, False
    return {
        "previous_observations": _previous_observations(_object(row[10]), now),
        "before_precision": _object(row[10]).get("before_precision"),
        "event_id": row[0],
        "change_key": row[1],
        "category": row[2],
        "before": before,
        "after": after,
        "supported": supported,
        "observed_at": row[5],
        "source": row[6],
        "age_seconds": _age(now, row[5]),
        "observation_started_at": row[7],
        "observation_completed_at": row[8],
        "server_time": row[9],
        "time_semantics": "observed_evidence_not_action_time",
    }


def _previous_observations(previous: dict[str, object], now: int) -> list[dict[str, object]]:
    return [
        {
            "path": path,
            "order": value.get("order"),
            "observed_at": value.get("observed_at"),
            "source": value.get("source"),
            "observation_started_at": value.get("observation_started_at"),
            "observation_completed_at": value.get("observation_completed_at"),
            "age_seconds": _age(now, value.get("observed_at")),
        }
        for path, value in previous.items()
        if isinstance(value, dict) and not path.startswith("__invalidation__:")
    ]
