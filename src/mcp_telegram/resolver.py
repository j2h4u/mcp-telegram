from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from anyascii import anyascii
from rapidfuzz import fuzz, process, utils

logger = logging.getLogger(__name__)

CANDIDATE_THRESHOLD = 60

# t.me link pattern: optional https://, optional www., t.me/username[/message_id]
_TME_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?t\.me/([a-zA-Z_][a-zA-Z0-9_]{3,})(?:/(\d+))?$"
)


def latinize(text: str) -> str:
    """Normalize any-script text to lowercase Latin for fuzzy matching."""
    return re.sub(r"[^a-z0-9 ]+", "", anyascii(text).lower()).strip()


@dataclass(frozen=True)
class Resolved:
    """Exact match: entity uniquely identified."""
    entity_id: int
    display_name: str


@dataclass(frozen=True)
class ResolvedWithMessage(Resolved):
    """Resolution result that also carries a message_id (from t.me/channel/123 links)."""
    message_id: int | None = None


@dataclass(frozen=True)
class Candidates:
    """Multiple matches found — caller should disambiguate."""
    query: str
    matches: list[dict]
    """Each dict: {entity_id: int, display_name: str, score: float, username: str|None, entity_type: str}."""


@dataclass(frozen=True)
class NotFound:
    """No match found for the query."""
    query: str


ResolveResult = Resolved | ResolvedWithMessage | Candidates | NotFound


def _parse_numeric_query(query: str) -> int | None:
    normalized = query.strip()
    if not normalized:
        return None
    if normalized.isdigit():
        return int(normalized)
    if normalized[0] in "+-" and normalized[1:].isdigit():
        return int(normalized)
    return None


def parse_exact_dialog_id(dialog: str) -> int | None:
    """Return an exact dialog id for one signed numeric selector string.

    Rejects @username queries and non-numeric strings.
    """
    selector = dialog.strip()
    if not selector or selector.startswith("@"):
        return None
    return _parse_numeric_query(selector)


def _parse_tme_link(query: str) -> tuple[str, int | None] | None:
    """Extract (username, message_id|None) from a t.me URL.

    Returns None if query is not a t.me link.
    """
    match = _TME_RE.match(query.strip())
    if not match:
        return None
    username = match.group(1)
    msg_id = int(match.group(2)) if match.group(2) else None
    return (username, msg_id)


def _build_norm_map(
    display_name_map: dict[int, str],
    normalized_name_map: dict[int, str] | None,
) -> dict[str, list[tuple[int, str]]]:
    """Build {normalized_name: [(entity_id, original_name), ...]} lookup."""
    norm_map: dict[str, list[tuple[int, str]]] = {}
    if normalized_name_map is not None:
        for entity_id, norm_name in normalized_name_map.items():
            original_name = display_name_map.get(entity_id, norm_name)
            norm_map.setdefault(norm_name, []).append((entity_id, original_name))
    else:
        for entity_id, name in display_name_map.items():
            norm_name = latinize(name)
            norm_map.setdefault(norm_name, []).append((entity_id, name))
    return norm_map


def _fuzzy_resolve(
    query: str,
    display_name_map: dict[int, str],
    entity_cache: Any | None = None,
    *,
    normalized_name_map: dict[int, str] | None = None,
) -> ResolveResult:
    """Fuzzy match query against display_name_map in normalized (Latin) space.

    - Normalizes both query and display_name_map via latinize()
    - Exact normalized match with multi-word query → Resolved
    - Single-word query with ≥2 hits → always Candidates (even if exact)
    - Otherwise exact normalized match → Resolved
    - No exact → all hits ≥60 as Candidates
    """
    norm_map = _build_norm_map(display_name_map, normalized_name_map)
    norm_name_to_id: dict[str, int] = {
        norm_name: entries[0][0] for norm_name, entries in norm_map.items()
    }
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
    exact_entity_id: int | None = None
    exact_display_name: str | None = None
    for norm_name, _score, _idx in hits:
        if norm_name == norm_query:
            entries = norm_map[norm_name]
            exact_entity_id = entries[0][0]
            exact_display_name = entries[0][1]
            break

    if is_single_word and len(hits) >= 2:
        matches = _build_matches(hits, norm_map, entity_cache, exact_first_id=exact_entity_id)
        return Candidates(query=query, matches=matches)

    if exact_entity_id is not None:
        assert exact_display_name is not None  # set atomically with exact_entity_id on line above
        # Collision check: if ≥2 distinct entity_ids share the same normalized name,
        # auto-pick would violate the Resolved contract ("unique entity identified").
        # Always return Candidates when collision is detected.
        exact_entries = norm_map[norm_query]
        if len(exact_entries) >= 2:
            logger.debug(
                "resolver_collision query=%r n_entities=%d",
                query,
                len(exact_entries),
            )
            matches = _build_matches(hits, norm_map, entity_cache, exact_first_id=exact_entity_id, collision_query=query)
            return Candidates(query=query, matches=matches)
        return Resolved(entity_id=exact_entity_id, display_name=exact_display_name)

    matches = _build_matches(hits, norm_map, entity_cache)
    return Candidates(query=query, matches=matches)


def _build_matches(
    hits: list[tuple[str, float, int]],
    norm_map: dict[str, list[tuple[int, str]]],
    entity_cache: Any | None,
    exact_first_id: int | None = None,
    collision_query: str | None = None,
) -> list[dict]:
    """Build match dicts from rapidfuzz hits, optionally putting exact_first_id first.

    When collision_query is provided (≥2 entities share the same normalized name),
    a ``disambiguation_hint`` string is added to every match dict.
    """
    matches: list[dict] = []
    seen_ids: set[int] = set()

    if exact_first_id is not None:
        for norm_name, score, _idx in hits:
            for entity_id, original_name in norm_map.get(norm_name, []):
                if entity_id == exact_first_id and entity_id not in seen_ids:
                    seen_ids.add(entity_id)
                    matches.append(_make_match_info(entity_id, original_name, int(score), entity_cache))

    for norm_name, score, _idx in hits:
        for entity_id, original_name in norm_map.get(norm_name, []):
            if entity_id in seen_ids:
                continue
            seen_ids.add(entity_id)
            matches.append(_make_match_info(entity_id, original_name, int(score), entity_cache))

    if collision_query is not None:
        n = len(matches)
        types = sorted({m["entity_type"] or "Unknown" for m in matches})
        hint = (
            f'{n} entities match "{collision_query}": {", ".join(types)}. '
            f'Specify @username or numeric id.'
        )
        for m in matches:
            m["disambiguation_hint"] = hint
    else:
        for m in matches:
            m["disambiguation_hint"] = None

    return matches


def _make_match_info(entity_id: int, display_name: str, score: int, entity_cache: Any | None) -> dict:
    entity_info: dict = {
        "entity_id": entity_id,
        "display_name": display_name,
        "score": score,
        "username": None,
        "entity_type": None,
        "disambiguation_hint": None,
    }
    if entity_cache:
        try:
            entity_cached = entity_cache.get(entity_id, ttl_seconds=300)
            if entity_cached:
                entity_info["username"] = entity_cached.get("username")
                entity_info["entity_type"] = entity_cached.get("type")
        except (sqlite3.OperationalError, sqlite3.DatabaseError, OSError):
            pass
        except Exception:
            logger.warning("unexpected entity_cache error in fuzzy resolve for entity_id=%r", entity_id, exc_info=True)
    return entity_info


def resolve(
    query: str,
    display_name_map: dict[int, str],
    entity_cache: Any | None = None,
    *,
    normalized_name_map: dict[int, str] | None = None,
) -> ResolveResult:
    """Resolve query to entity using normalized matching (pure/sync).

    Case 1: Numeric ID query → Resolved/NotFound by id
    Case 2: @username query → lookup in entity_cache, Resolved/NotFound (requires entity_cache)
    Case 3-5: Fuzzy matching in latinized space with single-word caution

    entity_cache must expose .get(id, ttl_seconds=) and .get_by_username(str).
    Pass None to skip @username resolution.
    """
    entity_id = _parse_numeric_query(query)
    if entity_id is not None:
        if entity_id in display_name_map:
            return Resolved(entity_id=entity_id, display_name=display_name_map[entity_id])
        return NotFound(query=query)

    if query.startswith("@") and entity_cache:
        username_query = query[1:]
        try:
            result = entity_cache.get_by_username(username_query)
            if result:
                entity_id, name = result
                return Resolved(entity_id=entity_id, display_name=name)
        except (sqlite3.OperationalError, sqlite3.DatabaseError, OSError):
            pass
        except Exception:
            logger.warning("unexpected entity_cache error in @username resolve for query=%r", query, exc_info=True)
        return NotFound(query=query)

    if query.startswith("@"):
        return NotFound(query=query)

    return _fuzzy_resolve(query, display_name_map, entity_cache, normalized_name_map=normalized_name_map)
