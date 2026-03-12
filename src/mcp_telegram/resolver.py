from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from rapidfuzz import fuzz, process, utils

if TYPE_CHECKING:
    from .cache import EntityCache

AUTO_THRESHOLD = 90
CANDIDATE_THRESHOLD = 60

# Cyrillic → Latin transliteration table (multi-char mappings first)
_CYR_TO_LAT: list[tuple[str, str]] = [
    ("ж", "zh"), ("ё", "yo"), ("х", "kh"), ("ц", "ts"), ("ч", "ch"),
    ("ш", "sh"), ("щ", "sch"), ("ю", "yu"), ("я", "ya"),
    ("а", "a"), ("б", "b"), ("в", "v"), ("г", "g"), ("д", "d"),
    ("е", "e"), ("з", "z"), ("и", "i"), ("й", "y"), ("к", "k"),
    ("л", "l"), ("м", "m"), ("н", "n"), ("о", "o"), ("п", "p"),
    ("р", "r"), ("с", "s"), ("т", "t"), ("у", "u"), ("ф", "f"),
    ("ъ", ""), ("ы", "y"), ("ь", ""), ("э", "e"),
]


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


def _has_cyrillic(text: str) -> bool:
    return any("\u0400" <= c <= "\u04ff" for c in text)


def _transliterate(text: str) -> str:
    result = text.lower()
    for cyr, lat in _CYR_TO_LAT:
        result = result.replace(cyr, lat)
    return result


def _fuzzy_resolve(query: str, choices: dict[int, str], cache: EntityCache | None = None) -> ResolveResult:
    """Fuzzy match query against choices.

    - All hits >=60 extracted into matches list
    - Apply exact case-insensitive filter → if match found, return Resolved
    - Otherwise → always return Candidates (even single fuzzy hit >=90)
    """
    name_to_id: dict[str, int] = {name: eid for eid, name in choices.items()}

    hits = process.extract(
        query,
        name_to_id.keys(),
        scorer=fuzz.WRatio,
        processor=utils.default_process,
        score_cutoff=CANDIDATE_THRESHOLD,
        limit=None,
    )

    if not hits:
        return NotFound(query=query)

    # Check for exact case-insensitive match among all hits
    query_lower = query.lower().strip()
    for name, score, _idx in hits:
        if name.lower().strip() == query_lower:
            entity_id = name_to_id[name]
            return Resolved(entity_id=entity_id, display_name=name)

    # No exact match → return all hits as Candidates with metadata
    matches = []
    for name, score, _idx in hits:
        entity_id = name_to_id[name]
        entity_info = {
            "entity_id": entity_id,
            "display_name": name,
            "score": int(score),
            "username": None,
            "entity_type": None,
        }
        # Fetch metadata from cache if available
        if cache:
            try:
                cached = cache.get(entity_id, ttl_seconds=300)  # 5-min TTL for metadata
                if cached:
                    entity_info["username"] = cached.get("username")
                    entity_info["entity_type"] = cached.get("type")
            except Exception:
                pass  # Ignore cache errors, use None values
        matches.append(entity_info)

    return Candidates(query=query, matches=matches)


def resolve(query: str, choices: dict[int, str], cache: EntityCache | None = None) -> ResolveResult:
    """Resolve query to entity using 5-case logic.

    Case 1: Numeric ID query → Resolved/NotFound by id
    Case 2: @username query → lookup in cache, Resolved/NotFound (requires cache)
    Case 3: Exact case-insensitive string match → Resolved
    Case 4: All fuzzy matches >=60 → Candidates (don't auto-resolve single fuzzy hit >=90)
    Case 5: No matches >=60 → NotFound
    Bonus: Cyrillic query → retry with transliteration if initial attempt fails

    Args:
        query: User input (numeric ID, @username, or name string)
        choices: {entity_id: name} mapping
        cache: Optional EntityCache for @username lookup and metadata fetch

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
        username_query = query[1:]  # Strip @
        try:
            # Search cache for entity with matching username
            result = cache.get_by_username(username_query)
            if result:
                entity_id, name = result
                return Resolved(entity_id=entity_id, display_name=name)
        except Exception:
            pass  # Ignore cache errors
        return NotFound(query=query)

    # Cases 3-5: Fuzzy matching with exact match priority
    result = _fuzzy_resolve(query, choices, cache)

    # Retry with transliteration if Cyrillic query didn't resolve
    if isinstance(result, NotFound) and _has_cyrillic(query):
        result = _fuzzy_resolve(_transliterate(query), choices, cache)

    return result
