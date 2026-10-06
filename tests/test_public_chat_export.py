import json
from importlib.metadata import version
from pathlib import Path
from typing import cast

import pytest
from devtools.public_chat_export import MESSAGE, public_facts, sanitize_export
from ijson.common import IncompleteJSONError

from mcp_telegram.chat_export_checkpoint import ORDER, census


@pytest.mark.parametrize("format_version", [1, 2])
def test_public_export_is_account_independent_atomic_and_idempotent(tmp_path: Path, format_version: int) -> None:
    outputs = []
    for account in (0, 1):
        path = tmp_path / f"account-{account}.json"
        data = {
            "format_version": format_version,
            "group": {"dialog_id": "-1001", "title": "Group"},
            "metadata": {
                "order": "newest_to_oldest within each peer; primary then migrated predecessors",
                "peers": [{"dialog_id": "-1001"}],
            },
            "admin_events": [{"event_id": "1", "dialog_id": "-1001", "action": {"approved_by": account}}],
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
                    "reactors": [
                        {
                            "actor_id": "456",
                            "reaction": {"_": "ReactionEmoji", "emoticon": "👍"},
                            "raw": {"my": bool(account)},
                        }
                    ],
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
        assert json.loads(first)["format_version"] == format_version
        assert json.loads((tmp_path / f"public-{account}.removed.json").read_text())["format_version"] == 1
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
    assert message["reactors"] == [{"actor_id": "456", "reaction": {"_": "ReactionEmoji", "emoticon": "👍"}, "raw": {}}]

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
        "metadata": {"order": ORDER, "peers": [{"dialog_id": "-1001", **secret}], **secret},
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
        "aggregate": {
            "results": [{"count": 1, "reaction": {"_": "ReactionEmoji", "emoticon": "👍"}}],
            "recent_reactions": [{}],
        }
    }
    assert public["metadata"]["replies"] == {"replies": 2}
    assert public["metadata"]["media"]["poll"]["question"]["text"] == "Question"
    assert public["metadata"]["media"]["results"] == {
        "total_voters": 2,
        "results": [{"option": "a", "voters": 2, "correct": True}],
    }
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
    assert public["related_users"][0]["role"] == "unknown"
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


def test_public_projection_rejects_container_scalars_and_keeps_legacy_reactions() -> None:
    assert public_facts({"author_name": {"private": "injected"}, "text": ["private"]}, MESSAGE) == {}
    assert public_facts({"reactors": [{"reaction": "👍"}]}, MESSAGE) == {"reactors": [{"reaction": "👍"}]}


def test_removed_manifest_contains_exact_source_values_and_pointers(tmp_path: Path) -> None:
    private_event = {
        "event_id": "1",
        "dialog_id": "-1001",
        "action": {"private_admin_fact": ["original", {"value": 7}]},
    }
    data = {
        "format_version": 1,
        "group": {"dialog_id": "-1001", "title": "Group", "private/~": [1, {"nested": "private"}]},
        "metadata": {"order": ORDER, "peers": [{"dialog_id": "-1001", "private": True}]},
        "admin_events": [private_event],
        "messages": [
            {
                "message_id": "1",
                "dialog_id": "-1001",
                "message_key": "-1001:1",
                "text": "Public",
                "metadata": {"from_rank": {"private": "injected"}},
                "entities": [{"_": "MessageEntityBold", "offset": 0, "length": 1, "private/~": ["hidden"]}],
                "service_action": {"_": "MessageActionChatAddUser", "users": ["123", {"private": 5}]},
                "reactors": [
                    {"actor_id": "456", "reaction": {"_": "ReactionEmoji", "emoticon": "👍"}, "raw": {"private": 9}}
                ],
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
            {"path": "/messages/0/metadata/from_rank", "value": {"private": "injected"}},
            {"path": "/messages/0/entities/0/private~1~0", "value": ["hidden"]},
            {"path": "/messages/0/service_action/users/1", "value": {"private": 5}},
            {"path": "/messages/0/reactors/0/raw/private", "value": 9},
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
                "group": {"dialog_id": "-1001"},
                "metadata": {"order": ORDER, "peers": [{"dialog_id": "-1001"}]},
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


def _public_content_records() -> tuple[dict[str, object], dict[str, object], dict[str, object], dict[str, object]]:
    hidden = {"novel_private_marker": "private"}
    plain = {"_": "TextPlain", "text": "Hello ", **hidden}
    photo = {
        "_": "Photo",
        "id": "10",
        "sizes": [{"_": "PhotoSize", "type": "x", "w": 20, "h": 30, "size": 40, **hidden}],
        **hidden,
    }
    document = {
        "_": "Document",
        "id": "11",
        "mime_type": "image/png",
        "attributes": [
            {
                "_": "DocumentAttributeSticker",
                "alt": "Sticker",
                "stickerset": {"_": "InputStickerSetID", "id": "12", **hidden},
                **hidden,
            }
        ],
        "thumbs": photo["sizes"],
        **hidden,
    }
    rich = {
        "_": "RichMessage",
        "part": False,
        "rtl": False,
        "blocks": [
            {
                "_": "PageBlockParagraph",
                "text": {
                    "_": "TextConcat",
                    "texts": [
                        plain,
                        {
                            "_": "TextBold",
                            "text": {
                                "_": "TextUrl",
                                "text": {"_": "TextPlain", "text": "world"},
                                "url": "https://example.org",
                                **hidden,
                            },
                        },
                    ],
                },
                **hidden,
            },
            {"_": "PageBlockHeading6", "text": {"_": "TextFixed", "text": {"_": "TextPlain", "text": "Heading"}}},
            {"_": "PageBlockDivider"},
            {
                "_": "PageBlockList",
                "items": [
                    {
                        "_": "PageListItemBlocks",
                        "checkbox": True,
                        "checked": False,
                        "blocks": [
                            {
                                "_": "PageBlockParagraph",
                                "text": {"_": "TextItalic", "text": {"_": "TextPlain", "text": "Item"}},
                            }
                        ],
                    }
                ],
            },
            {"_": "FuturePrivateRichConstructor", "text": "private"},
        ],
        "documents": [document],
        "photos": [photo],
        **hidden,
    }
    first = {
        "message_id": "1",
        "dialog_id": "-1001",
        "message_key": "-1001:1",
        "text": "",
        "author_rank": "Current",
        "reactors": [
            {
                "actor_id": "456",
                "reaction": {"_": "ReactionEmoji", "emoticon": "👍"},
                "raw": {"big": True, "my": False, **hidden},
            }
        ],
        "metadata": {
            "rich_message": rich,
            "from_rank": "Historical",
            "guestchat_via_from": {"_": "PeerUser", "user_id": "123", **hidden},
            "summary_from_language": "en",
            "edit_hide": True,
            "invert_media": True,
            "silent": True,
            "reply_markup": {
                "_": "ReplyInlineMarkup",
                "rows": [
                    {
                        "_": "KeyboardInlineButtonRow",
                        "buttons": [
                            {
                                "_": "KeyboardInlineButton",
                                "text": "Visit",
                                "style": "primary",
                                "type": {"_": "InlineButtonTypeUrl", "url": "https://example.org", **hidden},
                                **hidden,
                            }
                        ],
                    }
                ],
                **hidden,
            },
            "media": {
                "_": "MessageMediaPoll",
                "poll": {
                    "question": "Question",
                    "answers": [
                        {
                            "text": "Option",
                            "option": "a",
                            "added_by": {"_": "PeerUser", "user_id": "123", **hidden},
                            "media": {"_": "MessageMediaDocument", "document": document, **hidden},
                            **hidden,
                        }
                    ],
                },
                **hidden,
            },
            **hidden,
        },
        **hidden,
    }
    second = {
        "message_id": "2",
        "dialog_id": "-1001",
        "message_key": "-1001:2",
        "text": "Existing text",
        "reactors": [{"raw": {"big": hidden}}],
        "metadata": {
            "rich_message": rich,
            "media": {
                "_": "MessageMediaWebPage",
                "force_large_media": True,
                "force_small_media": False,
                "manual": True,
                "safe": True,
                "alt_documents": [document],
                "webpage": {
                    "_": "WebPage",
                    "has_large_media": True,
                    "video_cover_photo": True,
                    "embed_url": "https://example.org/embed",
                    "embed_type": "video",
                    "embed_width": 20,
                    "embed_height": 30,
                    "photo": photo,
                    "document": document,
                    **hidden,
                },
            },
        },
    }
    encoded = {"data": "AQ==", "encoding": "base64"}
    photo["dc_id"] = 2
    photo["access_hash"] = 777
    photo["file_reference"] = encoded
    photo["sizes"].append({"_": "PhotoStrippedSize", "type": "i", "bytes": {**encoded, **hidden}})
    document["dc_id"] = 3
    document["access_hash"] = 888
    document["file_reference"] = encoded
    document["attributes"].append({"_": "DocumentAttributeAudio", "voice": True, "waveform": {**encoded, **hidden}})
    document["attributes"][0]["mask"] = False
    document["attributes"][0]["mask_coords"] = {"_": "MaskCoords", "n": 1, "x": 0.0, "y": 0.0, "zoom": 1.0, **hidden}
    first["metadata"]["media"]["poll"].update({"hash": 9, "countries_iso2": [], "creator": True})
    first["metadata"]["media"]["results"] = {
        "_": "PollResults",
        "min": False,
        "solution": "Public explanation",
        "solution_entities": [],
        "solution_media": None,
        "results": [{"option": "a", "correct": True, "chosen": True}],
        "recent_voters": [123],
    }
    first["metadata"]["reactions"] = {
        "_": "MessageReactions",
        "min": False,
        "recent_reactions": [
            {
                "_": "MessagePeerReaction",
                "big": True,
                "my": True,
                "unread": True,
                "peer_id": {"_": "PeerUser", "user_id": "123"},
                "reaction": {"_": "ReactionEmoji", "emoticon": "👍"},
                "date": "2026-01-01",
            }
        ],
        "top_reactors": [],
        "results": [],
        "can_see_list": True,
    }
    first["metadata"].update(
        {
            "from_id": {"_": "PeerUser", "user_id": "123"},
            "peer_id": {"_": "PeerChannel", "channel_id": "10"},
            "legacy": False,
            "effect": None,
        }
    )
    cached = {
        "_": "Page",
        "part": False,
        "rtl": False,
        "v2": True,
        "url": "https://example.org",
        "views": None,
        "documents": [document],
        "photos": [photo],
        "blocks": [
            {
                "_": "PageBlockBlockquote",
                "collapsed": False,
                "text": {"_": "TextPlain", "text": "Article"},
                "caption": {"_": "TextEmpty"},
            },
            {
                "_": "PageBlockOrderedList",
                "items": [
                    {
                        "_": "PageListOrderedItemText",
                        "checkbox": False,
                        "checked": False,
                        "type": "decimal",
                        "value": 3,
                        "num": "1",
                        "text": {"_": "TextPlain", "text": "Entry"},
                    }
                ],
            },
        ],
        **hidden,
    }
    second["metadata"]["media"]["webpage"]["cached_page"] = cached
    return first, second, encoded, cached


def test_public_rich_content_media_and_buttons_are_preserved(tmp_path: Path) -> None:
    first, second, encoded, cached = _public_content_records()
    source = tmp_path / "source.json"
    source.write_text(
        json.dumps(
            {
                "format_version": 1,
                "group": {"dialog_id": "-1001"},
                "metadata": {"order": ORDER, "peers": [{"dialog_id": "-1001"}]},
                "admin_events": [],
                "messages": [first, second],
                "export": {"messages": 2, "admin_events": 0, "reactors": 2},
            }
        )
    )
    original = source.read_bytes()
    output = tmp_path / "public.json"
    sanitize_export(source, output)
    public = json.loads(output.read_text())["messages"]  # pyright: ignore[reportAny]
    assert public[0]["text"] == ""
    assert public[1]["text"] == "Existing text"
    assert public[0]["author_rank"] == "Current"
    assert public[0]["metadata"]["from_rank"] == "Historical"
    assert public[0]["metadata"]["guestchat_via_from"] == {"_": "PeerUser", "user_id": "123"}
    assert public[0]["reactors"][0]["raw"] == {"big": True}
    assert public[1]["reactors"][0]["raw"] == {}
    assert public[0]["metadata"]["reply_markup"]["rows"][0]["buttons"][0] == {
        "_": "KeyboardInlineButton",
        "text": "Visit",
        "style": "primary",
        "type": {"_": "InlineButtonTypeUrl", "url": "https://example.org"},
    }
    assert public[0]["metadata"]["media"]["poll"]["answers"][0]["added_by"] == {"_": "PeerUser", "user_id": "123"}
    assert public[0]["metadata"]["media"]["poll"]["answers"][0]["media"]["document"]["attributes"][0]["stickerset"] == {
        "_": "InputStickerSetID",
        "id": "12",
    }
    assert public[0]["metadata"]["rich_message"]["blocks"][-1] == {}
    assert public[1]["metadata"]["media"]["webpage"]["photo"]["sizes"][0]["w"] == 20
    assert public[1]["metadata"]["media"]["alt_documents"][0]["thumbs"][0]["h"] == 30
    assert public[0]["metadata"]["legacy"] is False and public[0]["metadata"]["effect"] is None
    assert public[0]["metadata"]["from_id"] == {"_": "PeerUser", "user_id": "123"}
    assert public[0]["metadata"]["media"]["poll"]["countries_iso2"] == []
    assert "creator" not in public[0]["metadata"]["media"]["poll"]
    assert public[0]["metadata"]["media"]["results"] == {
        "_": "PollResults",
        "min": False,
        "solution": "Public explanation",
        "solution_entities": [],
        "solution_media": None,
        "results": [{"option": "a", "correct": True}],
    }
    assert public[0]["metadata"]["reactions"]["recent_reactions"][0] == {
        "_": "MessagePeerReaction",
        "big": True,
        "peer_id": {"_": "PeerUser", "user_id": "123"},
        "reaction": {"_": "ReactionEmoji", "emoticon": "👍"},
        "date": "2026-01-01",
    }
    assert public[1]["metadata"]["media"]["webpage"]["cached_page"]["blocks"] == cached["blocks"]
    assert public[1]["metadata"]["media"]["webpage"]["photo"]["dc_id"] == 2
    assert public[1]["metadata"]["media"]["webpage"]["photo"]["sizes"][1]["bytes"] == encoded
    assert public[1]["metadata"]["media"]["webpage"]["document"]["attributes"][1]["waveform"] == encoded
    assert "access_hash" not in output.read_text() and "file_reference" not in output.read_text()
    assert "novel_private_marker" not in output.read_text()
    removed = json.loads((tmp_path / "public.removed.json").read_text())["removed"]  # pyright: ignore[reportAny]
    assert all(item["path"] != "/messages/0/text" for item in removed)
    assert {"path": "/messages/0/reactors/0/raw/my", "value": False} in removed
    assert source.read_bytes() == original
    repeated = tmp_path / "repeated.json"
    sanitize_export(output, repeated)
    assert output.read_bytes() == repeated.read_bytes()
