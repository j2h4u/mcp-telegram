from __future__ import annotations

import pytest

from mcp_telegram.resolver import Candidates, NotFound, Resolved, resolve


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
