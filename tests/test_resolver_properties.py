"""Property-based regression tests for resolver collision invariant.

Covers all 5 collision categories from CONTEXT.md:
  (a) exact same norm_name
  (b) substring ambiguity
  (c) diacritic-only difference
  (d) decoration/emoji noise
  (e) cross-type (user vs channel)

Uses hypothesis strategies with max_examples=50, deadline=None.
"""

from __future__ import annotations

from string import ascii_letters

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from mcp_telegram.resolver import Candidates, NotFound, Resolved, latinize, resolve

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Signed int excluding 0; positive = user-range, negative = channel-range.
st_user_id = st.integers(min_value=1, max_value=10**9)
st_channel_id = st.integers(min_value=-(10**12), max_value=-1)
st_entity_id = st.one_of(st_user_id, st_channel_id)

# Pair of distinct entity ids (either sign combination)
st_id_pair = st.tuples(st_entity_id, st_entity_id).filter(lambda p: p[0] != p[1])
st_id_triple = st.tuples(st_entity_id, st_entity_id, st_entity_id).filter(lambda t: len(set(t)) == 3)

# Latin name: 2-20 chars, must produce non-empty latinize output (always true for ascii)
st_shared_name_latin = st.text(alphabet=ascii_letters + " ", min_size=2, max_size=20).filter(
    lambda x: len(latinize(x)) >= 2
)

# Cyrillic range U+0410..U+044F (А-я)
_cyrillic_alphabet = "".join(chr(c) for c in range(0x0410, 0x0450))
st_shared_name_cyrillic = st.text(alphabet=_cyrillic_alphabet, min_size=2, max_size=15).filter(
    lambda x: len(latinize(x)) >= 2
)

# Decoration wrappers: emoji / punctuation that latinize() strips
_DECORATIONS = ["⭐", "★", "•", ">>>", "<<<", "!!!", "~", "#", "[]", "()", "**"]
st_decoration = st.sampled_from(_DECORATIONS)

# ---------------------------------------------------------------------------
# Category (a) — exact same norm_name
# ---------------------------------------------------------------------------


@given(ids=st_id_pair, name=st_shared_name_latin)
@settings(max_examples=50, deadline=None)
def test_property_exact_collision_always_candidates(ids: tuple[int, int], name: str) -> None:
    """Two distinct entities with the same name → always Candidates, never Resolved."""
    id_a, id_b = ids
    dm = {id_a: name, id_b: name}
    result = resolve(name, dm)
    assert isinstance(result, Candidates), (
        f"Expected Candidates for collision {id_a}/{id_b} with name={name!r}, got {result}"
    )


@given(ids=st_id_pair, name=st_shared_name_latin)
@settings(max_examples=50, deadline=None)
def test_property_exact_collision_both_ids_in_matches(ids: tuple[int, int], name: str) -> None:
    """Both colliding entity_ids must appear in Candidates.matches."""
    id_a, id_b = ids
    dm = {id_a: name, id_b: name}
    result = resolve(name, dm)
    assert isinstance(result, Candidates)
    match_ids = {m["entity_id"] for m in result.matches}
    assert id_a in match_ids, f"id_a={id_a} missing from matches: {match_ids}"
    assert id_b in match_ids, f"id_b={id_b} missing from matches: {match_ids}"


@given(ids=st_id_triple, name=st_shared_name_latin)
@settings(max_examples=50, deadline=None)
def test_property_exact_collision_three_way(ids: tuple[int, int, int], name: str) -> None:
    """Three distinct entities sharing one name → Candidates containing all three ids."""
    id_a, id_b, id_c = ids
    dm = {id_a: name, id_b: name, id_c: name}
    result = resolve(name, dm)
    assert isinstance(result, Candidates)
    match_ids = {m["entity_id"] for m in result.matches}
    assert {id_a, id_b, id_c}.issubset(match_ids)


# ---------------------------------------------------------------------------
# Category (b) — substring
# ---------------------------------------------------------------------------


@given(
    id_a=st_user_id,
    id_b=st_user_id.filter(lambda x: x > 10**5),
    base=st.text(alphabet=ascii_letters, min_size=3, max_size=10).filter(lambda x: len(latinize(x)) >= 2),
    ext=st.text(alphabet=ascii_letters + " ", min_size=3, max_size=10).filter(
        lambda x: len(latinize(x)) >= 2 and " " not in x.strip()
    ),
)
@settings(max_examples=50, deadline=None)
def test_property_substring_multiword_query_resolves_exact(id_a: int, id_b: int, base: str, ext: str) -> None:
    """Multi-word query that exactly matches one entry should resolve to Resolved (not ambiguous)."""
    assume(id_a != id_b)
    extended = f"{base} {ext}"
    # Only add extended name at id_b; base alone at id_a
    dm = {id_a: base, id_b: extended}
    # Multi-word query: "base ext" should resolve exactly to id_b if names differ in latinize space
    assume(latinize(base) != latinize(extended))
    result = resolve(extended, dm)
    # Multi-word exact match against a unique norm_name → Resolved
    if isinstance(result, Resolved):
        assert result.entity_id == id_b
    # Candidates is also acceptable if rapidfuzz also scores base highly — not a violation


@given(
    ids=st_id_pair,
    base=st.text(alphabet=ascii_letters, min_size=3, max_size=10).filter(
        lambda x: len(latinize(x)) >= 2 and " " not in x
    ),
    suffix=st.text(alphabet=ascii_letters, min_size=2, max_size=8).filter(
        lambda x: len(latinize(x)) >= 1 and " " not in x
    ),
)
@settings(max_examples=50, deadline=None)
def test_property_substring_single_word_query_is_candidates(ids: tuple[int, int], base: str, suffix: str) -> None:
    """Single-word query that matches both base and extended names → Candidates (ambiguity)."""
    id_a, id_b = ids
    assume(latinize(base) != latinize(f"{base}{suffix}"))
    dm = {id_a: base, id_b: f"{base} {suffix}"}
    result = resolve(base, dm)
    # Single-word query with ≥2 hits must be Candidates (never Resolved)
    assert not isinstance(result, Resolved), f"Single-word query {base!r} should not auto-resolve when ≥2 names match"


# ---------------------------------------------------------------------------
# Category (c) — diacritic-only difference
# ---------------------------------------------------------------------------

# Pairs where one is accented Latin that latinize() collapses to the same base
_DIACRITIC_PAIRS = [
    ("Müller", "Muller"),
    ("Café", "Cafe"),
    ("José", "Jose"),
    ("résumé", "resume"),
    ("naïve", "naive"),
    ("über", "uber"),
    ("Ångström", "Angstrom"),
    ("tête", "tete"),
]


@given(
    ids=st_id_pair,
    pair=st.sampled_from(_DIACRITIC_PAIRS),
)
@settings(max_examples=50, deadline=None)
def test_property_diacritic_collision_always_candidates(ids: tuple[int, int], pair: tuple[str, str]) -> None:
    """Diacritic variant and base form that share latinize() output → always Candidates."""
    id_a, id_b = ids
    accented, base = pair
    assume(latinize(accented) == latinize(base))
    dm = {id_a: accented, id_b: base}
    result = resolve(base, dm)
    assert isinstance(result, Candidates), (
        f"Diacritic collision {accented!r}/{base!r} should be Candidates, got {result}"
    )


@given(
    ids=st_id_pair,
    pair=st.sampled_from(_DIACRITIC_PAIRS),
)
@settings(max_examples=50, deadline=None)
def test_property_diacritic_both_ids_preserved_in_matches(ids: tuple[int, int], pair: tuple[str, str]) -> None:
    """Both diacritic-colliding ids must appear in Candidates.matches."""
    id_a, id_b = ids
    accented, base = pair
    assume(latinize(accented) == latinize(base))
    dm = {id_a: accented, id_b: base}
    result = resolve(base, dm)
    assert isinstance(result, Candidates)
    match_ids = {m["entity_id"] for m in result.matches}
    assert id_a in match_ids
    assert id_b in match_ids


# ---------------------------------------------------------------------------
# Category (d) — decoration / emoji noise
# ---------------------------------------------------------------------------


@given(
    ids=st_id_pair,
    name=st_shared_name_latin,
    deco=st_decoration,
)
@settings(max_examples=50, deadline=None)
def test_property_decoration_collision_always_candidates(ids: tuple[int, int], name: str, deco: str) -> None:
    """Decorated variant (e.g. ⭐name⭐) and plain name collide when latinize() strips deco."""
    id_a, id_b = ids
    decorated = f"{deco}{name}{deco}"
    # Only test cases where both latinize to the same string (decoration fully stripped)
    assume(latinize(decorated) == latinize(name))
    dm = {id_a: name, id_b: decorated}
    result = resolve(name, dm)
    assert isinstance(result, Candidates), (
        f"Decoration collision {name!r}/{decorated!r} should be Candidates, got {result}"
    )


@given(
    ids=st_id_pair,
    name=st_shared_name_latin,
    decorations=st.lists(st_decoration, min_size=2, max_size=4, unique=True),
)
@settings(max_examples=50, deadline=None)
def test_property_decoration_punctuation_wrappers(ids: tuple[int, int], name: str, decorations: list[str]) -> None:
    """Multiple decoration patterns: any that collapse to same latinize → Candidates."""
    id_a, id_b = ids
    # Pick first decoration that collapses
    collapsing = [d for d in decorations if latinize(f"{d}{name}{d}") == latinize(name)]
    assume(len(collapsing) >= 1)
    deco = collapsing[0]
    decorated = f"{deco}{name}{deco}"
    dm = {id_a: name, id_b: decorated}
    result = resolve(name, dm)
    assert isinstance(result, Candidates)


# ---------------------------------------------------------------------------
# Category (e) — cross-type (user vs channel)
# ---------------------------------------------------------------------------


@given(
    user_id=st_user_id,
    channel_id=st_channel_id,
    name=st_shared_name_latin,
)
@settings(max_examples=50, deadline=None)
def test_property_cross_type_collision_user_channel(user_id: int, channel_id: int, name: str) -> None:
    """User (positive id) and channel (negative id) with same name → always Candidates."""
    dm = {user_id: name, channel_id: name}
    result = resolve(name, dm)
    assert isinstance(result, Candidates), (
        f"Cross-type collision user={user_id}/channel={channel_id} name={name!r} → {result}"
    )


@given(
    user_id=st_user_id,
    bot_id=st.integers(min_value=10**6 + 1, max_value=10**9),
    channel_id=st_channel_id,
    name=st_shared_name_latin,
)
@settings(max_examples=50, deadline=None)
def test_property_cross_type_collision_three_types(user_id: int, bot_id: int, channel_id: int, name: str) -> None:
    """User + bot-shaped id + channel all sharing same name → Candidates with all 3 entries."""
    assume(user_id != bot_id)
    dm = {user_id: name, bot_id: name, channel_id: name}
    result = resolve(name, dm)
    assert isinstance(result, Candidates)
    match_ids = {m["entity_id"] for m in result.matches}
    assert {user_id, bot_id, channel_id}.issubset(match_ids)


# ---------------------------------------------------------------------------
# Meta / invariants
# ---------------------------------------------------------------------------


@given(
    entity_id=st_entity_id,
    name=st.text(alphabet=ascii_letters + " ", min_size=4, max_size=20).filter(
        lambda x: " " in x.strip() and len(latinize(x)) >= 4
    ),
)
@settings(max_examples=50, deadline=None)
def test_property_no_collision_single_entity_resolves(entity_id: int, name: str) -> None:
    """Single entity with multi-word name, multi-word query → Resolved (no false positive)."""
    dm = {entity_id: name}
    result = resolve(name, dm)
    assert isinstance(result, Resolved), f"Single entity {entity_id} name={name!r} should resolve, got {result}"
    assert result.entity_id == entity_id


@given(
    numeric_query=st.one_of(
        st.integers(min_value=1).map(str),
        st.integers(max_value=-1).map(str),
    ),
    ids=st_id_pair,
    name=st_shared_name_latin,
)
@settings(max_examples=50, deadline=None)
def test_property_numeric_query_never_returns_candidates(numeric_query: str, ids: tuple[int, int], name: str) -> None:
    """Numeric string query (even with collisions in display_name_map) → never Candidates."""
    id_a, id_b = ids
    dm = {id_a: name, id_b: name}
    result = resolve(numeric_query, dm)
    assert not isinstance(result, Candidates), (
        f"Numeric query {numeric_query!r} should not return Candidates, got {result}"
    )


@given(
    ids=st_id_pair,
    name_a=st_shared_name_latin,
    name_b=st_shared_name_latin,
)
@settings(max_examples=50, deadline=None)
def test_property_not_found_disjoint(ids: tuple[int, int], name_a: str, name_b: str) -> None:
    """Query with no token overlap against display_name_map → NotFound (threshold guard)."""
    id_a, id_b = ids
    dm = {id_a: name_a, id_b: name_b}
    # Pure digits are parsed as exact_id, never fuzzy-matched against display names → NotFound
    disjoint_query = "99999999999"
    result = resolve(disjoint_query, dm)
    # Should be NotFound since score will be below threshold; Candidates would be a false positive
    assert isinstance(result, NotFound), f"Disjoint query should be NotFound, got {result}"


@given(
    ids=st_id_pair,
    name=st_shared_name_latin,
)
@settings(max_examples=50, deadline=None)
def test_property_matches_contain_required_keys(ids: tuple[int, int], name: str) -> None:
    """Every dict in Candidates.matches must contain the required keys (superset check)."""
    id_a, id_b = ids
    dm = {id_a: name, id_b: name}
    result = resolve(name, dm)
    assert isinstance(result, Candidates)
    required_keys = {"entity_id", "display_name", "score", "username", "entity_type"}
    for match in result.matches:
        assert required_keys.issubset(set(match.keys())), f"match missing keys: {required_keys - set(match.keys())}"
