import dataclasses
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

import pytest
from jsonschema import validate
from telethon.tl.types import PeerChannel, PeerChat, PeerUser, ReactionCustomEmoji, ReactionEmoji, ReactionPaid

from mcp_telegram.message_view import MESSAGE_VIEW_SCHEMA, project_message_view
from mcp_telegram.reading.query_records import read_message_from_row
from mcp_telegram.telegram_message_projection import MessageLike, message_to_dict
from mcp_telegram.tools.reading import _list_messages_structured_messages, _read_messages_from_rows


def test_uncached_rich_facts_reach_existing_message_view() -> None:
    msg = SimpleNamespace(
        id=5,
        date=None,
        edit_date=None,
        message="caption",
        media=None,
        out=False,
        sender_id=1,
        sender=None,
        reply_to=None,
        entities=None,
        action=None,
        reactions=SimpleNamespace(
            results=[
                SimpleNamespace(reaction=ReactionEmoji(emoticon="👍"), count=2),
                SimpleNamespace(reaction=ReactionCustomEmoji(document_id=123), count=3),
                SimpleNamespace(reaction=ReactionPaid(), count=4),
            ]
        ),
        fwd_from=SimpleNamespace(from_name="Original author"),
        post_author="Channel signature",
    )
    row = message_to_dict(cast(MessageLike, msg), dialog_id=1)
    view = project_message_view(_read_messages_from_rows([row])[0])
    assert row["reactions_display"] == "[paid×4 custom:123×3 👍×2]"
    assert view["forward"] == {"from_name": "Original author"}
    assert view["post_author"] == "Channel signature"
    assert cast(dict[str, object], view["reactions"])["display"] == row["reactions_display"]
    validate(view, MESSAGE_VIEW_SCHEMA)


@pytest.mark.parametrize("has_actors", [False, True])
def test_uncached_recent_actors_survive_wire_and_reading_projection(has_actors: bool) -> None:
    msg = SimpleNamespace(
        id=5,
        date=None,
        edit_date=None,
        message="caption",
        media=None,
        out=False,
        sender_id=1,
        sender=None,
        reply_to=None,
        entities=None,
        action=None,
        reactions=SimpleNamespace(
            results=[],
            recent_reactions=[
                SimpleNamespace(
                    peer_id=PeerUser(7), reaction=ReactionEmoji("👍"), date=datetime.fromtimestamp(250, UTC)
                ),
                SimpleNamespace(peer_id=PeerChat(8), reaction=ReactionCustomEmoji(123), date=None),
                SimpleNamespace(peer_id=PeerChannel(9), reaction=ReactionPaid(), date=None),
            ]
            if has_actors
            else [],
        ),
    )
    row = cast(dict[str, object], json.loads(json.dumps(message_to_dict(cast(MessageLike, msg), dialog_id=1))))
    expected = (
        [
            {"reactor_id": 7, "emoji": "👍", "reacted_at": 250},
            {"reactor_id": -8, "emoji": "custom:123"},
            {"reactor_id": -1000000000009, "emoji": "paid"},
        ]
        if has_actors
        else []
    )
    status = "partial" if has_actors else "unavailable"
    view = _list_messages_structured_messages([row])[0]
    assert view["reaction_events"] == expected
    assert view["reaction_events_status"] == status
    decoded = read_message_from_row({**row, "formatting_entities": json.dumps(row["formatting_entities"])})
    decoded_wire = cast(dict[str, object], json.loads(json.dumps(dataclasses.asdict(decoded))))
    decoded_view = _list_messages_structured_messages([decoded_wire])[0]
    assert decoded_view["reaction_events"] == expected
    assert decoded_view["reaction_events_status"] == status
    validate(project_message_view(decoded), MESSAGE_VIEW_SCHEMA)
