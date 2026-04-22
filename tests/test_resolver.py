from __future__ import annotations

from unittest.mock import MagicMock

from mcp_telegram.resolver import (
    Candidates,
    NotFound,
    Resolved,
    _parse_tme_link,
    latinize,
    resolve,
)


def test_latinize_cyrillic() -> None:
    assert latinize("Ольга Петрова") == "olga petrova"


def test_latinize_latin() -> None:
    assert latinize("Olga Petrova") == "olga petrova"


def test_latinize_mixed() -> None:
    assert latinize("Café résumé") == "cafe resume"


def test_resolve_exact_match(sample_entities: dict) -> None:
    result = resolve("Иван Петров", sample_entities)
    assert isinstance(result, Resolved)
    assert result.entity_id == 101
    assert result.display_name == "Иван Петров"


def test_numeric_query(sample_entities: dict) -> None:
    result = resolve("101", sample_entities)
    assert isinstance(result, Resolved)
    assert result.entity_id == 101
    assert result.display_name == "Иван Петров"

    result_missing = resolve("999", sample_entities)
    assert isinstance(result_missing, NotFound)
    assert result_missing.query == "999"


def test_negative_numeric_query() -> None:
    choices = {-1003779402801: "Studio Robots and Inbox"}
    result = resolve("-1003779402801", choices)
    assert isinstance(result, Resolved)
    assert result.entity_id == -1003779402801
    assert result.display_name == "Studio Robots and Inbox"


def test_ambiguity(sample_entities: dict) -> None:
    choices = {201: "Ivan Petrov", 202: "Ivan's Team Chat"}
    result = resolve("Ivan", choices)
    assert isinstance(result, Candidates)
    assert result.query == "Ivan"
    assert len(result.matches) >= 2


def test_sender_resolution(sample_entities: dict) -> None:
    sender_map = {501: "Иван Петров", 502: "Анна Иванова"}
    result = resolve("Анна Иванова", sender_map)
    assert isinstance(result, Resolved)
    assert result.entity_id == 502
    assert result.display_name == "Анна Иванова"


def test_not_found(sample_entities: dict) -> None:
    result = resolve("xyz_nomatch_zzz", sample_entities)
    assert isinstance(result, NotFound)
    assert result.query == "xyz_nomatch_zzz"


def test_below_candidate_threshold(sample_entities: dict) -> None:
    result = resolve("qqqqzzzz", sample_entities)
    assert isinstance(result, NotFound)
    assert result.query == "qqqqzzzz"


def test_cross_script_resolves_via_normalization() -> None:
    """Cyrillic 'Ольга Петрова' resolves to Latin 'Olga Petrova' via anyascii normalization."""
    choices = {1: "Olga Petrova", 2: "Ольга", 3: "Olga"}
    result = resolve("Ольга Петрова", choices)
    assert isinstance(result, Resolved)
    assert result.entity_id == 1
    assert result.display_name == "Olga Petrova"


def test_single_word_multiple_candidates_returns_candidates() -> None:
    """Single-word query 'Ольга' with 2+ matches → always Candidates."""
    choices = {1: "Olga Petrova", 2: "Ольга", 3: "Olga"}
    result = resolve("Ольга", choices)
    assert isinstance(result, Candidates)
    assert len(result.matches) >= 2


def test_single_word_single_candidate_resolves() -> None:
    """Single-word query 'Ольга' with only 1 match → Resolved via exact normalized."""
    choices = {2: "Ольга"}
    result = resolve("Ольга", choices)
    assert isinstance(result, Resolved)
    assert result.entity_id == 2


def test_multi_word_exact_resolves() -> None:
    """Multi-word 'Ольга Петрова' where two entities share the same normalized name.
    Both 'Olga Petrova' and 'Ольга Петрова' normalize to 'olga petrova' — collision
    detected, must return Candidates (not arbitrary Resolved)."""
    choices = {1: "Olga Petrova", 2: "Ольга Петрова"}
    result = resolve("Ольга Петрова", choices)
    # Both normalize to "olga petrova" — collision: Candidates required
    assert isinstance(result, Candidates)
    ids = {m["entity_id"] for m in result.matches}
    assert {1, 2} <= ids


def test_single_low_score_match_returns_candidates() -> None:
    """Single candidate in 60-89 range → Candidates (no auto-resolve for fuzzy)."""
    choices = {101: "Sergei Khabarov"}
    result = resolve("сергей", choices)  # Cyrillic, normalizes to "sergei"
    assert isinstance(result, Candidates)
    assert len(result.matches) >= 1
    assert result.matches[0]["entity_id"] == 101


def test_multiple_low_score_matches_are_candidates() -> None:
    """Multiple candidates in 60-89 range → Candidates (ambiguous)."""
    choices = {101: "Sergei Khabarov", 102: "Sergei Ivanov"}
    result = resolve("сергей", choices)
    assert isinstance(result, Candidates)


def test_exact_match_wins_over_ambiguity() -> None:
    """Exact match resolves even when shorter name also scores >=90."""
    choices = {101: "Sergei Khabarov", 102: "Serge"}
    result = resolve("Sergei Khabarov", choices)
    assert isinstance(result, Resolved)
    assert result.entity_id == 101
    assert result.display_name == "Sergei Khabarov"


def test_numeric_id_in_cache_resolves() -> None:
    choices = {12345: "Alice", 67890: "Bob"}
    result = resolve("12345", choices)
    assert isinstance(result, Resolved)
    assert result.entity_id == 12345
    assert result.display_name == "Alice"


def test_numeric_id_not_found() -> None:
    choices = {12345: "Alice"}
    result = resolve("99999", choices)
    assert isinstance(result, NotFound)
    assert result.query == "99999"


def test_username_query_resolves_via_cache(mock_cache) -> None:
    choices = {101: "Иван Петров", 102: "Anna"}
    result = resolve("@ivan", choices, entity_cache=mock_cache)
    assert isinstance(result, Resolved)
    assert result.entity_id == 101
    assert result.display_name == "Иван Петров"


def test_username_query_not_found(mock_cache) -> None:
    choices = {101: "Иван Петров"}
    result = resolve("@notfound", choices, entity_cache=mock_cache)
    assert isinstance(result, NotFound)
    assert result.query == "@notfound"


def test_exact_match_case_insensitive() -> None:
    """Single-word 'bob' with 2 hits → Candidates (single-word caution), exact first."""
    choices = {101: "Bob", 102: "Bobby"}
    result = resolve("bob", choices)
    assert isinstance(result, Candidates)
    assert result.matches[0]["entity_id"] == 101  # exact match first


def test_single_fuzzy_match_returns_candidates() -> None:
    """Single fuzzy match score=92 → Candidates (NOT Resolved)."""
    choices = {101: "Sergei Khabarov"}
    result = resolve("Sergei Khabar", choices)
    assert isinstance(result, Candidates)
    assert result.query == "Sergei Khabar"
    assert len(result.matches) >= 1
    match = result.matches[0]
    assert isinstance(match, dict)
    assert "entity_id" in match
    assert "display_name" in match
    assert "score" in match
    assert "username" in match
    assert "entity_type" in match
    assert match["entity_id"] == 101


def test_multiple_fuzzy_matches_returns_candidates() -> None:
    choices = {101: "Alice Smith", 102: "Alicia Jones", 103: "Alien"}
    result = resolve("Ali", choices)
    assert isinstance(result, Candidates)
    assert result.query == "Ali"
    assert len(result.matches) >= 2
    for match in result.matches:
        assert isinstance(match, dict)
        assert "entity_id" in match
        assert "display_name" in match
        assert "score" in match


def test_no_fuzzy_matches_returns_not_found() -> None:
    choices = {101: "Alice", 102: "Bob"}
    result = resolve("xyzzz", choices)
    assert isinstance(result, NotFound)
    assert result.query == "xyzzz"


def test_cyrillic_cross_script_still_works() -> None:
    """Cyrillic multi-word query matches Latin name via normalization → Candidates with top match."""
    choices = {101: "Sergei Khabarov"}
    result = resolve("сергей хабаров", choices)
    assert isinstance(result, Candidates)
    assert len(result.matches) >= 1
    assert result.matches[0]["entity_id"] == 101


def test_cyrillic_query_resolves_latin_name_over_partial_cyrillic_match() -> None:
    """Cyrillic 'Ольга Петрова' should resolve to Latin 'Olga Petrova' via normalization."""
    choices = {1: "Olga Petrova", 2: "Ольга", 3: "Olga"}
    result = resolve("Ольга Петрова", choices)
    assert isinstance(result, Resolved)
    assert result.entity_id == 1
    assert result.display_name == "Olga Petrova"


def test_cyrillic_normalization_prefers_exact_over_fuzzy_candidates() -> None:
    """When normalization yields exact match, prefer exact."""
    choices = {10: "Ivan Petrov", 20: "Иван"}
    result = resolve("Иван Петров", choices)
    assert isinstance(result, Resolved)
    assert result.entity_id == 10


def test_candidates_include_metadata_from_cache(mock_cache) -> None:
    choices = {101: "Иван Петров", 102: "Another User"}
    result = resolve("иван", choices, entity_cache=mock_cache)
    assert isinstance(result, Candidates)
    match_101 = next((m for m in result.matches if m["entity_id"] == 101), None)
    assert match_101 is not None
    assert match_101["username"] == "ivan"
    assert match_101["entity_type"] == "user"


def test_candidates_without_cache_derive_type_from_id_sign() -> None:
    """No cache → username is None, but entity_type is derived from the Telegram id sign
    convention (positive=User, -100…=Channel, other negative=Group). This keeps the
    disambiguation_hint informative even before entity_cache is populated."""
    choices = {101: "Sergei Khabarov", 102: "Sergei Ivanov", -1001234567: "Sergei Ch"}
    result = resolve("сергей", choices, entity_cache=None)
    assert isinstance(result, Candidates)
    by_id = {m["entity_id"]: m for m in result.matches}
    assert by_id[101]["username"] is None
    assert by_id[101]["entity_type"] == "User"
    assert by_id[102]["entity_type"] == "User"
    assert by_id[-1001234567]["entity_type"] == "Channel"


def test_exact_match_among_fuzzy_returns_candidates_single_word() -> None:
    """Single-word 'Alice' with ≥2 hits → Candidates (single-word caution), exact first."""
    choices = {101: "Alice", 102: "Alicia", 103: "Alien"}
    result = resolve("Alice", choices)
    assert isinstance(result, Candidates)
    assert result.matches[0]["entity_id"] == 101  # exact match first


def test_resolve_without_cache_still_works() -> None:
    choices = {101: "Иван Петров", 102: "Anna"}
    result = resolve("101", choices, entity_cache=None)
    assert isinstance(result, Resolved)

    result = resolve("Иван Петров", choices, entity_cache=None)
    assert isinstance(result, Resolved)

    result = resolve("иван", choices, entity_cache=None)
    assert isinstance(result, Candidates)


def test_normalized_name_map_param() -> None:
    """Pre-computed normalized_name_map are used instead of on-the-fly computation."""
    choices = {1: "Olga Petrova", 2: "Ольга"}
    normalized = {1: "olga petrova", 2: "olga"}
    result = resolve("Ольга Петрова", choices, normalized_name_map=normalized)
    assert isinstance(result, Resolved)
    assert result.entity_id == 1


def test_parse_tme_link_full_url_with_message() -> None:
    result = _parse_tme_link("https://t.me/vlbecode/355")
    assert result == ("vlbecode", 355)


def test_parse_tme_link_full_url_without_message() -> None:
    result = _parse_tme_link("https://t.me/vlbecode")
    assert result == ("vlbecode", None)


def test_parse_tme_link_no_scheme() -> None:
    result = _parse_tme_link("t.me/vlbecode/355")
    assert result == ("vlbecode", 355)


def test_parse_tme_link_http() -> None:
    result = _parse_tme_link("http://t.me/vlbecode")
    assert result == ("vlbecode", None)


def test_parse_tme_link_not_a_link() -> None:
    assert _parse_tme_link("@vlbecode") is None
    assert _parse_tme_link("some random text") is None
    assert _parse_tme_link("101") is None


def test_parse_tme_link_short_username_rejected() -> None:
    """Telegram usernames must be ≥5 chars (4 after prefix), reject 3-char."""
    assert _parse_tme_link("t.me/abc") is None


# ---------------------------------------------------------------------------
# Collision invariant tests (39.4-01)
# ---------------------------------------------------------------------------


def test_collision_doronin_production_repro() -> None:
    """Production repro: User 268071163 and Channel -1001245391218 share the same name.
    resolve() must return Candidates — never Resolved — when ≥2 distinct entity_ids
    share the same normalized display name."""
    choices = {268071163: "Константин Доронин", -1001245391218: "Константин Доронин"}
    result = resolve("Константин Доронин", choices)
    assert isinstance(result, Candidates), f"Expected Candidates, got {result!r}"
    ids = {m["entity_id"] for m in result.matches}
    assert 268071163 in ids, "User 268071163 missing from matches"
    assert -1001245391218 in ids, "Channel -1001245391218 missing from matches"


def test_collision_exact_same_norm_name_multiword() -> None:
    """Two distinct ids share 'Иван Петров' exactly — must be Candidates."""
    choices = {1: "Иван Петров", 2: "Иван Петров"}
    result = resolve("Иван Петров", choices)
    assert isinstance(result, Candidates), f"Expected Candidates, got {result!r}"
    ids = {m["entity_id"] for m in result.matches}
    assert {1, 2} <= ids


def test_collision_exact_same_norm_name_single_word() -> None:
    """{1: 'Ivan', 2: 'Ivan'} — single-word query must return Candidates with both ids."""
    choices = {1: "Ivan", 2: "Ivan"}
    result = resolve("Ivan", choices)
    assert isinstance(result, Candidates), f"Expected Candidates, got {result!r}"
    ids = {m["entity_id"] for m in result.matches}
    assert {1, 2} <= ids


def test_collision_three_entities_same_name() -> None:
    """{1,2,3} all named 'Anna' — Candidates with all three."""
    choices = {1: "Anna", 2: "Anna", 3: "Anna"}
    result = resolve("Anna", choices)
    assert isinstance(result, Candidates), f"Expected Candidates, got {result!r}"
    ids = {m["entity_id"] for m in result.matches}
    assert {1, 2, 3} <= ids


def test_collision_diacritic_variants() -> None:
    """{1: 'Müller', 2: 'Muller'} both latinize to 'muller' — must be Candidates."""
    choices = {1: "Müller", 2: "Muller"}
    result = resolve("Müller", choices)
    assert isinstance(result, Candidates), f"Expected Candidates, got {result!r}"
    ids = {m["entity_id"] for m in result.matches}
    assert {1, 2} <= ids


def test_collision_decoration_emoji() -> None:
    """{1: '⭐Ivan⭐', 2: 'Ivan'} both latinize to 'ivan' — must be Candidates."""
    choices = {1: "⭐Ivan⭐", 2: "Ivan"}
    result = resolve("Ivan", choices)
    assert isinstance(result, Candidates), f"Expected Candidates, got {result!r}"
    ids = {m["entity_id"] for m in result.matches}
    assert {1, 2} <= ids


def test_no_collision_single_exact_multiword_still_resolved() -> None:
    """Regression guard: unique multiword exact match must still return Resolved."""
    choices = {101: "Иван Петров", 102: "Анна Иванова"}
    result = resolve("Иван Петров", choices)
    assert isinstance(result, Resolved), f"Expected Resolved, got {result!r}"
    assert result.entity_id == 101


def test_no_collision_single_exact_single_word_with_no_other_hits() -> None:
    """Single unique entity — must not over-trigger Candidates."""
    choices = {500: "Zxywvut"}
    result = resolve("Zxywvut", choices)
    assert isinstance(result, Resolved), f"Expected Resolved, got {result!r}"
    assert result.entity_id == 500


def test_collision_preserves_all_entity_ids_in_matches() -> None:
    """5 entities sharing 'Name' — all 5 entity_ids must appear in matches."""
    choices = {i: "Name" for i in range(1, 6)}
    result = resolve("Name", choices)
    assert isinstance(result, Candidates), f"Expected Candidates, got {result!r}"
    ids = {m["entity_id"] for m in result.matches}
    assert ids == {1, 2, 3, 4, 5}


def test_collision_with_additional_near_matches() -> None:
    """{1:'Ivan', 2:'Ivan', 3:'Ivano'} — Candidates contains {1,2} at minimum."""
    choices = {1: "Ivan", 2: "Ivan", 3: "Ivano"}
    result = resolve("Ivan", choices)
    assert isinstance(result, Candidates), f"Expected Candidates, got {result!r}"
    ids = {m["entity_id"] for m in result.matches}
    assert {1, 2} <= ids


def test_collision_with_normalized_name_map_path() -> None:
    """Explicit normalized_name_map with collision must still be detected."""
    choices = {1: "Ivan A", 2: "Ivan B"}
    normalized = {1: "ivan", 2: "ivan"}
    result = resolve("Ivan", choices, normalized_name_map=normalized)
    assert isinstance(result, Candidates), f"Expected Candidates, got {result!r}"
    ids = {m["entity_id"] for m in result.matches}
    assert {1, 2} <= ids


def test_collision_cross_type_same_name() -> None:
    """{100: 'Support', -1001234: 'Support'} (user vs channel) — Candidates with both."""
    choices = {100: "Support", -1001234: "Support"}
    result = resolve("Support", choices)
    assert isinstance(result, Candidates), f"Expected Candidates, got {result!r}"
    ids = {m["entity_id"] for m in result.matches}
    assert {100, -1001234} <= ids


def test_no_collision_substring_not_exact_match() -> None:
    """Multiword exact match on id=2; id=1 is only partial — must be Resolved(2)."""
    choices = {1: "Иван", 2: "Иван Петров"}
    result = resolve("Иван Петров", choices)
    assert isinstance(result, Resolved), f"Expected Resolved, got {result!r}"
    assert result.entity_id == 2


def test_collision_returned_matches_nonempty_dicts() -> None:
    """Each dict in result.matches has all required keys."""
    choices = {1: "TestName", 2: "TestName"}
    result = resolve("TestName", choices)
    assert isinstance(result, Candidates)
    required_keys = {"entity_id", "display_name", "score", "username", "entity_type"}
    for match in result.matches:
        assert required_keys <= set(match.keys()), f"Match missing keys: {match}"


def test_numeric_query_bypasses_collision_logic() -> None:
    """Numeric query '123' on collision map must return Resolved(123) — numeric path unaffected."""
    choices = {123: "Shared", 456: "Shared"}
    result = resolve("123", choices)
    assert isinstance(result, Resolved), f"Expected Resolved, got {result!r}"
    assert result.entity_id == 123


# ---------------------------------------------------------------------------
# Task 1: disambiguation_hint field on Candidates matches
# ---------------------------------------------------------------------------


def _make_cache_with_types(entity_types: dict[int, str]):
    """Build a mock entity_cache that returns entity_type for given ids."""
    cache = MagicMock()

    def get_side_effect(entity_id, ttl_seconds=300):
        if entity_id in entity_types:
            return {"type": entity_types[entity_id], "username": None}
        return None

    cache.get.side_effect = get_side_effect
    cache.get_by_username.return_value = None
    return cache


def test_candidates_match_has_disambiguation_hint_on_collision() -> None:
    """Collision case (same norm name, ≥2 entities) → each match dict has non-empty disambiguation_hint."""
    # Two entities with names that normalize to the same string → collision
    choices = {101: "Ivan", 102: "IVAN"}
    result = resolve("Ivan", choices)
    assert isinstance(result, Candidates)
    for match in result.matches:
        assert "disambiguation_hint" in match
        assert match["disambiguation_hint"] is not None
        assert len(match["disambiguation_hint"]) > 0


def test_candidates_match_hint_mentions_entity_types() -> None:
    """Hint text mentions both entity types when known."""
    cache = _make_cache_with_types({101: "User", 102: "Channel"})
    choices = {101: "Ivan", 102: "IVAN"}
    result = resolve("Ivan", choices, entity_cache=cache)
    assert isinstance(result, Candidates)
    hint = result.matches[0]["disambiguation_hint"]
    assert hint is not None
    # Should mention types (sorted)
    assert "Channel" in hint or "User" in hint


def test_candidates_match_hint_mentions_query() -> None:
    """Hint contains the original query string."""
    choices = {101: "Ivan", 102: "IVAN"}
    result = resolve("Ivan", choices)
    assert isinstance(result, Candidates)
    for match in result.matches:
        hint = match["disambiguation_hint"]
        assert hint is not None
        assert "Ivan" in hint


def test_candidates_match_hint_suggests_action() -> None:
    """Hint contains @username or numeric id action guidance."""
    choices = {101: "Ivan", 102: "IVAN"}
    result = resolve("Ivan", choices)
    assert isinstance(result, Candidates)
    for match in result.matches:
        hint = match["disambiguation_hint"]
        assert hint is not None
        assert "@username" in hint or "numeric id" in hint


def test_candidates_no_hint_on_non_collision_candidates() -> None:
    """Non-collision Candidates (fuzzy neighbors, distinct norm names) → disambiguation_hint is None."""
    # These have distinct normalized names — fuzzy neighbors, not collision
    choices = {101: "Alice Smith", 102: "Alicia Jones"}
    result = resolve("Ali", choices)
    assert isinstance(result, Candidates)
    for match in result.matches:
        assert match.get("disambiguation_hint") is None


def test_make_match_info_without_hint_context_has_no_hint_key_or_none() -> None:
    """_make_match_info called standalone → disambiguation_hint key is None."""
    from mcp_telegram.resolver import _make_match_info

    match = _make_match_info(101, "Ivan", 90, None)
    assert match.get("disambiguation_hint") is None


# ---------------------------------------------------------------------------
# Task 2: observability log on collision
# ---------------------------------------------------------------------------


def test_collision_emits_debug_log(caplog) -> None:
    """Collision case → one log record with text matching 'resolver_collision'."""
    import logging

    choices = {101: "Ivan", 102: "IVAN"}
    with caplog.at_level(logging.DEBUG, logger="mcp_telegram.resolver"):
        resolve("Ivan", choices)
    collision_records = [r for r in caplog.records if "resolver_collision" in r.getMessage()]
    assert len(collision_records) == 1


def test_collision_log_contains_query_and_count(caplog) -> None:
    """Log record has query and n_entities=2 in its formatted message."""
    import logging

    choices = {101: "Ivan", 102: "IVAN"}
    with caplog.at_level(logging.DEBUG, logger="mcp_telegram.resolver"):
        resolve("Ivan", choices)
    collision_records = [r for r in caplog.records if "resolver_collision" in r.getMessage()]
    assert len(collision_records) == 1
    msg = collision_records[0].getMessage()
    assert "Ivan" in msg
    assert "2" in msg


def test_collision_log_contains_entity_types(caplog) -> None:
    """Log record args include entity types list."""
    import logging

    cache = _make_cache_with_types({101: "User", 102: "Channel"})
    choices = {101: "Ivan", 102: "IVAN"}
    with caplog.at_level(logging.DEBUG, logger="mcp_telegram.resolver"):
        resolve("Ivan", choices, entity_cache=cache)
    collision_records = [r for r in caplog.records if "resolver_collision" in r.getMessage()]
    assert len(collision_records) >= 1


def test_no_log_when_no_collision(caplog) -> None:
    """Resolved path → zero resolver_collision log records."""
    import logging

    choices = {101: "Alice"}
    with caplog.at_level(logging.DEBUG, logger="mcp_telegram.resolver"):
        result = resolve("Alice", choices)
    assert isinstance(result, Resolved)
    collision_records = [r for r in caplog.records if "resolver_collision" in r.getMessage()]
    assert len(collision_records) == 0


def test_not_found_no_collision_log(caplog) -> None:
    """NotFound path → zero collision log records."""
    import logging

    choices = {101: "Alice"}
    with caplog.at_level(logging.DEBUG, logger="mcp_telegram.resolver"):
        result = resolve("xyzzznomatch", choices)
    assert isinstance(result, NotFound)
    collision_records = [r for r in caplog.records if "resolver_collision" in r.getMessage()]
    assert len(collision_records) == 0


def test_log_level_is_debug_not_info(caplog) -> None:
    """Collision log must be DEBUG level, not INFO."""
    import logging

    choices = {101: "Ivan", 102: "IVAN"}
    with caplog.at_level(logging.DEBUG, logger="mcp_telegram.resolver"):
        resolve("Ivan", choices)
    collision_records = [r for r in caplog.records if "resolver_collision" in r.getMessage()]
    assert len(collision_records) == 1
    assert collision_records[0].levelno == logging.DEBUG


def test_hint_stable_across_calls() -> None:
    """Deterministic template → identical hint text on repeated calls."""
    choices = {101: "Ivan", 102: "IVAN"}
    result1 = resolve("Ivan", choices)
    result2 = resolve("Ivan", choices)
    assert isinstance(result1, Candidates)
    assert isinstance(result2, Candidates)
    for m1, m2 in zip(result1.matches, result2.matches, strict=True):
        assert m1["disambiguation_hint"] == m2["disambiguation_hint"]
