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
    assert migrate_export(source, output) == {"from_version": 1, "to_version": 4}
    assert source.read_bytes() == before
    assert census(output, 100) == {**old_info, "format_version": 4}
    migrated = json.loads(output.read_text())
    data["format_version"] = 4
    del data["messages"][0]["metadata"]["from_id"]
    del data["messages"][0]["message_key"]
    del data["messages"][0]["author_id"]
    del data["messages"][0]["author_kind"]
    data["messages"][0]["author"] = 0
    data["identities"] = [{"id": "42", "kind": "user"}]
    assert migrated == data
    repeated = tmp_path / "repeated.json"
    assert migrate_export(output, repeated) == {"from_version": 4, "to_version": 4}
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
