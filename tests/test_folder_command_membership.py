from dataclasses import replace

import pytest

from mcp_telegram.folders.commands import apply_membership_changes
from mcp_telegram.folders.contracts import FolderRule


def test_explicit_membership_changes_preserve_unrelated_native_fields() -> None:
    original = FolderRule(
        2, "Unread", included_ids=(10, 11), pinned_ids=(12, 13), excluded_ids=(14, 15), exclude_read=True
    )
    changed = apply_membership_changes(
        original, add_dialog_ids=(14, 14), remove_dialog_ids=(11, 12), clear_excluded_dialog_ids=(15,)
    )
    assert changed == replace(original, included_ids=(10, 14), pinned_ids=(13,), excluded_ids=(11, 12))


def test_conflicting_actions_rejected() -> None:
    with pytest.raises(ValueError, match="disjoint"):
        apply_membership_changes(FolderRule(2, "A"), add_dialog_ids=(1,), clear_excluded_dialog_ids=(1,))


def test_native_wire_reconstruction_preserves_metadata_and_rejects_substitution() -> None:
    from dataclasses import replace

    from telethon.tl.types import InputPeerUser

    from mcp_telegram.folders.commands import (
        folder_intent_fingerprint,
        native_folder_request,
        validate_folder_wire_request,
    )
    from mcp_telegram.folders.contracts import DialogCategory
    from mcp_telegram.folders.sqlite_repository import encode_folder_definition
    from mcp_telegram.native_write_fence import NativeWriteReceiptBinding

    rule = FolderRule(
        2,
        "🐈A",
        included_ids=(42,),
        categories=frozenset({DialogCategory.GROUP}),
        exclude_read=True,
        title_entities=({"_": "MessageEntityBold", "offset": 0, "length": 2},),
        emoticon="🐈",
        color=4,
    )
    before = encode_folder_definition(replace(rule, title="before", title_entities=()))
    intended = encode_folder_definition(rule)
    binding = NativeWriteReceiptBinding(
        "folder", 1, 2, 3, "op", "update", 2, folder_intent_fingerprint("update", 2, before, intended), 1
    )

    def resolve(peer: int) -> InputPeerUser:
        return InputPeerUser(peer, 99)

    request = native_folder_request("update", 2, rule, resolve)
    assert validate_folder_wire_request(binding, request, before, intended, resolve)
    assert not validate_folder_wire_request(binding, request, before, intended, lambda peer: InputPeerUser(peer, 100))
    assert not validate_folder_wire_request(binding, request, before, intended + " ", resolve)
    bad = replace(rule, title_entities=({"_": "MessageEntityBold", "offset": 1, "length": 1},))
    with pytest.raises(ValueError, match="entity"):
        native_folder_request("update", 2, bad, resolve)


def test_plan_create_update_delete_and_reject_empty_manual_rule() -> None:
    from mcp_telegram.folders.commands import plan_folder_change

    created = plan_folder_change("create", 4, None, title="A", dialog_ids=(10, 10, 11))
    assert created is not None and created.included_ids == (10, 11)
    updated = plan_folder_change("update", 4, created, title="B", remove_dialog_ids=(10,))
    assert updated is not None and updated.title == "B" and updated.excluded_ids == (10,)
    assert plan_folder_change("delete", 4, updated) is None
    with pytest.raises(ValueError, match="at least one"):
        plan_folder_change("update", 4, updated, remove_dialog_ids=(11,))


def test_title_mention_and_formatted_date_preserve_native_fields() -> None:
    from telethon.tl.types import InputMessageEntityMentionName, InputPeerUser, MessageEntityFormattedDate

    from mcp_telegram.folders.commands import native_folder_request

    rule = FolderRule(
        3,
        "Alice today",
        included_ids=(42,),
        title_entities=(
            {"_": "MessageEntityMentionName", "offset": 0, "length": 5, "user_id": 42},
            {
                "_": "MessageEntityFormattedDate",
                "offset": 6,
                "length": 5,
                "date": "2026-01-02T00:00:00+00:00",
                "short_date": True,
            },
        ),
    )
    request = native_folder_request("update", 3, rule, lambda user_id: InputPeerUser(user_id, 123))
    mention, date = request.filter.title.entities  # type: ignore[attr-defined]
    assert isinstance(mention, InputMessageEntityMentionName)
    from telethon.tl.types import InputUser

    assert isinstance(mention.user_id, InputUser)
    assert mention.user_id.user_id == 42 and mention.user_id.access_hash == 123
    assert isinstance(date, MessageEntityFormattedDate) and date.short_date is True
    assert bytes(request)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="peer unavailable"):
        native_folder_request("update", 3, rule, lambda _user_id: None)


def test_clear_exclusion_preserves_independent_override_and_reports_it() -> None:
    from mcp_telegram.folders.commands import _operation_result, plan_folder_change
    from mcp_telegram.folders.sqlite_repository import encode_folder_definition

    original = FolderRule(
        4, "Unread", included_ids=(42,), pinned_ids=(43,), excluded_ids=(42, 43, 44), exclude_read=True
    )
    updated = plan_folder_change("update", 4, original, clear_excluded_dialog_ids=(42, 43, 44))
    assert updated is not None
    assert updated.included_ids == (42,) and updated.pinned_ids == (43,)
    assert updated.excluded_ids == () and updated.exclude_read
    row = ("fp", 4, "acknowledged", "matched", "attempt", 0, encode_folder_definition(original))
    result = _operation_result(1, "op", row, {"clear_excluded_dialog_ids": [42, 43, 44, 42]})
    assert result["preserved_overrides"] == [42, 43]
