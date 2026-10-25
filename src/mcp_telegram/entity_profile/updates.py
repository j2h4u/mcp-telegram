"""Normalize supplied Telegram profile facts without acquiring anything remotely."""

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import cast

from telethon.tl import types  # type: ignore[import-untyped]
from telethon.tl.tlobject import TLObject  # type: ignore[import-untyped]

from ..identity_observation import USERNAME_UNOBSERVED, observe_username
from .contracts import (
    ProfileCollectionItem,
    ProfileFieldClass,
    ProfileFieldCoverage,
    ProfileFieldObservation,
    ProfileFieldState,
)
from .full_user_normalization import (
    _USER_SCALAR_FIELDS,
    _normalize_names,
    _normalize_scalar_fields,
    _normalize_status,
    _normalize_user_optional,
)
from .semantic_normalization import normalize_collection_item


@dataclass(frozen=True, slots=True)
class ProfilePush:
    entity_id: int
    fields: Mapping[str, object]
    minimal: bool = False
    authoritative_photo: bool = False
    semantic_fields: Mapping[str, object] = field(default_factory=dict)
    semantic_coverage: Mapping[str, ProfileFieldCoverage] = field(default_factory=dict)
    story: ProfileCollectionItem | None = None


@dataclass(frozen=True, slots=True)
class ProfilePushMarker:
    """One received object's order; attached only after its transaction commits."""

    owner: object
    observation: ProfileFieldObservation


def push_marker(value: object, owner: object) -> ProfileFieldObservation | None:
    marker = getattr(value, "_mcp_profile_push", None)
    return marker.observation if isinstance(marker, ProfilePushMarker) and marker.owner is owner else None


def mark_push_applied(value: object, owner: object, observation: ProfileFieldObservation) -> None:
    """Call in the writer job after commit, before the next job can start."""
    if isinstance(value, TLObject):
        value.__dict__["_mcp_profile_push"] = ProfilePushMarker(owner, observation)


def bundled_profile_users(update: object) -> tuple[types.User, ...]:
    """Read Telethon's received envelope, never its potentially remote getters."""
    entities: object = getattr(update, "_entities", None)
    if not isinstance(entities, Mapping):
        return ()
    return tuple(
        user for key, user in entities.items() if isinstance(user, types.User) and _positive_id(key) and user.id == key
    )


def _positive_id(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _status(value: object) -> tuple[bool, object]:
    if value is None or isinstance(value, types.UserStatusEmpty):
        return True, None
    normalized = _normalize_status(value)
    return normalized is not None, normalized


def _emoji(value: object) -> tuple[bool, object]:
    if value is None or isinstance(value, types.EmojiStatusEmpty):
        return True, None
    if isinstance(value, (types.EmojiStatus, types.EmojiStatusCollectible)) and _positive_id(value.document_id):
        return True, value.document_id
    return False, None


def _photo(value: object) -> tuple[bool, object]:
    if value is None or isinstance(value, types.UserProfilePhotoEmpty):
        return True, None
    if isinstance(value, types.UserProfilePhoto) and _positive_id(value.photo_id):
        return True, {"photo_id": value.photo_id, "date": None}
    return False, None


def _aliases(primary: object, usernames: object) -> dict[str, object]:
    observed = observe_username(primary, usernames)
    result: dict[str, object] = {}
    if observed is not USERNAME_UNOBSERVED:
        result["username"] = observed
    if usernames is None:
        result["extra_usernames"] = []
    elif isinstance(usernames, (list, tuple)) and all(isinstance(item, types.Username) for item in usernames):
        result["extra_usernames"] = [item.username for item in usernames if item.username != observed]
    return result


def _user_profile_fields(user: types.User) -> dict[str, object]:
    fields, _ = _normalize_scalar_fields(user, _USER_SCALAR_FIELDS)
    names, _ = _normalize_names(user)
    optional, _ = _normalize_user_optional(user)
    fields.update(names)
    fields.update(optional)
    fields.update(_aliases(user.username, user.usernames))
    for name, kind in _USER_SCALAR_FIELDS:
        if kind == "bool" and getattr(user, name, None) is None and not user.min:
            fields[name] = False
    for name, raw, normalize in (
        ("status", user.status, _status),
        ("emoji_status_id", user.emoji_status, _emoji),
        ("current_photo", user.photo, _photo),
    ):
        observed, value = normalize(raw)
        if observed:
            fields[name] = value
    return fields


def _minimal_user_fields(user: types.User, fields: dict[str, object]) -> dict[str, object]:
    fields = {name: value for name, value in fields.items() if value is not None and value != ""}
    for name in ("contact", "mutual_contact", "close_friend"):
        fields.pop(name, None)
    if user.photo is not None and user.apply_min_photo:
        observed, photo = _photo(user.photo)
        if observed:
            fields["current_photo"] = photo
    return fields


def project_user_profile(user: object) -> ProfilePush | None:
    """Project existing supported User fields; min authority is applied by the repository."""
    if not isinstance(user, types.User) or not _positive_id(user.id):
        return None
    fields = _user_profile_fields(user)
    if user.min:
        fields = _minimal_user_fields(user, fields)
    semantic, coverage, _ = normalize_collection_item(user)
    return ProfilePush(
        user.id,
        fields,
        bool(user.min),
        bool(user.min and user.apply_min_photo),
        {f"user.{key}": value for key, value in semantic.items() if key != "constructor"},
        {"user" + key[len("item") :]: value for key, value in coverage.items()},
    )


def profile_update_id(update: object) -> int | None:
    if isinstance(
        update,
        (
            types.UpdateUser,
            types.UpdateUserName,
            types.UpdateUserPhone,
            types.UpdateUserStatus,
            types.UpdateUserEmojiStatus,
        ),
    ):
        return int(update.user_id) if _positive_id(update.user_id) else None
    peer: object = None
    if isinstance(update, types.UpdatePeerBlocked):
        peer = update.peer_id
    elif isinstance(
        update,
        (
            types.UpdatePeerHistoryTTL,
            types.UpdatePeerSettings,
            types.UpdatePeerWallpaper,
            types.UpdateStory,
            types.UpdateReadStories,
            types.UpdateSentStoryReaction,
        ),
    ):
        peer = update.peer
    elif isinstance(update, types.UpdateNotifySettings):
        peer = getattr(update.peer, "peer", None)
    return int(peer.user_id) if isinstance(peer, types.PeerUser) and _positive_id(peer.user_id) else None


def _ttl_fields(update: types.UpdatePeerHistoryTTL) -> dict[str, object]:
    value = cast(object, update.ttl_period)
    if value is None or (isinstance(value, int) and not isinstance(value, bool) and value >= 0):
        return {"ttl_period": value}
    return {}


def _observed_field(name: str, observed_value: tuple[bool, object]) -> dict[str, object]:
    observed, value = observed_value
    return {name: value} if observed else {}


def _phone_fields(update: types.UpdateUserPhone) -> dict[str, object]:
    return {"phone": update.phone or None} if isinstance(update.phone, str) else {}


def project_profile_update(update: object) -> ProfilePush | None:
    entity_id = profile_update_id(update)
    if entity_id is None:
        return None
    fields: dict[str, object] = {}
    if isinstance(update, types.UpdateUserName):
        fields, _ = _normalize_names(update)
        fields.update(_aliases(USERNAME_UNOBSERVED, update.usernames))
    elif isinstance(update, types.UpdateUserPhone):
        fields = _phone_fields(update)
    elif isinstance(update, types.UpdateUserStatus):
        fields = _observed_field("status", _status(update.status))
    elif isinstance(update, types.UpdateUserEmojiStatus):
        fields = _observed_field("emoji_status_id", _emoji(update.emoji_status))
    elif isinstance(update, types.UpdatePeerBlocked):
        if not update.blocked_my_stories_from:
            fields["blocked"] = bool(update.blocked)
    elif isinstance(update, types.UpdatePeerHistoryTTL):
        fields.update(_ttl_fields(update))
    semantic, coverage = _specific_semantics(update)
    return ProfilePush(
        entity_id, fields, semantic_fields=semantic, semantic_coverage=coverage, story=_supplied_story(update)
    )


def _specific_values(update: object) -> dict[str, object]:
    if isinstance(update, types.UpdateUserName):
        values: dict[str, object] = {
            "user.first_name": update.first_name,
            "user.last_name": update.last_name,
            "user.usernames": update.usernames,
        }
        alias = _aliases(USERNAME_UNOBSERVED, update.usernames)
        if "username" in alias:
            values["user.username"] = alias["username"]
        return values
    if isinstance(update, types.UpdateUserPhone):
        return {"user.phone": update.phone or None}
    if isinstance(update, types.UpdateUserStatus):
        return {"user.status": update.status}
    if isinstance(update, types.UpdateUserEmojiStatus):
        return {"user.emoji_status": update.emoji_status}
    if isinstance(update, types.UpdatePeerBlocked):
        path = "full_user.blocked_my_stories_from" if update.blocked_my_stories_from else "full_user.blocked"
        return {path: bool(update.blocked)}
    return _specific_settings(update)


def _specific_settings(update: object) -> dict[str, object]:
    if isinstance(update, types.UpdatePeerHistoryTTL):
        return {"full_user.ttl_period": update.ttl_period}
    if isinstance(update, types.UpdatePeerSettings):
        return {"full_user.settings": update.settings}
    if isinstance(update, types.UpdatePeerWallpaper):
        return {
            "full_user.wallpaper": update.wallpaper,
            "full_user.wallpaper_overridden": bool(update.wallpaper_overridden),
        }
    if isinstance(update, types.UpdateNotifySettings):
        return {"full_user.notify_settings": update.notify_settings}
    return {}


def _specific_semantics(update: object) -> tuple[dict[str, object], dict[str, ProfileFieldCoverage]]:
    if isinstance(update, types.UpdateReadStories):
        root = "full_user.stories"
        return {root: {"constructor": "PeerStories", "max_read_id": update.max_id}}, {
            root + ".max_read_id": ProfileFieldCoverage(ProfileFieldClass.PRIVATE_FACT, ProfileFieldState.PRESENT, True)
        }
    fields: dict[str, object] = {}
    coverage: dict[str, ProfileFieldCoverage] = {}
    for path, value in _specific_values(update).items():
        normalized, evidence = _semantic_push_value(path, value)
        fields[path] = normalized
        if path in {
            "user.phone",
            "full_user.settings",
            "full_user.notify_settings",
            "full_user.blocked",
            "full_user.blocked_my_stories_from",
            "full_user.ttl_period",
            "full_user.wallpaper_overridden",
        }:
            evidence = {
                key: replace(item, classification=ProfileFieldClass.PRIVATE_FACT)
                if item.classification is ProfileFieldClass.PROFILE
                else item
                for key, item in evidence.items()
            }
        coverage.update(evidence)
    return fields, coverage


def _semantic_push_value(path: str, value: object) -> tuple[object, dict[str, ProfileFieldCoverage]]:
    if isinstance(value, (list, tuple)):
        values: list[object] = []
        coverage = {path: ProfileFieldCoverage(ProfileFieldClass.PROFILE, ProfileFieldState.PRESENT, True)}
        for index, item in enumerate(value):
            normalized, evidence = _semantic_push_value(f"{path}[{index}]", item)
            values.append(normalized)
            coverage.update(evidence)
        return values, coverage
    if isinstance(value, TLObject):
        fields, evidence, diagnostics = normalize_collection_item(value)
        coverage = {path + key[len("item") :]: item for key, item in evidence.items()}
        if path == "user.emoji_status" and isinstance(value, types.EmojiStatusEmpty):
            coverage[path] = ProfileFieldCoverage(ProfileFieldClass.PROFILE, ProfileFieldState.CLEARED, True)
        coverage.setdefault(
            path,
            ProfileFieldCoverage(
                ProfileFieldClass.PROFILE,
                ProfileFieldState.UNKNOWN if diagnostics else ProfileFieldState.PRESENT,
                not diagnostics,
            ),
        )
        return fields, coverage
    state = ProfileFieldState.CLEARED if value is None else ProfileFieldState.PRESENT
    return value, {path: ProfileFieldCoverage(ProfileFieldClass.PROFILE, state, True)}


def _supplied_story(update: object) -> ProfileCollectionItem | None:
    if isinstance(update, types.UpdateStory):
        fields, facts, diagnostics = normalize_collection_item(update.story)
        coverage = dict(facts)
        coverage["item"] = ProfileFieldCoverage(
            ProfileFieldClass.PROFILE,
            ProfileFieldState.PRESENT,
            not diagnostics
            and not bool(getattr(update.story, "min", False))
            and not isinstance(update.story, types.StoryItemSkipped),
        )
        identifier = fields.get("id")
        if type(identifier) is int and identifier > 0:
            return ProfileCollectionItem(
                str(identifier),
                fields,
                coverage,
                detail_complete=not diagnostics
                and not bool(getattr(update.story, "min", False))
                and not isinstance(update.story, types.StoryItemSkipped),
            )
    if isinstance(update, types.UpdateSentStoryReaction):
        reaction, coverage = _semantic_push_value("item.sent_reaction", update.reaction)
        return ProfileCollectionItem(
            str(update.story_id),
            {"constructor": "StoryItem", "id": update.story_id, "sent_reaction": reaction},
            coverage,
            detail_complete=False,
        )
    return None
