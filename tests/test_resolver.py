from __future__ import annotations

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from mcp_telegram.resolver import (
    Candidates,
    NotFound,
    Resolved,
    ResolvedWithMessage,
    _parse_tme_link,
    latinize,
    resolve,
    resolve_dialog,
)
from mcp_telegram.cache import EntityCache


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
    """Multi-word 'Ольга Петрова' exact normalized match → Resolved."""
    choices = {1: "Olga Petrova", 2: "Ольга Петрова"}
    result = resolve("Ольга Петрова", choices)
    # Both normalize to "olga petrova", one should resolve
    assert isinstance(result, Resolved)


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


def test_username_query_resolves_via_cache(mock_cache: EntityCache) -> None:
    choices = {101: "Иван Петров", 102: "Anna"}
    result = resolve("@ivan", choices, cache=mock_cache)
    assert isinstance(result, Resolved)
    assert result.entity_id == 101
    assert result.display_name == "Иван Петров"


def test_username_query_not_found(mock_cache: EntityCache) -> None:
    choices = {101: "Иван Петров"}
    result = resolve("@notfound", choices, cache=mock_cache)
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


def test_candidates_include_metadata_from_cache(mock_cache: EntityCache) -> None:
    choices = {101: "Иван Петров", 102: "Another User"}
    result = resolve("иван", choices, cache=mock_cache)
    assert isinstance(result, Candidates)
    match_101 = next((m for m in result.matches if m["entity_id"] == 101), None)
    assert match_101 is not None
    assert match_101["username"] == "ivan"
    assert match_101["entity_type"] == "user"


def test_candidates_without_cache_have_none_metadata() -> None:
    choices = {101: "Sergei Khabarov", 102: "Sergei Ivanov"}
    result = resolve("сергей", choices, cache=None)
    assert isinstance(result, Candidates)
    for match in result.matches:
        assert match["username"] is None
        assert match["entity_type"] is None


def test_exact_match_among_fuzzy_returns_candidates_single_word() -> None:
    """Single-word 'Alice' with ≥2 hits → Candidates (single-word caution), exact first."""
    choices = {101: "Alice", 102: "Alicia", 103: "Alien"}
    result = resolve("Alice", choices)
    assert isinstance(result, Candidates)
    assert result.matches[0]["entity_id"] == 101  # exact match first


def test_resolve_without_cache_still_works() -> None:
    choices = {101: "Иван Петров", 102: "Anna"}
    result = resolve("101", choices, cache=None)
    assert isinstance(result, Resolved)

    result = resolve("Иван Петров", choices, cache=None)
    assert isinstance(result, Resolved)

    result = resolve("иван", choices, cache=None)
    assert isinstance(result, Candidates)


def test_normalized_choices_param() -> None:
    """Pre-computed normalized_choices are used instead of on-the-fly computation."""
    choices = {1: "Olga Petrova", 2: "Ольга"}
    normalized = {1: "olga petrova", 2: "olga"}
    result = resolve("Ольга Петрова", choices, normalized_choices=normalized)
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


@pytest.fixture()
def mock_client_factory(mock_client: AsyncMock):
    """Return a client factory that yields the mock_client."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def factory():
        yield mock_client

    return factory


@pytest.mark.asyncio
async def test_resolve_dialog_tme_link_via_api(mock_cache: EntityCache, mock_client_factory) -> None:
    """t.me link resolves via get_entity API call when not in cache."""
    mock_client = mock_client_factory
    # We need the actual client mock from the factory
    entity = SimpleNamespace(
        id=12345,
        first_name="vlbe",
        last_name=None,
        title="vlbe code",
        username="vlbecode",
    )

    # Patch the factory to set up get_entity
    from contextlib import asynccontextmanager

    client_mock = AsyncMock()
    client_mock.get_entity = AsyncMock(return_value=entity)

    @asynccontextmanager
    async def factory():
        yield client_mock

    result = await resolve_dialog("https://t.me/vlbecode/355", mock_cache, factory)
    assert isinstance(result, ResolvedWithMessage)
    assert result.entity_id == 12345
    assert result.message_id == 355
    assert result.display_name == "vlbe code"


@pytest.mark.asyncio
async def test_resolve_dialog_username_cache_hit(mock_cache: EntityCache, mock_client_factory) -> None:
    """@username resolves from cache without API call."""
    result = await resolve_dialog("@ivan", mock_cache, mock_client_factory)
    assert isinstance(result, Resolved)
    assert result.entity_id == 101
    assert result.display_name == "Иван Петров"


@pytest.mark.asyncio
async def test_resolve_dialog_username_api_fallback(mock_cache: EntityCache) -> None:
    """@username not in cache falls back to API."""
    from contextlib import asynccontextmanager

    entity = SimpleNamespace(
        id=999,
        first_name="New",
        last_name="User",
        title=None,
        username="newuser",
    )
    client_mock = AsyncMock()
    client_mock.get_entity = AsyncMock(return_value=entity)

    @asynccontextmanager
    async def factory():
        yield client_mock

    result = await resolve_dialog("@newuser", mock_cache, factory)
    assert isinstance(result, Resolved)
    assert result.entity_id == 999
    assert result.display_name == "New User"


@pytest.mark.asyncio
async def test_resolve_dialog_fuzzy_from_cache(mock_cache: EntityCache, mock_client_factory) -> None:
    """Fuzzy name resolves from cache without warmup."""
    result = await resolve_dialog("Иван Петров", mock_cache, mock_client_factory)
    assert isinstance(result, Resolved)
    assert result.entity_id == 101
