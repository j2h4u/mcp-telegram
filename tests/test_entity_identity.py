from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from mcp_telegram.entity_identity import ENTITY_IDENTITY_SCHEMA, project_entity_identity


def test_username_identity_is_canonical_and_excludes_numeric_fallback() -> None:
    identity = project_entity_identity(display_name=" Alice ", username="@alice", telegram_id=42)

    assert identity == {"display_name": "Alice", "username": "@alice"}
    assert "telegram_id" not in identity


def test_numeric_identity_is_the_only_fallback_without_username() -> None:
    identity = project_entity_identity(display_name="Alice", username=None, telegram_id=42)

    assert identity == {"display_name": "Alice", "telegram_id": 42}
    assert "username" not in identity


def test_identity_schema_rejects_both_arms_and_accepts_each_contract_arm() -> None:
    validator = Draft202012Validator(ENTITY_IDENTITY_SCHEMA)
    validator.validate({"display_name": "Alice", "username": "@alice"})
    validator.validate({"display_name": "Alice", "telegram_id": 42})
    assert not validator.is_valid({"display_name": "Alice", "username": "@alice", "telegram_id": 42})
    assert not validator.is_valid({"display_name": "Alice"})


def test_identity_schema_is_json_serializable() -> None:
    json.dumps(ENTITY_IDENTITY_SCHEMA)
