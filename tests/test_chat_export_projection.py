"""The export keeps scalar analysis keys and full nontext Telegram metadata."""

from mcp_telegram.chat_export_projection import project_admin_event, project_message, project_reactor


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
    assert message["message_key"] == "-1001234567890:5"
    assert message["reply_key"] == "-1009876543210:4"
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
