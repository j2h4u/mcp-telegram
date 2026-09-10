from __future__ import annotations

from types import SimpleNamespace

import pytest

from mcp_telegram.entity_profile.full_user_normalization import (
    NORMALIZATION_VERSION,
    FullUserNormalization,
    ObservationBoundary,
    ProjectionStatus,
    TargetKind,
    normalize_full_user_response,
)


def _response(
    *,
    entity_id: int = 42,
    bot: bool = False,
    personal_channel_id: object = None,
    chats: object = (),
    **full_user_fields: object,
) -> SimpleNamespace:
    full_user = SimpleNamespace(personal_channel_id=personal_channel_id, **full_user_fields)
    user = SimpleNamespace(
        id=entity_id,
        bot=bot,
        first_name="Ada",
        last_name="Lovelace",
        username="ada",
        contact=True,
    )
    return SimpleNamespace(full_user=full_user, users=[user], chats=chats)


def test_user_and_bot_targets_normalize_independently() -> None:
    for target_kind, bot in ((TargetKind.USER, False), (TargetKind.BOT, True)):
        result = normalize_full_user_response(
            _response(bot=bot, personal_channel_id=777, chats=[SimpleNamespace(id=777, title="Notes")]),
            target_id=42,
            target_kind=target_kind,
            observation=ObservationBoundary(10.0, 11.0),
        )

        assert result.full_profile.status is ProjectionStatus.USABLE
        assert result.personal_channel.status is ProjectionStatus.USABLE
        assert result.full_profile.payload == {
            "name": "Ada Lovelace",
            "first_name": "Ada",
            "last_name": "Lovelace",
            "username": "ada",
            "contact": True,
            "bot": bot,
            "my_membership": {
                "is_member": True,
                "is_admin": False,
                "admin_rights": None,
                "relationship": {"contact": True},
            },
        }
        assert result.personal_channel.payload == {"personal_channel_id": 777, "title": "Notes"}
        assert result.full_profile.provenance is not None
        assert result.full_profile.provenance.normalization_version == NORMALIZATION_VERSION
        assert result.full_profile.provenance.reusable is True


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (
            SimpleNamespace(full_user=SimpleNamespace(), users=[SimpleNamespace(id=99, bot=False)], chats=[]),
            "target_identity_mismatch",
        ),
        (SimpleNamespace(full_user=SimpleNamespace(), users=[], chats=[]), "target_identity_mismatch"),
        (SimpleNamespace(full_user=SimpleNamespace(), chats=[]), "missing_users"),
        (SimpleNamespace(users=[SimpleNamespace(id=42, bot=False)], chats=[]), "missing_full_user"),
    ],
)
def test_missing_or_wrong_identity_makes_both_outcomes_unavailable(response: object, reason: str) -> None:
    result = normalize_full_user_response(response, target_id=42, target_kind=TargetKind.USER)

    assert result.full_profile.status is ProjectionStatus.UNAVAILABLE
    assert result.personal_channel.status is ProjectionStatus.UNAVAILABLE
    assert result.full_profile.reason == reason
    assert result.personal_channel.reason == reason
    assert result.full_profile.provenance is None
    assert result.personal_channel.provenance is None


def test_target_kind_mismatch_makes_both_outcomes_unavailable() -> None:
    result = normalize_full_user_response(_response(bot=True), target_id=42, target_kind=TargetKind.USER)

    assert result.full_profile.status is ProjectionStatus.UNAVAILABLE
    assert result.personal_channel.status is ProjectionStatus.UNAVAILABLE
    assert result.full_profile.reason == "target_kind_mismatch"


def test_explicit_no_channel_is_authoritative_absence() -> None:
    result = normalize_full_user_response(_response(personal_channel_id=None), target_id=42, target_kind="user")

    assert result.full_profile.status is ProjectionStatus.USABLE
    assert result.personal_channel.status is ProjectionStatus.ABSENT
    assert result.personal_channel.authoritative_absence is True
    assert result.personal_channel.payload == {"personal_channel_id": None}
    assert result.personal_channel.provenance is not None
    assert result.personal_channel.provenance.authoritative is True


@pytest.mark.parametrize(
    ("chats", "reason"),
    [
        (None, "channel_metadata_missing"),
        ([SimpleNamespace(id=777)], "channel_metadata_invalid"),
        ([SimpleNamespace(id="777", title="Wrong id")], "channel_metadata_invalid"),
    ],
)
def test_missing_or_invalid_channel_metadata_is_partial_without_hiding_profile(chats: object, reason: str) -> None:
    result = normalize_full_user_response(
        _response(personal_channel_id=777, chats=chats), target_id=42, target_kind=TargetKind.USER
    )

    assert result.full_profile.status is ProjectionStatus.USABLE
    assert result.personal_channel.status is ProjectionStatus.PARTIAL
    assert result.personal_channel.reason == reason
    assert result.personal_channel.payload == {"personal_channel_id": 777}
    assert result.personal_channel.provenance is not None
    assert result.personal_channel.provenance.authoritative is False


def test_invalid_channel_id_is_partial_and_attached_message_is_bounded() -> None:
    result = normalize_full_user_response(
        _response(personal_channel_id="777", personal_channel_message=55), target_id=42, target_kind=TargetKind.USER
    )
    assert result.full_profile.status is ProjectionStatus.USABLE
    assert result.personal_channel.status is ProjectionStatus.PARTIAL
    assert result.personal_channel.reason == "personal_channel_id_invalid"
    assert result.personal_channel.payload is None

    usable = normalize_full_user_response(
        _response(
            personal_channel_id=777,
            personal_channel_message=55,
            chats=[SimpleNamespace(id=777, username="notes")],
        ),
        target_id=42,
        target_kind=TargetKind.USER,
    )
    assert usable.personal_channel.status is ProjectionStatus.USABLE
    assert usable.personal_channel.payload == {
        "personal_channel_id": 777,
        "personal_channel_message": 55,
        "username": "notes",
    }


def _optional_facts_result() -> FullUserNormalization:
    return normalize_full_user_response(
        _response(
            personal_channel_id=777,
            about="  hello ",
            blocked=False,
            ttl_period=86400,
            private_forward_name="forward",
            folder_id=4,
            birthday=SimpleNamespace(day=1, month=2, year=2000),
            bot_info=SimpleNamespace(description="bot", commands=[SimpleNamespace(command="start", description="go")]),
            business_intro=SimpleNamespace(title="work", description="service"),
            business_work_hours=SimpleNamespace(timezone_id="UTC"),
            note=SimpleNamespace(text="private note"),
        ),
        target_id=42,
        target_kind=TargetKind.USER,
    )


def test_optional_full_profile_facts_are_normalized() -> None:
    result = _optional_facts_result()
    profile = result.full_profile.payload
    assert profile is not None
    assert profile["about"] == "hello"
    assert profile["birthday"] == {"day": 1, "month": 2, "year": 2000}
    assert profile["bot_info"] == {"description": "bot", "commands": [{"command": "start", "description": "go"}]}
    assert profile["business_intro"] == {"title": "work", "description": "service"}
    assert profile["business_work_hours"] == {"timezone": "UTC"}
    assert profile["note"] == "private note"


def test_optional_full_profile_does_not_leak_unowned_facts() -> None:
    result = _optional_facts_result()
    profile = result.full_profile.payload
    assert profile is not None
    assert "folder_name" not in profile
    assert "personal_channel_id" not in profile
    assert "latest_or_attached_post" not in profile
    assert "dialog_id" not in profile


def test_unknown_fields_and_local_preview_data_are_not_carried() -> None:
    response = _response(
        personal_channel_id=777,
        chats=[
            SimpleNamespace(
                id=777,
                title="Notes",
                username="notes",
                latest_or_attached_post={"text_preview": "secret"},
                dialog_id=-1000000000777,
            )
        ],
        secret_field="must not cross boundary",
    )
    result = normalize_full_user_response(response, target_id=42, target_kind=TargetKind.USER)

    assert result.personal_channel.payload == {"personal_channel_id": 777, "title": "Notes", "username": "notes"}
    assert result.full_profile.payload is not None
    assert "secret_field" not in result.full_profile.payload
    assert result.personal_channel.provenance is not None
    assert "dialog_id" not in result.personal_channel.provenance.declared_fields
    assert "latest_or_attached_post" not in result.personal_channel.provenance.declared_fields


def test_invalid_observation_boundaries_cannot_authorize_reuse() -> None:
    result = normalize_full_user_response(
        _response(),
        target_id=42,
        target_kind=TargetKind.USER,
        observation=ObservationBoundary(12.0, 11.0),
    )
    assert result.full_profile.status is ProjectionStatus.USABLE
    assert result.full_profile.provenance is not None
    assert result.full_profile.provenance.reusable is False
    assert result.personal_channel.provenance is not None
    assert result.personal_channel.provenance.reusable is False


def test_false_zero_and_empty_full_user_values_are_materialized() -> None:
    result = normalize_full_user_response(
        _response(
            about="",
            blocked=False,
            ttl_period=0,
            private_forward_name="",
            folder_id=0,
            chats=[],
        ),
        target_id=42,
        target_kind=TargetKind.USER,
    )
    profile = result.full_profile.payload
    assert profile is not None
    assert profile["about"] == ""
    assert profile["blocked"] is False
    assert profile["ttl_period"] == 0
    assert profile["private_forward_name"] == ""
    assert profile["folder_id"] == 0
    assert profile["username"] == "ada"


def test_malformed_owned_value_is_omitted_and_marks_partial_coverage() -> None:
    result = normalize_full_user_response(
        _response(about=object(), blocked=False), target_id=42, target_kind=TargetKind.USER
    )
    profile = result.full_profile.payload
    assert profile is not None
    assert "about" not in profile
    assert result.full_profile.reason == "full_profile_fields_unknown"
    assert result.full_profile.provenance is not None
    assert result.full_profile.provenance.authoritative is False
