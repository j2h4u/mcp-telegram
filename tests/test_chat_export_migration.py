"""Offline migration preserves history and is deterministic and non-destructive."""

import json
from pathlib import Path

import pytest
from devtools.migrate_chat_export import migrate_export

from mcp_telegram.chat_export_checkpoint import ORDER, census


def test_migration_preserves_incremental_base_and_repeats_identically(tmp_path: Path) -> None:
    source = tmp_path / "original.json"
    data = {
        "format_version": 1,
        "group": {"dialog_id": "-1001"},
        "metadata": {"order": ORDER, "peers": [{"dialog_id": "-1001"}]},
        "admin_events": [{"dialog_id": "-1001", "event_id": "7", "action": {"unique": "keep"}}],
        "messages": [
            {
                "dialog_id": "-1001",
                "message_id": "5",
                "message_key": "-1001:5",
                "author_id": "42",
                "author_kind": "user",
                "text": "Keep",
                "metadata": {
                    "from_id": {"_": "PeerUser", "user_id": 42},
                    "from_rank": "Public label",
                    "unique": {"nested": [1, 2]},
                },
                "reactors": [],
            }
        ],
        "export": {"messages": 1, "admin_events": 1, "reactors": 0},
    }
    source.write_text(json.dumps(data))
    before = source.read_bytes()
    old_info = census(source, 100)
    output = tmp_path / "migrated.json"
    assert migrate_export(source, output) == {"from_version": 1, "to_version": 5}
    assert source.read_bytes() == before
    assert census(output, 100) == {**old_info, "format_version": 5}
    migrated = json.loads(output.read_text())
    data["format_version"] = 5
    del data["messages"][0]["metadata"]["from_id"]
    del data["messages"][0]["message_key"]
    del data["messages"][0]["author_id"]
    del data["messages"][0]["author_kind"]
    del data["messages"][0]["reactors"]
    data["messages"][0]["author"] = 0
    data["identities"] = [{"id": "42", "kind": "user"}]
    assert migrated == data
    repeated = tmp_path / "repeated.json"
    assert migrate_export(output, repeated) == {"from_version": 5, "to_version": 5}
    assert repeated.read_bytes() == output.read_bytes()
    with pytest.raises(FileExistsError):
        migrate_export(source, output)
    with pytest.raises(ValueError):
        migrate_export(source, source)
    assert source.read_bytes() == before
    data["messages"][0]["text"] = {"invalid": "type"}
    source.write_text(json.dumps(data))
    invalid = source.read_bytes()
    rejected = tmp_path / "rejected.json"
    with pytest.raises(ValueError, match="Invalid export record"):
        migrate_export(source, rejected)
    assert source.read_bytes() == invalid
    assert not rejected.exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_v4_sparse_migration_preserves_values_and_array_positions(tmp_path: Path) -> None:
    original = {
        "format_version": 4,
        "group": {"dialog_id": "-1", "title": None},
        "metadata": {"order": ORDER, "peers": [{"dialog_id": "-1"}]},
        "identities": [{"id": "7", "role": "member", "metadata": {}, "username": None}],
        "admin_events": [],
        "messages": [
            {
                "dialog_id": "-1",
                "message_id": "2",
                "author": 0,
                "text": "",
                "reactors": [],
                "entities": [],
                "topic": None,
                "service_action": None,
                "metadata": {
                    "views": 0,
                    "pinned": False,
                    "unused": None,
                    "positions": [None, {}, [], {"empty": None, "flag": False}],
                },
            }
        ],
        "export": {"messages": 1, "admin_events": 0, "reactors": 0},
    }
    source = tmp_path / "v4.json"
    output = tmp_path / "v5.json"
    source.write_text(json.dumps(original))
    before = source.read_bytes()
    migrate_export(source, output)
    result = json.loads(output.read_text())
    assert source.read_bytes() == before
    assert result["format_version"] == 5
    assert result["group"] == {"dialog_id": "-1"}
    assert result["identities"] == [{"id": "7", "role": "member"}]
    assert result["messages"][0] == {
        "dialog_id": "-1",
        "message_id": "2",
        "author": 0,
        "text": "",
        "metadata": {"views": 0, "pinned": False, "positions": [None, {}, [], {"flag": False}]},
    }
    repeat = tmp_path / "again.json"
    migrate_export(output, repeat)
    assert output.read_bytes() == repeat.read_bytes()
