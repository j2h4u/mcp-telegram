from __future__ import annotations

from dataclasses import dataclass

from rapidfuzz import fuzz, process, utils

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
    matches: list[tuple[str, int, int]]  # (name, score, entity_id)


@dataclass(frozen=True)
class NotFound:
    query: str


ResolveResult = Resolved | Candidates | NotFound


def _has_cyrillic(text: str) -> bool:
    return any("\u0400" <= c <= "\u04ff" for c in text)


def _transliterate(text: str) -> str:
    result = text.lower()
    for cyr, lat in _CYR_TO_LAT:
        result = result.replace(cyr, lat)
    return result


def _fuzzy_resolve(query: str, choices: dict[int, str]) -> ResolveResult:
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

    above_auto = [(name, int(score), name_to_id[name]) for name, score, _idx in hits if score >= AUTO_THRESHOLD]

    if len(above_auto) == 1:
        name, _score, entity_id = above_auto[0]
        return Resolved(entity_id=entity_id, display_name=name)

    if len(above_auto) >= 2:
        return Candidates(query=query, matches=above_auto)

    matches = [(name, int(score), name_to_id[name]) for name, score, _idx in hits]
    if len(matches) == 1:
        name, _score, entity_id = matches[0]
        return Resolved(entity_id=entity_id, display_name=name)
    return Candidates(query=query, matches=matches)


def resolve(query: str, choices: dict[int, str]) -> ResolveResult:
    """Fuzzy-match query against {entity_id: name} mapping.

    - Numeric query: bypass fuzzy, return Resolved/NotFound by id
    - >=1 match at >=90: single -> Resolved; multiple -> Candidates
    - Matches only at 60-89: Candidates
    - No matches at >=60: NotFound
    - Cyrillic query: also retried with transliteration if initial match fails
    """
    if query.isdigit():
        entity_id = int(query)
        if entity_id in choices:
            return Resolved(entity_id=entity_id, display_name=choices[entity_id])
        return NotFound(query=query)

    result = _fuzzy_resolve(query, choices)

    # Retry with transliteration if Cyrillic query didn't resolve
    if isinstance(result, NotFound) and _has_cyrillic(query):
        result = _fuzzy_resolve(_transliterate(query), choices)

    return result
