import json
from datetime import UTC, datetime
from typing import cast

import pytest
from telethon.tl import types as tl

from mcp_telegram.messages.telegram_adapter import extract_message_row
from mcp_telegram.telegram_message_projection import MessageLike, message_to_dict
from mcp_telegram.todo_content import TodoContent, todo_content


def test_real_todo_message_preserves_full_fact_and_semantic_text() -> None:
    media = tl.MessageMediaToDo(
        tl.TodoList(
            tl.TextWithEntities("😀 Plan", [tl.MessageEntityBold(3, 4)]),
            [tl.TodoItem(7, tl.TextWithEntities("First", [])), tl.TodoItem(42, tl.TextWithEntities("Второй", []))],
            others_can_append=True,
            others_can_complete=True,
        ),
        [tl.TodoCompletion(42, tl.PeerChannel(9), datetime(2026, 1, 1, tzinfo=UTC))],
    )
    message = tl.Message(1, tl.PeerUser(5), date=datetime(2026, 1, 2, tzinfo=UTC), message="", media=media)
    stored = extract_message_row(5, message).message
    assert stored.media_kind == "todo"
    assert json.loads(stored.media_payload) == {
        "_": "MessageMediaToDo",
        "todo": {
            "_": "TodoList",
            "title": {
                "_": "TextWithEntities",
                "text": "😀 Plan",
                "entities": [{"_": "MessageEntityBold", "offset": 3, "length": 4}],
            },
            "list": [
                {"_": "TodoItem", "id": 7, "title": {"_": "TextWithEntities", "text": "First", "entities": []}},
                {"_": "TodoItem", "id": 42, "title": {"_": "TextWithEntities", "text": "Второй", "entities": []}},
            ],
            "others_can_append": True,
            "others_can_complete": True,
        },
        "completions": [
            {
                "_": "TodoCompletion",
                "id": 42,
                "completed_by": {"_": "PeerChannel", "channel_id": 9},
                "date": "2026-01-01T00:00:00+00:00",
            }
        ],
    }
    assert stored.text == "😀 Plan\nFirst\nВторой"
    assert stored.formatting_text == ""
    assert todo_content(json.loads(stored.media_payload)) == TodoContent(stored.text, "captured")
    assert message_to_dict(cast(MessageLike, message), dialog_id=5)["text"] == stored.text


@pytest.mark.parametrize(
    ("peer", "expected_peer"),
    [
        (tl.PeerUser(1), {"_": "PeerUser", "user_id": 1}),
        (tl.PeerChat(2), {"_": "PeerChat", "chat_id": 2}),
        (tl.PeerChannel(3), {"_": "PeerChannel", "channel_id": 3}),
    ],
)
def test_completion_peer_and_date_are_retained(peer: object, expected_peer: dict[str, object]) -> None:
    media = tl.MessageMediaToDo(
        tl.TodoList(tl.TextWithEntities("List", []), [tl.TodoItem(42, tl.TextWithEntities("Task", []))]),
        [tl.TodoCompletion(42, peer, datetime(2026, 1, 1, tzinfo=UTC))],
    )
    stored = extract_message_row(5, tl.Message(1, tl.PeerUser(5), message="", media=media)).message
    assert json.loads(stored.media_payload)["completions"] == [
        {"_": "TodoCompletion", "id": 42, "completed_by": expected_peer, "date": "2026-01-01T00:00:00+00:00"}
    ]


def test_empty_and_unknown_checklists_are_distinct() -> None:
    media = tl.MessageMediaToDo(tl.TodoList(tl.TextWithEntities("List", []), []))
    message = tl.Message(2, tl.PeerChat(5), message="", media=media)
    stored = extract_message_row(-5, message).message
    payload = json.loads(stored.media_payload)
    assert payload["completions"] == []
    assert todo_content(payload) == TodoContent("List", "captured")
    assert todo_content({}) == TodoContent("", "unknown")
    payload["todo"]["list"] = [
        {"_": "FutureItem"},
        {"_": "TodoItem", "title": {"_": "TextWithEntities", "text": "kept"}},
    ]
    assert todo_content(payload) == TodoContent("List\nkept", "unsupported")


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (None, TodoContent("", "unknown")),
        ({"_": "FutureMedia"}, TodoContent("", "unsupported")),
        ({"_": "MessageMediaToDo"}, TodoContent("", "unknown")),
        ({"_": "MessageMediaToDo", "todo": {"_": "FutureList"}}, TodoContent("", "unsupported")),
        ({"_": "MessageMediaToDo", "todo": {"_": "TodoList"}}, TodoContent("", "unknown")),
        (
            {"_": "MessageMediaToDo", "todo": {"_": "TodoList", "title": {"_": "FutureTitle"}, "list": [None]}},
            TodoContent("", "unsupported"),
        ),
        (
            {"_": "MessageMediaToDo", "todo": {"_": "TodoList", "title": {"_": "TextWithEntities"}, "list": [None]}},
            TodoContent("", "unknown"),
        ),
    ],
)
def test_incomplete_and_future_checklists(payload: object, expected: TodoContent) -> None:
    assert todo_content(payload) == expected
