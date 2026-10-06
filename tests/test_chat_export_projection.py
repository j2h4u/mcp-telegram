"""The export retains unique Telegram facts while removing confirmed duplicates."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from mcp_telegram.chat_export_checkpoint import ORDER, Checkpoint, census, fingerprint
from mcp_telegram.chat_export_projection import (
    compact_export_record,
    deduplicate_export_message,
    project_admin_event,
    project_message,
    project_reactor,
)


def test_pandas_projection_preserves_content_and_lossless_keys() -> None:
    author = {
        "id": 9007199254740993,
        "kind": "user",
        "display_name": "Alice",
        "username": "@alice",
        "is_admin": True,
        "role": "admin",
        "rank": "Moderator",
        "identity_source": "local_cache",
    }
    message = project_message(
        {
            "id": 5,
            "dialog_id": -1001234567890,
            "kind": "message",
            "date": "2026-10-03",
            "author": author,
            "topic_id": 3,
            "reply_to": {"message_id": 4, "peer": {"id": -1009876543210, "kind": "channel"}},
            "raw": {
                "message": "**Full text**",
                "edit_date": "2026-10-04",
                "grouped_id": 9007199254740995,
                "entities": [{"_": "MessageEntityBold", "offset": 0}],
                "media": {"_": "MessageMediaPhoto", "photo": {"id": 42}},
                "reply_to": {"quote_text": "Quoted text"},
            },
        }
    )
    assert message["author_id"] == "9007199254740993"
    assert message["author_rank"] == "Moderator"
    assert "message_key" not in message
    assert "reply_key" not in message
    assert message["topic_id"] == "3"
    assert message["grouped_id"] == "9007199254740995"
    assert message["text"] == "**Full text**"
    assert message["edited_at"] == "2026-10-04"
    assert message["entities"] == [{"_": "MessageEntityBold", "offset": 0}]
    assert message["metadata"] == {
        "media": {"_": "MessageMediaPhoto", "photo": {"id": 42}},
        "reply_to": {"quote_text": "Quoted text"},
    }
    assert message["author_metadata"] == {}
    reactor = project_reactor({"peer": author, "reaction": {"emoji": "👍"}, "date": "2026-10-03"})
    assert reactor["actor_id"] == "9007199254740993"
    assert reactor["actor_name"] == "Alice"
    assert reactor["reaction"] == {"emoji": "👍"}
    service = project_message(
        {
            "id": 6,
            "dialog_id": -1001234567890,
            "kind": "service",
            "raw": {"action": {"_": "MessageActionChatAddUser", "users": [2]}},
        }
    )
    assert service["service_action"] == {"_": "MessageActionChatAddUser", "users": [2]}
    admin = project_admin_event(
        {
            "id": 8,
            "date": "2026-10-03",
            "actor": author,
            "source": "telegram_admin_log",
            "action": {"new_participant": {"user_id": 2}},
        },
        -1001234567890,
    )
    assert admin["event_id"] == "8"
    assert admin["dialog_id"] == "-1001234567890"
    assert admin["actor_role"] == "admin"
    assert admin["action"] == {"new_participant": {"user_id": 2}}
    assert "source" not in admin


def test_message_duplicates_removed_without_losing_raw_facts() -> None:
    aggregate = {"_": "MessageReactions", "results": [{"count": 2}], "min": False}
    source = {
        "id": 5,
        "dialog_id": -1001234567890,
        "author": {"id": 42, "kind": "user", "rank": None},
        "reply_to": {"message_id": 4, "peer": {"id": -987, "kind": "chat"}},
        "reactions": {"aggregate": aggregate},
        "raw": {
            "_": "Message",
            "from_id": {"_": "PeerUser", "user_id": 42},
            "peer_id": {"_": "PeerChannel", "channel_id": 1234567890},
            "from_rank": "Original rank",
            "reactions": aggregate,
            "reply_to": {
                "_": "MessageReplyHeader",
                "reply_to_msg_id": 4,
                "reply_to_peer_id": {"_": "PeerChat", "chat_id": 987},
                "reply_to_top_id": 2,
                "quote_text": "Original quote",
                "quote_entities": [{"_": "MessageEntityBold", "offset": 0, "length": 5}],
                "reply_from": {"date": "2026-10-03"},
            },
        },
    }
    original = deepcopy(source)
    message = project_message(source)
    assert message["metadata"] == {
        "_": "Message",
        "from_rank": "Original rank",
        "reply_to": {
            "_": "MessageReplyHeader",
            "reply_to_top_id": 2,
            "quote_text": "Original quote",
            "quote_entities": [{"_": "MessageEntityBold", "offset": 0, "length": 5}],
            "reply_from": {"date": "2026-10-03"},
        },
    }
    assert message["author_id"] == "42"
    assert message["author_rank"] is None
    assert message["reply_to_message_id"] == "4"
    assert message["reply_to_dialog_id"] == "-987"
    assert source == original


def test_message_mismatches_and_missing_canonical_facts_retained() -> None:
    raw = {
        "from_id": {"_": "PeerUser", "user_id": 43},
        "peer_id": {"_": "PeerChat", "chat_id": 124},
        "reactions": {"results": [{"count": 2}]},
        "reply_to": {"reply_to_msg_id": 8, "reply_to_peer_id": {"_": "PeerChat", "chat_id": 99}},
    }
    message = {
        "dialog_id": -123,
        "author": {"id": 42, "kind": "user"},
        "reply_to": {"message_id": 4, "peer": {"id": -987, "kind": "chat"}},
        "reactions": {"aggregate": {"results": [{"count": 3}]}},
        "raw": raw,
    }
    assert project_message(message)["metadata"] == raw
    assert project_message({"raw": raw})["metadata"] == raw


def test_v3_compaction_rejects_conflicting_generated_keys_and_preserves_unique_values() -> None:
    record = {"dialog_id": "-1", "message_id": "5", "message_key": "wrong", "metadata": {"keep": 1}}
    with pytest.raises(ValueError, match="message_key"):
        compact_export_record("messages.item", record)
    assert record["message_key"] == "wrong"
    assert compact_export_record("messages.item", {"dialog_id": "-1", "message_id": "5", "message_key": "-1:5"}) == {
        "dialog_id": "-1",
        "message_id": "5",
    }


def test_v3_compaction_removes_only_equal_identity_aliases() -> None:
    record = {
        "kind": "message",
        "dialog_id": "-1",
        "message_id": "5",
        "message_key": "-1:5",
        "reply_to_dialog_id": None,
        "reply_to_message_id": None,
        "reply_key": None,
        "author_rank": "Equal",
        "author_metadata": {"label": "Equal", "other": "keep"},
        "metadata": {"_": "Message", "from_rank": "Different", "unique": True},
        "topic_id": "7",
        "topic": {"id": "7", "title": "Keep"},
        "related_users": [{"rank": "R", "metadata": {"label": "R", "extra": 1}}],
        "reactors": [{"actor_rank": "A", "actor_metadata": {"label": "A", "raw": True}}],
    }
    original = deepcopy(record)
    compacted = compact_export_record("messages.item", record)
    assert "message_key" not in compacted and "reply_key" not in compacted
    assert compacted["metadata"] == {"from_rank": "Different", "unique": True}
    assert compacted["author_metadata"] == {"other": "keep"}
    assert compacted["topic"] == {"title": "Keep"}
    assert compacted["related_users"] == [{"rank": "R", "metadata": {"extra": 1}}]
    assert compacted["reactors"] == [{"actor_rank": "A", "actor_metadata": {"raw": True}}]
    assert compact_export_record("messages.item", compacted) == compacted
    assert record == original


def test_v3_compaction_removes_present_equal_null_aliases_only() -> None:
    record = {
        "author_rank": None,
        "author_metadata": {"label": None},
        "metadata": {"from_rank": None},
        "topic_id": None,
        "topic": {"id": None},
    }
    compacted = compact_export_record("messages.item", record)
    assert compacted == {
        "author_rank": None,
        "author_metadata": {},
        "metadata": {},
        "topic_id": None,
        "topic": {},
    }
    missing = compact_export_record("messages.item", {"author_rank": None, "author_metadata": {}})
    assert missing == {"author_rank": None, "author_metadata": {}}


@pytest.mark.parametrize(
    ("kind", "identifier", "peer"),
    [
        ("user", "42", {"_": "PeerUser", "user_id": 42}),
        ("chat", "-987", {"_": "PeerChat", "chat_id": 987}),
        ("channel", "-1001234567890", {"_": "PeerChannel", "channel_id": 1234567890}),
    ],
)
def test_reactor_duplicates_removed_and_flags_preserved(kind: str, identifier: str, peer: dict[str, object]) -> None:
    flags = {"_": "MessagePeerReaction", "big": True, "my": False, "unread": True}
    reaction = {"_": "ReactionEmoji", "emoticon": "👍"}
    source = {
        "peer": {"id": identifier, "kind": kind},
        "reaction": reaction,
        "date": "2026-10-03",
        "raw": {**flags, "peer_id": peer, "reaction": reaction, "date": "2026-10-03"},
    }
    original = deepcopy(source)
    reactor = project_reactor(source)
    assert reactor["raw"] == flags
    assert reactor["actor_id"] == identifier
    assert reactor["reaction"] == reaction
    assert reactor["date"] == "2026-10-03"
    assert source == original


def test_reactor_mismatches_unknown_peer_fields_and_absent_facts_retained() -> None:
    raw = {
        "peer_id": {"_": "PeerUser", "user_id": 42, "extra": True},
        "reaction": {"_": "ReactionEmoji", "emoticon": "❤️"},
        "date": "2026-10-02",
    }
    reactor = {"peer": {"id": 42, "kind": "user"}, "reaction": {}, "date": "2026-10-03", "raw": raw}
    assert project_reactor(reactor)["raw"] == raw
    assert project_reactor({"raw": raw})["raw"] == raw
    raw["peer_id"] = {"_": "PeerChat", "chat_id": 42}
    assert project_reactor(reactor)["raw"] == raw


def test_existing_v1_deduplication_is_idempotent_and_keeps_incremental_cursors(tmp_path: Path) -> None:
    aggregate = {"results": [{"count": 2}]}
    peer = {"_": "PeerUser", "user_id": 42}
    reactor = {
        "actor_id": "42",
        "actor_kind": "user",
        "reaction": {"emoticon": "👍"},
        "date": "2026-10-03",
        "raw": {"_": "MessagePeerReaction", "big": True, "peer_id": peer, "reaction": {"emoticon": "👍"}},
    }
    record = {
        "dialog_id": "-1001",
        "message_id": "5",
        "message_key": "-1001:5",
        "author_id": "42",
        "author_kind": "user",
        "author_rank": None,
        "reply_to_dialog_id": "-1001",
        "reply_to_message_id": "4",
        "reply_key": "-1001:4",
        "metadata": {
            "from_id": peer,
            "from_rank": "Original rank",
            "peer_id": {"_": "PeerChat", "chat_id": 1001},
            "reactions": aggregate,
            "reply_to": {"reply_to_msg_id": 4, "quote_text": "Keep"},
        },
        "reactions": {"aggregate": aggregate},
        "reactors": [reactor],
        "unknown_future_field": {"keep": True},
    }
    original = deepcopy(record)
    cleaned = deduplicate_export_message(record)
    assert cleaned["metadata"] == {"from_rank": "Original rank", "reply_to": {"quote_text": "Keep"}}
    assert cleaned["reactors"] == [{**reactor, "raw": {"_": "MessagePeerReaction", "big": True}}]
    assert cleaned["unknown_future_field"] == {"keep": True}
    assert deduplicate_export_message(cleaned) == cleaned
    assert record == original
    doc = {
        "format_version": 1,
        "group": {"dialog_id": "-1001"},
        "metadata": {"order": ORDER, "peers": [{"dialog_id": "-1001"}]},
        "admin_events": [],
        "messages": [record],
        "export": {"messages": 1, "admin_events": 0, "reactors": 1},
    }
    base = tmp_path / "base.json"
    base.write_text(json.dumps(doc), encoding="utf-8")
    original_bytes = base.read_bytes()
    before = census(base, 0)
    doc["messages"] = [cleaned]
    rebuilt = tmp_path / "rebuilt.json"
    rebuilt.write_text(json.dumps(doc), encoding="utf-8")
    assert census(rebuilt, 0) == before
    checkpoint = Checkpoint(tmp_path / "checkpoint.sqlite3")
    try:
        checkpoint.mark("base_fingerprint", fingerprint(base))
        checkpoint.import_base(base, before)
        restored = [json.loads(item) for item in checkpoint.records("message", -1001)]
        assert restored[0]["message_id"] == cleaned["message_id"]
        assert "message_key" not in restored[0]
    finally:
        checkpoint.close()
    assert base.read_bytes() == original_bytes
