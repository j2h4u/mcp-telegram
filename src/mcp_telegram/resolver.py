from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

from anyascii import anyascii
from rapidfuzz import fuzz, process, utils

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .cache import EntityCache

AUTO_THRESHOLD = 90
CANDIDATE_THRESHOLD = 60


def latinize(text: str) -> str:
    """Normalize any-script text to lowercase Latin for fuzzy matching."""
    return re.sub(r"[^a-z0-9 ]+", "", anyascii(text).lower()).strip()


@dataclass(frozen=True)
class Resolved:
    entity_id: int
    display_name: str


@dataclass(frozen=True)
class Candidates:
    query: str
    matches: list[dict]  # [{entity_id, display_name, score, username, entity_type}]


@dataclass(frozen=True)
class NotFound:
    query: str


ResolveResult = Resolved | Candidates | NotFound


def _parse_numeric_query(query: str) -> int | None:
    normalized = query.strip()
    if not normalized:
        return None
    if normalized.isdigit():
        return int(normalized)
    if normalized[0] in "+-" and normalized[1:].isdigit():
        return int(normalized)
    return None


def _fuzzy_resolve(
    query: str,
    choices: dict[int, str],
    cache: EntityCache | None = None,
    *,
    normalized_choices: dict[int, str] | None = None,
) -> ResolveResult:
    """Fuzzy match query against choices in normalized (Latin) space.

    - Normalizes both query and choices via latinize()
    - Exact normalized match with multi-word query → Resolved
    - Single-word query with ≥2 hits → always Candidates (even if exact)
    - Otherwise exact normalized match → Resolved
    - No exact → all hits ≥60 as Candidates
    """
    # Build normalized lookup: norm_name → (entity_id, original_name)
    if normalized_choices is not None:
        norm_map: dict[str, list[tuple[int, str]]] = {}
        for eid, norm_name in normalized_choices.items():
            original_name = choices.get(eid, norm_name)
            norm_map.setdefault(norm_name, []).append((eid, original_name))
    else:
        norm_map = {}
        for eid, name in choices.items():
            norm_name = latinize(name)
            norm_map.setdefault(norm_name, []).append((eid, name))

    # Flatten to {norm_name: first_eid} for rapidfuzz (it needs unique keys)
    norm_name_to_id: dict[str, int] = {}
    for norm_name, entries in norm_map.items():
        norm_name_to_id[norm_name] = entries[0][0]

    norm_query = latinize(query)

    hits = process.extract(
        norm_query,
        norm_name_to_id.keys(),
        scorer=fuzz.WRatio,
        processor=utils.default_process,
        score_cutoff=CANDIDATE_THRESHOLD,
        limit=None,
    )

    if not hits:
        return NotFound(query=query)

    is_single_word = " " not in query.strip()

    # Check for exact normalized match
    exact_entity_id: int | None = None
    exact_display_name: str | None = None
    for norm_name, _score, _idx in hits:
        if norm_name == norm_query:
            entries = norm_map[norm_name]
            exact_entity_id = entries[0][0]
            exact_display_name = entries[0][1]
            break

    # Single-word caution: if ≥2 total hits and single word → always Candidates
    if is_single_word and len(hits) >= 2:
        # Build matches, putting exact first if found
        matches = _build_matches(hits, norm_map, cache, exact_first_id=exact_entity_id)
        return Candidates(query=query, matches=matches)

    # Multi-word or single hit: exact match → Resolved
    if exact_entity_id is not None:
        return Resolved(entity_id=exact_entity_id, display_name=exact_display_name)  # type: ignore[arg-type]

    # No exact match → Candidates
    matches = _build_matches(hits, norm_map, cache)
    return Candidates(query=query, matches=matches)


def _build_matches(
    hits: list[tuple[str, float, int]],
    norm_map: dict[str, list[tuple[int, str]]],
    cache: EntityCache | None,
    exact_first_id: int | None = None,
) -> list[dict]:
    """Build match dicts from rapidfuzz hits, optionally putting exact_first_id first."""
    matches: list[dict] = []
    seen_ids: set[int] = set()

    # Put exact match first if requested
    if exact_first_id is not None:
        for norm_name, score, _idx in hits:
            for eid, original_name in norm_map.get(norm_name, []):
                if eid == exact_first_id and eid not in seen_ids:
                    seen_ids.add(eid)
                    matches.append(_make_match_info(eid, original_name, int(score), cache))

    for norm_name, score, _idx in hits:
        for eid, original_name in norm_map.get(norm_name, []):
            if eid in seen_ids:
                continue
            seen_ids.add(eid)
            matches.append(_make_match_info(eid, original_name, int(score), cache))

    return matches


def _make_match_info(entity_id: int, display_name: str, score: int, cache: EntityCache | None) -> dict:
    entity_info: dict = {
        "entity_id": entity_id,
        "display_name": display_name,
        "score": score,
        "username": None,
        "entity_type": None,
    }
    if cache:
        try:
            cached = cache.get(entity_id, ttl_seconds=300)
            if cached:
                entity_info["username"] = cached.get("username")
                entity_info["entity_type"] = cached.get("type")
        except (sqlite3.OperationalError, sqlite3.DatabaseError, OSError):
            pass
        except Exception:
            logger.warning("unexpected cache error in fuzzy resolve for entity_id=%r", entity_id, exc_info=True)
    return entity_info


def resolve(
    query: str,
    choices: dict[int, str],
    cache: EntityCache | None = None,
    *,
    normalized_choices: dict[int, str] | None = None,
) -> ResolveResult:
    """Resolve query to entity using normalized matching.

    Case 1: Numeric ID query → Resolved/NotFound by id
    Case 2: @username query → lookup in cache, Resolved/NotFound (requires cache)
    Case 3-5: Fuzzy matching in latinized space with single-word caution

    Args:
        query: User input (numeric ID, @username, or name string)
        choices: {entity_id: name} mapping
        cache: Optional EntityCache for @username lookup and metadata fetch
        normalized_choices: Optional pre-computed {entity_id: latinized_name} for perf

    Returns:
        Resolved | Candidates | NotFound
    """
    # Case 1: Numeric ID query
    entity_id = _parse_numeric_query(query)
    if entity_id is not None:
        if entity_id in choices:
            return Resolved(entity_id=entity_id, display_name=choices[entity_id])
        return NotFound(query=query)

    # Case 2: @username query
    if query.startswith("@") and cache:
        username_query = query[1:]
        try:
            result = cache.get_by_username(username_query)
            if result:
                entity_id, name = result
                return Resolved(entity_id=entity_id, display_name=name)
        except (sqlite3.OperationalError, sqlite3.DatabaseError, OSError):
            pass
        except Exception:
            logger.warning("unexpected cache error in @username resolve for query=%r", query, exc_info=True)
        return NotFound(query=query)

    # Cases 3-5: Fuzzy matching in normalized space
    return _fuzzy_resolve(query, choices, cache, normalized_choices=normalized_choices)
