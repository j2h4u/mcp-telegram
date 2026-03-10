from __future__ import annotations

from dataclasses import dataclass

from rapidfuzz import fuzz, process, utils

AUTO_THRESHOLD = 90
CANDIDATE_THRESHOLD = 60


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


def resolve(query: str, choices: dict[int, str]) -> ResolveResult:
    """Fuzzy-match query against {entity_id: name} mapping.

    - Numeric query: bypass fuzzy, return Resolved/NotFound by id
    - >=1 match at >=90: single -> Resolved; multiple -> Candidates
    - Matches only at 60-89: Candidates
    - No matches at >=60: NotFound
    """
    if query.isdigit():
        entity_id = int(query)
        if entity_id in choices:
            return Resolved(entity_id=entity_id, display_name=choices[entity_id])
        return NotFound(query=query)

    # Build reversed mapping: name -> entity_id for rapidfuzz
    name_to_id: dict[str, int] = {name: eid for eid, name in choices.items()}

    hits = process.extract(
        query,
        name_to_id.keys(),
        scorer=fuzz.WRatio,
        processor=utils.default_process,
        score_cutoff=CANDIDATE_THRESHOLD,
        limit=None,
    )
    # hits: [(name, score, list_index), ...] sorted descending by score

    if not hits:
        return NotFound(query=query)

    above_auto = [(name, int(score), name_to_id[name]) for name, score, _idx in hits if score >= AUTO_THRESHOLD]

    if len(above_auto) == 1:
        name, _score, entity_id = above_auto[0]
        return Resolved(entity_id=entity_id, display_name=name)

    if len(above_auto) >= 2:
        return Candidates(query=query, matches=above_auto)

    # All hits are in 60-89 range
    matches = [(name, int(score), name_to_id[name]) for name, score, _idx in hits]
    return Candidates(query=query, matches=matches)
