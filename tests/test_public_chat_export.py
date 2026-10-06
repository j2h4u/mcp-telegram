import json
from pathlib import Path
from typing import cast

import pytest
from devtools.public_chat_export import sanitize_export
from ijson.common import IncompleteJSONError

from mcp_telegram.chat_export_checkpoint import census


def test_public_export_is_account_independent_atomic_and_idempotent(tmp_path: Path) -> None:
    outputs = []
    for account in (0, 1):
        path = tmp_path / f"account-{account}.json"
        data = {
            "format_version": 1,
            "group": {"dialog_id": "-1001", "title": "Group"},
            "metadata": {
                "order": "newest_to_oldest within each peer; primary then migrated predecessors",
                "peers": [{"dialog_id": "-1001"}],
            },
            "admin_events": [{"event_id": "1", "action": {"approved_by": account}}],
            "messages": [
                {
                    "dialog_id": "-1001",
                    "message_id": "1",
                    "message_key": "-1001:1",
                    "text": "Keep this",
                    "author_id": "123",
                    "author_rank": "Moderator",
                    "author_role": "former_member",
                    "entities": [{"_": "MessageEntityBold"}],
                    "reply_key": "-1001:2",
                    "service_action": {"_": "MessageActionPinMessage"},
                    "metadata": {
                        "out": bool(account),
                        "mentioned": bool(account),
                        "replies": {"read_max_id": account, "replies": 2},
                        "media": {
                            "can_view_stats": bool(account),
                            "has_unread_votes": bool(account),
                            "poll": {"question": "Keep question", "public_voters": False},
                            "results": {"results": [{"option": "a", "voters": 5, "chosen": bool(account)}]},
                            "access_hash": account,
                        },
                    },
                    "reactions": {"aggregate": {"results": [{"count": 1, "chosen_order": account}]}},
                    "reactors": [{"actor_id": "456", "reaction": "👍", "raw": {"my": bool(account)}}],
                }
            ],
            "export": {"messages": 1, "admin_events": 1, "reactors": 1},
        }
        path.write_text(json.dumps(data))
        original = path.read_bytes()
        output = tmp_path / f"public-{account}.json"
        assert sanitize_export(path, output) == {"messages": 1, "admin_events": 0, "reactors": 1}
        assert path.read_bytes() == original
        census(output, 0)
        first = output.read_bytes()
        repeated = tmp_path / f"repeated-{account}.json"
        sanitize_export(output, repeated)
        assert repeated.read_bytes() == first
        with pytest.raises(ValueError):
            sanitize_export(path, path)
        with pytest.raises(FileExistsError):
            sanitize_export(path, output)
        assert path.read_bytes() == original and output.read_bytes() == first
        outputs.append(first)
    assert outputs[0] == outputs[1]
    result = cast(dict[str, object], json.loads(outputs[0]))
    message = cast(dict[str, object], cast(list[object], result["messages"])[0])
    assert result["admin_events"] == []
    assert message["author_id"] == "123" and message["text"] == "Keep this"
    assert message["author_rank"] == "Moderator" and "author_role" not in message
    assert message["reply_key"] == "-1001:2" and message["entities"]
    assert message["metadata"] == {
        "replies": {"replies": 2},
        "media": {
            "poll": {"question": "Keep question", "public_voters": False},
            "results": {"results": [{"option": "a", "voters": 5}]},
        },
    }
    assert message["reactors"] == [{"actor_id": "456", "reaction": "👍"}]

    broken = tmp_path / "broken.json"
    original = outputs[0][:-2]
    broken.write_bytes(original)
    unpublished = tmp_path / "unpublished.json"
    with pytest.raises(IncompleteJSONError):
        sanitize_export(broken, unpublished)
    assert broken.read_bytes() == original
    assert not unpublished.exists()
    assert not list(tmp_path.glob("*.tmp"))
