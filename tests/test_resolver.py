from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from mcp_telegram.resolver import Candidates, NotFound, Resolved, resolve
from mcp_telegram.cache import EntityCache


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
    # Both "Ivan Petrov" and "Ivan's Team Chat" start with "Ivan" — both should score >=90 with WRatio
    choices = {201: "Ivan Petrov", 202: "Ivan's Team Chat"}
    result = resolve("Ivan", choices)
    assert isinstance(result, Candidates)
    assert result.query == "Ivan"
    assert len(result.matches) >= 2


def test_sender_resolution(sample_entities: dict) -> None:
    # Sender resolution uses the same resolve() with {sender_id: name} dict — no separate code path
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
    # A query that should score < 60 against all choices
    result = resolve("qqqqzzzz", sample_entities)
    assert isinstance(result, NotFound)
    assert result.query == "qqqqzzzz"


def test_single_low_score_match_returns_candidates() -> None:
    """Single candidate in 60-89 range → now returns Candidates (NEW behavior).

    This was previously auto-resolved, but with the redesign, all fuzzy matches
    return Candidates to ensure agent disambiguation.
    """
    choices = {101: "Sergei Khabarov"}
    result = resolve("сергей", choices)  # Cyrillic, transliterates to "sergey", scores ~81
    assert isinstance(result, Candidates)
    assert result.query == "sergey"  # After transliteration, the query is transliterated
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


### NEW TESTS FOR REDESIGNED RESOLVER ###

def test_numeric_id_in_cache_resolves() -> None:
    """Test case 1: Numeric ID query exists in cache → Resolved."""
    choices = {12345: "Alice", 67890: "Bob"}
    result = resolve("12345", choices)
    assert isinstance(result, Resolved)
    assert result.entity_id == 12345
    assert result.display_name == "Alice"


def test_numeric_id_not_found() -> None:
    """Test case 2: Numeric ID query not in cache → NotFound."""
    choices = {12345: "Alice"}
    result = resolve("99999", choices)
    assert isinstance(result, NotFound)
    assert result.query == "99999"


def test_username_query_resolves_via_cache(mock_cache: EntityCache) -> None:
    """Test case 3: @username query exists in cache → Resolved."""
    choices = {101: "Иван Петров", 102: "Anna"}
    # Cache already has entity 101 with username "ivan" from fixture
    result = resolve("@ivan", choices, cache=mock_cache)
    assert isinstance(result, Resolved)
    assert result.entity_id == 101
    assert result.display_name == "Иван Петров"


def test_username_query_not_found(mock_cache: EntityCache) -> None:
    """Test case 4: @username query not in cache → NotFound."""
    choices = {101: "Иван Петров"}
    result = resolve("@notfound", choices, cache=mock_cache)
    assert isinstance(result, NotFound)
    assert result.query == "@notfound"


def test_exact_match_case_insensitive() -> None:
    """Test case 5: Exact case-insensitive match → Resolved."""
    choices = {101: "Bob", 102: "Bobby"}
    result = resolve("bob", choices)  # Lowercase input
    assert isinstance(result, Resolved)
    assert result.entity_id == 101
    assert result.display_name == "Bob"


def test_single_fuzzy_match_returns_candidates() -> None:
    """Test case 6: Single fuzzy match score=92 → Candidates (NOT Resolved).

    Even a single good match (>=90 score) should return Candidates for disambiguation.
    """
    choices = {101: "Sergei Khabarov"}
    result = resolve("Sergei Khabar", choices)  # Latin typo, scores ~92
    assert isinstance(result, Candidates)
    # Query is preserved as provided
    assert result.query == "Sergei Khabar"
    assert len(result.matches) >= 1
    # Verify match structure
    match = result.matches[0]
    assert isinstance(match, dict)
    assert "entity_id" in match
    assert "display_name" in match
    assert "score" in match
    assert "username" in match
    assert "entity_type" in match
    assert match["entity_id"] == 101


def test_multiple_fuzzy_matches_returns_candidates() -> None:
    """Test case 7: Multiple fuzzy matches all >=60 → Candidates."""
    choices = {101: "Alice Smith", 102: "Alicia Jones", 103: "Alien"}
    result = resolve("Ali", choices)  # Ambiguous
    assert isinstance(result, Candidates)
    assert result.query == "Ali"
    assert len(result.matches) >= 2
    # All matches should be dicts with metadata
    for match in result.matches:
        assert isinstance(match, dict)
        assert "entity_id" in match
        assert "display_name" in match
        assert "score" in match


def test_no_fuzzy_matches_returns_not_found() -> None:
    """Test case 8: No matches >=60 → NotFound."""
    choices = {101: "Alice", 102: "Bob"}
    result = resolve("xyzzz", choices)  # No match
    assert isinstance(result, NotFound)
    assert result.query == "xyzzz"


def test_cyrillic_transliteration_still_works() -> None:
    """Test case 9: Cyrillic query with transliteration fallback (existing behavior preserved)."""
    choices = {101: "Sergei Khabarov"}
    result = resolve("сергей хабаров", choices)
    # Should work due to transliteration
    assert isinstance(result, (Resolved, Candidates))
    if isinstance(result, Resolved):
        assert result.entity_id == 101
    elif isinstance(result, Candidates):
        assert len(result.matches) >= 1
        assert result.matches[0]["entity_id"] == 101


def test_candidates_include_metadata_from_cache(mock_cache: EntityCache) -> None:
    """Verify Candidates include username and entity_type from cache."""
    choices = {101: "Иван Петров", 102: "Another User"}
    result = resolve("иван", choices, cache=mock_cache)
    assert isinstance(result, Candidates)
    # Entity 101 is in cache with username="ivan"
    match_101 = next((m for m in result.matches if m["entity_id"] == 101), None)
    assert match_101 is not None
    assert match_101["username"] == "ivan"
    assert match_101["entity_type"] == "user"


def test_candidates_without_cache_have_none_metadata() -> None:
    """Verify Candidates have None for username/entity_type when cache not provided."""
    choices = {101: "Sergei Khabarov", 102: "Sergei Ivanov"}
    result = resolve("сергей", choices, cache=None)
    assert isinstance(result, Candidates)
    for match in result.matches:
        assert match["username"] is None
        assert match["entity_type"] is None


def test_exact_match_among_fuzzy_returns_resolved() -> None:
    """Exact match among multiple fuzzy candidates → Resolved (exact priority)."""
    choices = {101: "Alice", 102: "Alicia", 103: "Alien"}
    result = resolve("Alice", choices)  # Exact case-insensitive match
    assert isinstance(result, Resolved)
    assert result.entity_id == 101


def test_resolve_without_cache_still_works() -> None:
    """Resolve should work without cache (cache=None)."""
    choices = {101: "Иван Петров", 102: "Anna"}
    # Numeric ID should work
    result = resolve("101", choices, cache=None)
    assert isinstance(result, Resolved)

    # Exact match should work
    result = resolve("Иван Петров", choices, cache=None)
    assert isinstance(result, Resolved)

    # Fuzzy should return Candidates without metadata
    result = resolve("иван", choices, cache=None)
    assert isinstance(result, Candidates)
