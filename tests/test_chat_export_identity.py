"""The v4 wire directory stores each exact identity snapshot once."""

import io
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from mcp_telegram.chat_export_checkpoint import ORDER
from mcp_telegram.chat_export_identity import IdentityIndex, expand_record, pack_record, write_export
from mcp_telegram.chat_export_schema import CURRENT_FORMAT_VERSION, migrate_record


def _identity(prefix: str, *, rank: str = "member") -> dict[str, object]:
    return {
        prefix + "id": "5",
        prefix + "kind": "user",
        prefix + "name": "Alice",
        prefix + "role": "member",
        prefix + "rank": rank,
        prefix + "metadata": {"source": "telegram"},
    }


def _records() -> list[tuple[str, dict[str, object]]]:
    group = {"dialog_id": "-1", "title": "Group"}
    same = {
        "id": "5",
        "kind": "user",
        "name": "Alice",
        "role": "member",
        "rank": "member",
        "metadata": {"source": "telegram"},
    }
    reactor = {
        **_identity("actor_"),
        "reaction": {"emoji": "👍"},
        "date": "2026-10-01",
        "raw": {"_": "MessagePeerReaction", "big": True},
    }
    event = {
        "_": "MessagePeerReaction",
        "big": True,
        "peer_id": {"_": "PeerUser", "user_id": 5},
        "reaction": {"emoji": "👍"},
        "date": "2026-10-01",
    }
    first = {
        "dialog_id": "-1",
        "message_id": "2",
        **_identity("author_"),
        "related_users": [same],
        "reactors": [reactor],
        "reactions": {"aggregate": {"recent_reactions": [event, {**event, "custom": True}]}},
    }
    second = {"dialog_id": "-1", "message_id": "1", **_identity("author_", rank="owner"), "reactors": []}
    return [
        ("group", group),
        ("metadata", {"order": ORDER, "peers": [group]}),
        ("admin_events.item", {"dialog_id": "-1", "event_id": "4", **_identity("actor_")}),
        ("messages.item", first),
        ("messages.item", second),
        ("export", {"messages": 2, "admin_events": 1, "reactors": 1}),
    ]


def test_wire_export_deduplicates_exact_snapshots_and_roundtrips() -> None:
    rows = _records()
    stream = io.StringIO()
    write_export(lambda: iter(rows), stream)
    wire = json.loads(stream.getvalue())
    assert wire["format_version"] == CURRENT_FORMAT_VERSION == 4
    assert len(wire["identities"]) == 2
    assert "author_id" not in wire["messages"][0]
    assert "actor_id" not in wire["admin_events"][0]
    assert "actor_id" not in wire["messages"][0]["reactors"][0]
    assert wire["messages"][0]["author"] == wire["messages"][0]["related_users"][0]
    assert wire["admin_events"][0]["actor"] == wire["messages"][0]["author"]
    assert wire["messages"][0]["reactors"][0]["actor"] == wire["messages"][0]["author"]
    assert wire["messages"][0]["reactions"]["aggregate"]["recent_reactions"] == [
        {"reactor": 0},
        {
            "_": "MessagePeerReaction",
            "big": True,
            "peer_id": {"_": "PeerUser", "user_id": 5},
            "reaction": {"emoji": "👍"},
            "date": "2026-10-01",
            "custom": True,
        },
    ]
    with IdentityIndex() as index:
        for position, identity in enumerate(wire["identities"]):
            index.put_at(position, identity)
        expanded_admin = expand_record("admin_events.item", wire["admin_events"][0], index)
        expanded_message = expand_record("messages.item", wire["messages"][0], index)
        assert expanded_admin["actor_name"] == "Alice"
        assert expanded_message["author_rank"] == "member"
        assert expanded_message["related_users"] == [_identity("")]
        assert expanded_message["reactions"]["aggregate"]["recent_reactions"][0] == {
            "_": "MessagePeerReaction",
            "big": True,
            "peer_id": {"_": "PeerUser", "user_id": 5},
            "reaction": {"emoji": "👍"},
            "date": "2026-10-01",
        }
        assert migrate_record(4, "messages.item", expanded_message, internal=True) == expanded_message


def test_distinct_metadata_and_null_presence_remain_distinct() -> None:
    with IdentityIndex() as index:
        first = pack_record("messages.item", {"author_rank": None}, index)
        second = pack_record("messages.item", {}, index)
        third = pack_record("messages.item", {"author_rank": "member"}, index)
        assert first["author"] != second.get("author")
        assert third["author"] != first["author"]
        assert index.count() == 2


def test_duplicate_public_directory_slots_and_empty_related_identity_are_legal() -> None:
    with IdentityIndex() as index:
        index.put_at(0, {})
        index.put_at(1, {})
        assert index.get(0) == index.get(1) == {}
        packed = pack_record("messages.item", {"related_users": [{}], "reactors": []}, index)
        assert packed["related_users"] == [0]
        assert expand_record("messages.item", packed, index)["related_users"] == [{}]


def test_expansion_rejects_boolean_and_out_of_range_references() -> None:
    with IdentityIndex() as index:
        index.put_at(0, {"id": "5"})
        for reference in (True, 1):
            with pytest.raises(ValueError, match="[Ii]dentity reference"):
                expand_record("messages.item", {"author": reference}, index)


@pytest.mark.parametrize("flat", [{"author": 0}, {"author": 0, "author_id": "5"}])
def test_flat_identity_reserved_reference_never_overwrites_source(flat: dict[str, object]) -> None:
    with IdentityIndex() as index, pytest.raises(ValueError, match="reserved"):
        pack_record("messages.item", flat, index)


def test_publication_rejects_changed_identity_and_misordered_records() -> None:
    rows = _records()
    passes = 0

    def changing() -> Iterator[tuple[str, dict[str, object]]]:
        nonlocal passes
        passes += 1
        for kind, record in rows:
            if passes == 2 and kind == "messages.item":
                record = {**record, "author_name": "Changed"}
            yield kind, record

    with pytest.raises(ValueError, match="changed between"):
        write_export(changing, io.StringIO())
    wrong = [*rows[:3], rows[3], rows[2], *rows[4:]]
    with pytest.raises(ValueError, match="order"):
        write_export(lambda: iter(wrong), io.StringIO())


def test_reader_rejects_nonobject_directory_and_mixed_identity_fields(tmp_path: Path) -> None:
    from mcp_telegram.chat_export_checkpoint import base_records
    from mcp_telegram.chat_export_schema import validate_record

    stream = io.StringIO()
    write_export(lambda: iter(_records()), stream)
    wire = json.loads(stream.getvalue())
    wire["identities"][0] = None
    path = tmp_path / "malformed.json"
    path.write_text(json.dumps(wire))
    with pytest.raises(ValueError, match="objects"):
        list(base_records(path))
    with pytest.raises(ValueError):
        validate_record(
            "messages.item", {"dialog_id": "-1", "message_id": "1", "reactors": [], "author_unknown": "fact"}
        )
    with IdentityIndex() as index, pytest.raises(ValueError, match="reserved"):
        pack_record(
            "messages.item", {"reactors": [], "reactions": {"aggregate": {"recent_reactions": [{"reactor": 0}]}}}, index
        )
