"""Versioned export validation and migration preserve incremental identifiers."""

import json
from pathlib import Path
from typing import cast

import pytest
from jsonschema import Draft202012Validator

from mcp_telegram import chat_export_cli as cli
from mcp_telegram.chat_export_checkpoint import ORDER, Checkpoint, census
from mcp_telegram.chat_export_schema import (
    CURRENT_FORMAT_VERSION,
    IDENTITY_FORMAT_VERSION,
    INTERNAL_FORMAT_VERSION,
    migrate_record,
    read_export_version,
    schema_for_version,
    validate_record,
)


def legacy_record() -> dict[str, object]:
    return {
        "dialog_id": "-1",
        "message_id": "5",
        "message_key": "-1:5",
        "reactors": [],
        "text": "Private source text",
        "metadata": {"peer_id": {"_": "PeerChat", "chat_id": 1}, "from_rank": "Keep"},
        "unique": {"keep": True},
    }


def document(record: dict[str, object], version: object = 1) -> dict[str, object]:
    return {
        "format_version": version,
        "group": {"dialog_id": "-1"},
        "metadata": {"order": ORDER, "peers": [{"dialog_id": "-1"}]},
        "admin_events": [],
        "messages": [record],
        "export": {"messages": 1, "admin_events": 0, "reactors": len(cast(list[object], record["reactors"]))},
    }


@pytest.mark.parametrize("version", [True, "1", 0, 6, None])
def test_future_or_malformed_version_rejected(tmp_path: Path, version: object) -> None:
    path = tmp_path / "base.json"
    path.write_text(json.dumps(document(legacy_record(), version)), encoding="utf-8")
    with pytest.raises(ValueError, match="format_version"):
        read_export_version(path)
    with pytest.raises(ValueError):
        census(path, 0)


@pytest.mark.parametrize(
    ("field", "value"),
    [("text", []), ("text", None), ("reactors", [42]), ("reactors", [{"actor_id": 42}]), ("entities", "bad")],
)
def test_known_fields_validated_without_private_error_values(field: str, value: object) -> None:
    record = legacy_record()
    record[field] = value
    with pytest.raises(ValueError) as error:
        validate_record("messages.item", record, internal=True)
    assert field in str(error.value)
    assert "Private source text" not in str(error.value)


@pytest.mark.parametrize("value", [None, {}, []])
def test_v5_records_require_canonical_sparse_fields(value: object) -> None:
    record = legacy_record()
    record.pop("message_key")
    record["metadata"] = value
    with pytest.raises(ValueError):
        validate_record("messages.item", record, 5)


@pytest.mark.parametrize("version", [1, 2, 3])
def test_migration_versions_census_and_checkpoint(tmp_path: Path, version: int) -> None:
    original = legacy_record()
    if version == INTERNAL_FORMAT_VERSION:
        original.pop("message_key")
    migrated = migrate_record(version, "messages.item", original)
    assert "message_key" not in migrated
    assert migrated["metadata"] == ({"from_rank": "Keep"} if version == 1 else original["metadata"])
    assert migrated["unique"] == original["unique"]
    assert migrate_record(INTERNAL_FORMAT_VERSION, "messages.item", migrated) == migrated
    base = tmp_path / "base.json"
    base.write_text(json.dumps(document(original, version)), encoding="utf-8")
    original_bytes = base.read_bytes()
    info = census(base, 0)
    assert info["format_version"] == version
    assert info["boundaries"] == {"-1": 5}
    rebuilt = tmp_path / "new.json"
    rebuilt.write_text(json.dumps(document(migrated, INTERNAL_FORMAT_VERSION)), encoding="utf-8")
    assert {key: val for key, val in census(rebuilt, 0).items() if key != "format_version"} == {
        key: val for key, val in info.items() if key != "format_version"
    }
    assert base.read_bytes() == original_bytes


def test_old_resume_checkpoint_migrates_records(tmp_path: Path) -> None:
    checkpoint = Checkpoint(tmp_path / "old.sqlite3")
    try:
        checkpoint.db.execute("DELETE FROM state WHERE key='format_version'")
        checkpoint.db.commit()
        checkpoint.save("message", -1, 5, json.dumps(legacy_record()), "history:-1")
        restored = cast(dict[str, object], json.loads(next(checkpoint.records("message", -1))))
        assert restored["metadata"] == {"from_rank": "Keep"}
        assert checkpoint.state("history:-1") == 5
    finally:
        checkpoint.close()


@pytest.mark.parametrize(("field", "value"), [("", None), ("text", None), ("text", []), ("author_name", [])])
def test_current_checkpoint_preserves_json_and_rejects_malformed_empty_fields(
    tmp_path: Path, field: str, value: object
) -> None:
    record = migrate_record(1, "messages.item", legacy_record())
    if field:
        record[field] = value
    payload = json.dumps(record, indent=2)
    checkpoint = Checkpoint(tmp_path / "current.sqlite3")
    try:
        checkpoint.mark("format_version", INTERNAL_FORMAT_VERSION)
        checkpoint.save("message", -1, 5, payload, "history:-1")
        if field:
            with pytest.raises(ValueError, match=field):
                next(checkpoint.records("message", -1))
        else:
            assert next(checkpoint.records("message", -1)) == payload
    finally:
        checkpoint.close()


@pytest.mark.parametrize("version", [1, 2, INTERNAL_FORMAT_VERSION])
def test_fixed_schema_registry_and_legacy_string_reactions(version: int) -> None:
    record = legacy_record()
    record["reactors"] = [{"actor_id": "42", "reaction": "👍", "raw": {"my": True}}]
    schema = schema_for_version(version)
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(document(record, version))
    current_schema = schema_for_version(INTERNAL_FORMAT_VERSION)
    assert (
        cast(dict[str, dict[str, object]], current_schema["properties"])["format_version"]["const"]
        == INTERNAL_FORMAT_VERSION
    )
    assert (
        "message_key"
        not in cast(dict[str, dict[str, dict[str, dict[str, object]]]], current_schema["properties"])["messages"][
            "items"
        ]["properties"]
    )
    wire_schema = schema_for_version(IDENTITY_FORMAT_VERSION)
    assert "identities" in cast(list[str], wire_schema["required"])
    assert (
        "message_key"
        not in cast(dict[str, dict[str, dict[str, dict[str, object]]]], wire_schema["properties"])["messages"]["items"][
            "properties"
        ]
    )
    sparse_schema = schema_for_version(CURRENT_FORMAT_VERSION)
    assert cast(dict[str, dict[str, object]], sparse_schema["properties"])["format_version"]["const"] == 5
    assert (
        "reactors"
        not in cast(dict[str, dict[str, dict[str, list[str]]]], sparse_schema["properties"])["messages"]["items"][
            "required"
        ]
    )
    assert (
        "reactors"
        in cast(dict[str, dict[str, dict[str, list[str]]]], wire_schema["properties"])["messages"]["items"]["required"]
    )
    with pytest.raises(ValueError):
        migrate_record(CURRENT_FORMAT_VERSION, "messages.item", record)
    expanded = legacy_record()
    expanded.pop("message_key")
    assert migrate_record(IDENTITY_FORMAT_VERSION, "messages.item", expanded, internal=True) == expanded
    assert migrate_record(CURRENT_FORMAT_VERSION, "messages.item", expanded, internal=True) == expanded


@pytest.mark.asyncio
async def test_invalid_incremental_content_rejected_before_telegram(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = legacy_record()
    record["text"] = ["Private source text"]
    base = tmp_path / "bad.json"
    base.write_text(json.dumps(document(record)), encoding="utf-8")

    async def request(*args: object, **kwargs: object) -> dict[str, object]:
        pytest.fail("Invalid base must be rejected before Telegram RPC")

    monkeypatch.setattr(cli._Export, "request", request)
    with pytest.raises(ValueError, match="text"):
        await cli.export_group(-1, tmp_path / "new.json", update_from=base)
    assert not (tmp_path / "new.json").exists()
