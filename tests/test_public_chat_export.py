import json
from importlib.metadata import version
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
                            "results": {
                                "total_voters": 5,
                                "recent_voters": [account],
                                "results": [
                                    {
                                        "option": "a",
                                        "voters": 5,
                                        "chosen": bool(account),
                                        "recent_voters": [account],
                                    }
                                ],
                            },
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
    assert cast(dict[str, object], result["metadata"])["exporter"] == {
        "name": "mcp-telegram",
        "version": version("mcp-telegram"),
        "repository_url": "https://github.com/j2h4u/mcp-telegram",
    }
    message = cast(dict[str, object], cast(list[object], result["messages"])[0])
    assert result["admin_events"] == []
    assert message["author_id"] == "123" and message["text"] == "Keep this"
    assert message["author_rank"] == "Moderator" and "author_role" not in message
    assert message["reply_key"] == "-1001:2" and message["entities"]
    assert message["metadata"] == {
        "replies": {"replies": 2},
        "media": {
            "poll": {"question": "Keep question", "public_voters": False},
            "results": {"total_voters": 5, "results": [{"option": "a", "voters": 5}]},
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
    assert not (tmp_path / "unpublished.removed.json").exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_public_export_discards_unknown_fields_in_every_context(tmp_path: Path) -> None:
    secret = {"novel_sensitive_field": "private"}
    identity = {"id": "123", "name": "Member", "role": "member", "metadata": {"label": "Public", **secret}, **secret}
    reaction = {"_": "ReactionEmoji", "emoticon": "👍", **secret}
    message = {
        "message_id": "1",
        "dialog_id": "-1001",
        "message_key": "-1001:1",
        "text": "Public",
        "author_id": "123",
        "author_name": secret,
        "author_metadata": {"label": "Public", **secret},
        "topic": {"id": "2", "title": "Topic", **secret},
        "related_users": [identity],
        "entities": [{"_": "MessageEntityTextUrl", "offset": 0, "length": 6, "url": "https://example.org", **secret}],
        "service_action": {
            "_": "MessageActionInviteToGroupCall",
            "users": ["123"],
            "call": {"_": "InputGroupCall", "id": "2", **secret},
            **secret,
        },
        "reactions": {
            "can_view_list": True,
            "aggregate": {
                "results": [{"count": 1, "reaction": reaction, **secret}],
                "recent_reactions": [secret],
                **secret,
            },
            **secret,
        },
        "reactors": [{"actor_id": "456", "date": "2026-01-01", "reaction": reaction, **secret}],
        "metadata": {
            "replies": {"replies": 2, "title": "wrong context", **secret},
            "fwd_from": {"date": "2026-01-01", "from_id": {"_": "PeerUser", "user_id": "123", **secret}, **secret},
            "media": {
                "_": "MessageMediaPoll",
                "poll": {
                    "question": {"_": "TextWithEntities", "text": "Question", "entities": [], **secret},
                    "answers": [
                        {
                            "text": "Option",
                            "option": {"encoding": "base64", "data": "YQ==", **secret},
                            "added_by": secret,
                            **secret,
                        }
                    ],
                    **secret,
                },
                "results": {
                    "total_voters": 2,
                    "results": [{"option": "a", "voters": 2, "correct": True, **secret}],
                    "recent_voters": [secret],
                    **secret,
                },
                **secret,
            },
            **secret,
        },
        **secret,
    }
    data = {
        "format_version": 1,
        "group": {"dialog_id": "-1001", "title": "Group", **secret},
        "metadata": {"order": "newest", "peers": [{"dialog_id": "-1001", **secret}], **secret},
        "admin_events": [],
        "messages": [message],
        "export": {"messages": 1, "admin_events": 0, "reactors": 1},
    }
    source = tmp_path / "source.json"
    source.write_text(json.dumps(data))
    output = tmp_path / "public.json"
    sanitize_export(source, output)
    result = json.loads(output.read_text())  # pyright: ignore[reportAny]
    assert "novel_sensitive_field" not in output.read_text()
    public = result["messages"][0]  # pyright: ignore[reportAny]
    assert "author_name" not in public
    assert public["related_users"] == [
        {"id": "123", "name": "Member", "role": "member", "metadata": {"label": "Public"}}
    ]
    assert public["topic"] == {"id": "2", "title": "Topic"}
    assert public["service_action"] == {
        "_": "MessageActionInviteToGroupCall",
        "users": ["123"],
        "call": {"_": "InputGroupCall", "id": "2"},
    }
    assert public["reactions"] == {
        "aggregate": {"results": [{"count": 1, "reaction": {"_": "ReactionEmoji", "emoticon": "👍"}}]}
    }
    assert public["metadata"]["replies"] == {"replies": 2}
    assert public["metadata"]["media"]["poll"]["question"]["text"] == "Question"
    assert public["metadata"]["media"]["results"] == {"total_voters": 2, "results": [{"option": "a", "voters": 2}]}
    message["service_action"] = {
        "_": "ChannelAdminLogEventActionParticipantToggleAdmin",
        "users": ["123"],
        "title": "private",
    }
    message["entities"][0]["url"] = secret
    identity["role"] = "unknown"
    identity["metadata"]["label"] = secret
    message["metadata"]["media"]["results"]["total_voters"] = secret
    message["metadata"]["media"]["poll"]["question"]["text"] = secret
    message["reactors"][0]["reaction"] = {"_": "UnknownReaction", "emoticon": "private"}
    source.write_text(json.dumps(data))
    second = tmp_path / "unknown.json"
    sanitize_export(source, second)
    public = json.loads(second.read_text())["messages"][0]  # pyright: ignore[reportAny]
    assert public["service_action"] == {}
    assert "url" not in public["entities"][0]
    assert "role" not in public["related_users"][0]
    assert public["related_users"][0]["metadata"] == {}
    assert "total_voters" not in public["metadata"]["media"]["results"]
    assert "text" not in public["metadata"]["media"]["poll"]["question"]
    assert public["reactors"][0]["reaction"] == {}

    data["novel_sensitive_field"] = "private"
    source.write_text(json.dumps(data))
    rejected = tmp_path / "rejected.json"
    with pytest.raises(ValueError, match="Malformed base export structure"):
        sanitize_export(source, rejected)
    assert not rejected.exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_removed_manifest_contains_exact_source_values_and_pointers(tmp_path: Path) -> None:
    private_event = {"event_id": "1", "action": {"private_admin_fact": ["original", {"value": 7}]}}
    data = {
        "format_version": 1,
        "group": {"dialog_id": "-1001", "title": "Group", "private/~": [1, {"nested": "private"}]},
        "metadata": {"order": "newest", "peers": [{"dialog_id": "-1001", "private": True}]},
        "admin_events": [private_event],
        "messages": [
            {
                "message_id": "1",
                "text": {"private": "injected"},
                "entities": [{"_": "MessageEntityBold", "offset": 0, "length": 1, "private/~": ["hidden"]}],
                "service_action": {"_": "MessageActionChatAddUser", "users": ["123", {"private": 5}]},
                "reactors": [{"actor_id": "456", "reaction": "👍", "raw": {"private": 9}}],
            }
        ],
        "export": {"messages": 1, "admin_events": 1, "reactors": 1},
    }
    source = tmp_path / "source.json"
    source.write_text(json.dumps(data))
    original = source.read_bytes()
    output = tmp_path / "public.json"
    removed = tmp_path / "explicit-removed.json"
    sanitize_export(source, output, removed)
    assert source.read_bytes() == original
    assert removed.stat().st_mode & 0o777 == 0o600
    assert output.stat().st_mode & 0o777 == 0o600
    assert json.loads(removed.read_text()) == {
        "format_version": 1,
        "removed": [
            {"path": "/group/private~1~0", "value": [1, {"nested": "private"}]},
            {"path": "/metadata/peers/0/private", "value": True},
            {"path": "/admin_events/0", "value": private_event},
            {"path": "/messages/0/text", "value": {"private": "injected"}},
            {"path": "/messages/0/entities/0/private~1~0", "value": ["hidden"]},
            {"path": "/messages/0/service_action/users/1", "value": {"private": 5}},
            {"path": "/messages/0/reactors/0/raw", "value": {"private": 9}},
            {"path": "/export/admin_events", "value": 1},
        ],
    }
    assert "private" not in output.read_text()
    repeated = tmp_path / "repeated.json"
    sanitize_export(output, repeated)
    assert repeated.read_bytes() == output.read_bytes()
    assert json.loads((tmp_path / "repeated.removed.json").read_text()) == {"format_version": 1, "removed": []}
    with pytest.raises(FileExistsError):
        sanitize_export(source, tmp_path / "absent-public.json", removed)
    assert not (tmp_path / "absent-public.json").exists()
    with pytest.raises(ValueError):
        sanitize_export(source, tmp_path / "same.json", tmp_path / "same.json")
    with pytest.raises(ValueError):
        sanitize_export(source, tmp_path / "absent-public.json", source)
    alias = tmp_path / "alias.json"
    alias.symlink_to(source)
    with pytest.raises(ValueError):
        sanitize_export(source, tmp_path / "absent-public.json", alias)
    dangling = tmp_path / "dangling.json"
    dangling.symlink_to(tmp_path / "missing.json")
    with pytest.raises(FileExistsError):
        sanitize_export(source, dangling)
    assert dangling.is_symlink()
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("race", ["none", "public", "removed"])
def test_second_publication_failure_rolls_back_only_owned_links(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    race: str,
) -> None:
    import os

    source = tmp_path / "source.json"
    source.write_text(
        json.dumps(
            {
                "format_version": 1,
                "group": {},
                "metadata": {},
                "admin_events": [],
                "messages": [],
                "export": {"messages": 0, "admin_events": 0, "reactors": 0},
            }
        )
    )
    original = source.read_bytes()
    output = tmp_path / "public.json"
    removed = tmp_path / "public.removed.json"
    link = os.link

    def fail_second_link(temporary: Path, destination: Path) -> None:
        if destination == output:
            assert removed.exists()
            if race == "public":
                output.write_text("racing public")
            elif race == "removed":
                removed.unlink()
                removed.write_text("racing removed")
            raise OSError("second publication failed")
        link(temporary, destination)

    monkeypatch.setattr(os, "link", fail_second_link)
    with pytest.raises(OSError, match="second publication failed"):
        sanitize_export(source, output)
    assert source.read_bytes() == original
    if race == "public":
        assert output.read_text() == "racing public"
    else:
        assert not output.exists()
    if race == "removed":
        assert removed.read_text() == "racing removed"
    else:
        assert not removed.exists()
    assert not list(tmp_path.glob("*.tmp"))
