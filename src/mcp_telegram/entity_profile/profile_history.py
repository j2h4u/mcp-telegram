"""Observed meaningful changes, appended only after canonical merge acceptance.

The canonical caller owns account/order/authority fences and baseline comparison.
Inputs are capability-free canonical facts from the normalizer. This module
never acquires data, changes current facts, infers actors, or commits.
"""

import json
import math
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field

from ..sync_transactions import require_write_transaction
from .contracts import ObservationBoundary, ProfileFieldObservation

# Fixed semantic domains, not a rule inferred from primitive value types.
_PROFILE_ROOTS = frozenset(
    {
        "user.first_name",
        "user.last_name",
        "user.phone",
        "user.username",
        "user.usernames",
        "user.deleted",
        "user.bot",
        "user.verified",
        "user.support",
        "user.scam",
        "user.fake",
        "user.restricted",
        "user.restriction_reason",
        "user.premium",
        "user.emoji_status",
        "user.color",
        "user.profile_color",
        "user.bot_inline_placeholder",
        "user.bot_verification_icon",
        "user.send_paid_messages_stars",
        "user.linked_community_id",
        "full_user.about",
        "full_user.birthday",
        "full_user.business_work_hours",
        "full_user.business_location",
        "full_user.business_intro",
        "full_user.business_greeting_message",
        "full_user.business_away_message",
        "full_user.bot_info",
        "full_user.personal_channel_id",
        "full_user.personal_channel_message",
        "full_user.theme",
        "full_user.wallpaper",
        "full_user.starref_program",
        "full_user.send_paid_messages_stars",
        "full_user.bot_verification",
        "full_user.main_tab",
        # Compare an already comparable legacy composite as-is; never split it into v2 names.
        "legacy.name",
        "legacy.username",
    }
)
_OWNER_ROOTS = frozenset({"full_user.note", "owner.saved_contact_name"})
_PHOTO_ROOTS = {
    "user.photo": "current",
    "legacy.current_photo": "current",
    "full_user.profile_photo": "public",
    "full_user.personal_photo": "personal",
    "full_user.fallback_photo": "fallback",
}
_COLLECTIONS = frozenset(
    {"avatar_history", "common_chats", "saved_music", "saved_gifts", "current_stories", "pinned_stories"}
)
_MISSING = object()


@dataclass(frozen=True, slots=True)
class HistoryObservation:
    entity_id: int
    field: ProfileFieldObservation
    boundary: ObservationBoundary
    server_time: float | None = None

    def __post_init__(self) -> None:
        if type(self.entity_id) is not int or self.entity_id <= 0:
            raise ValueError("history requires a positive target")
        if self.server_time is not None and not math.isfinite(self.server_time):
            raise ValueError("history server time must be finite when explicitly supplied")


@dataclass(frozen=True, slots=True)
class ProfileHistoryMerge:
    observation: HistoryObservation
    before: Mapping[str, object]
    after: Mapping[str, object]
    accepted_paths: frozenset[str]
    comparable_paths: frozenset[str]
    previous_provenance: Mapping[str, object]
    comparison_values: Mapping[str, tuple[object, object]] = field(default_factory=dict)
    cleared_paths: frozenset[str] = field(default_factory=frozenset)
    previous_cleared_paths: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class CollectionHistoryMerge:
    observation: HistoryObservation
    section: str
    before: frozenset[str]
    after: frozenset[str]
    comparable: bool
    previous_provenance: Mapping[str, object]


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith((root + ".", root + "["))


def _history_root(path: str) -> str | None:
    return next((root for root in _PROFILE_ROOTS | _OWNER_ROOTS | _PHOTO_ROOTS.keys() if _under(path, root)), None)


def _photo_id(value: object, *, cleared: bool) -> object:
    if cleared or value is None:
        return None
    if isinstance(value, Mapping):
        if value.get("constructor") in {"PhotoEmpty", "UserProfilePhotoEmpty"}:
            return _MISSING
        photo_id = value.get("photo_id", value.get("id"))
        if type(photo_id) is int and photo_id > 0:
            return photo_id
    return _MISSING


def _previous_evidence(merge: ProfileHistoryMerge, root: str) -> dict[str, object]:
    evidence = {path: value for path, value in merge.previous_provenance.items() if _under(path, root)}
    if root in {"legacy.name", "legacy.username"}:
        evidence["before_precision"] = "legacy_normalized"
    return evidence


def _profile_change(merge: ProfileHistoryMerge, root: str) -> tuple[str, str, object, object] | None:
    # Unknown old values, including legacy stripped notes, establish a silent baseline.
    if root not in merge.comparable_paths or root not in merge.before or root not in merge.after:
        return None
    before, after = merge.before[root], merge.after[root]
    if root in _PHOTO_ROOTS:
        before = _photo_id(before, cleared=root in merge.previous_cleared_paths)
        after = _photo_id(after, cleared=root in merge.cleared_paths)
        if before is _MISSING or after is _MISSING:
            return None
        category = "private_owner" if _personal_photo(root, merge.before[root], merge.after[root]) else "profile"
        key = f"photo.{_PHOTO_ROOTS[root]}"
    else:
        category = "private_owner" if root in _OWNER_ROOTS else "profile"
        key = root
    return (key, category, before, after) if _different_values(merge, root, before, after) else None


def _different_values(merge: ProfileHistoryMerge, root: str, before: object, after: object) -> bool:
    comparison = merge.comparison_values.get(root)
    if root in {"legacy.name", "legacy.username"} and comparison is None:
        return False
    old_comparison, new_comparison = comparison if comparison is not None else (before, after)
    return old_comparison != new_comparison


def _personal_photo(root: str, before: object, after: object) -> bool:
    return root == "full_user.personal_photo" or any(
        isinstance(value, Mapping) and value.get("personal") is True for value in (before, after)
    )


def append_profile_history(conn: sqlite3.Connection, merge: ProfileHistoryMerge) -> int:
    """Use effective source-root values; accepted_paths may include nested leaves.

    comparable_paths names roots whose previous same-account representation is
    genuinely comparable, including known legacy values. Omit repaired legacy
    notes/unknown baselines. Caller excludes stale, rejected and nonauthoritative
    paths and passes PREVIOUS per-path provenance, not the new snapshot's age.
    Legacy names require comparison_values in their old normalized representation;
    payloads retain original precision. Empty photos require explicit clear evidence.
    """
    require_write_transaction(conn)
    if merge.observation.field.source == "min":
        return 0
    roots = {root for path in merge.accepted_paths if (root := _history_root(path)) is not None}
    inserted = 0
    for root in sorted(roots):
        change = _profile_change(merge, root)
        if change is not None:
            inserted += _append(conn, merge.observation, change, _previous_evidence(merge, root))
    return inserted


def append_collection_history(conn: sqlite3.Connection, merge: CollectionHistoryMerge) -> int:
    """Only caller-validated comparable complete publications with stable IDs.

    Anonymous gifts, partial scans, API nonreturn and first scans must set
    comparable=False. Membership never implies when a photo became an avatar.
    """
    require_write_transaction(conn)
    if not merge.comparable or merge.observation.field.source == "min" or merge.section not in _COLLECTIONS:
        return 0
    inserted = 0
    for key in sorted(merge.before ^ merge.after):
        inserted += _append(
            conn,
            merge.observation,
            (
                f"collection.{merge.section}.{key}",
                "collection",
                key if key in merge.before else None,
                key if key in merge.after else None,
            ),
            merge.previous_provenance,
        )
    return inserted


def _encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _append(
    conn: sqlite3.Connection,
    observation: HistoryObservation,
    change: tuple[str, str, object, object],
    previous_provenance: Mapping[str, object],
) -> int:
    key, category, before, after = change
    boundary = observation.boundary
    return conn.execute(
        "INSERT INTO entity_profile_change_events("
        "owner_account_id,entity_id,observation_order,change_key,category,before_json,after_json,"
        "previous_provenance_json,observation_started_at,observation_completed_at,observed_at,source,server_time) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(owner_account_id,entity_id,observation_order,change_key) DO NOTHING",
        (
            observation.field.owner_account_id,
            observation.entity_id,
            observation.field.order,
            key,
            category,
            _encode(before),
            _encode(after),
            _encode(previous_provenance),
            boundary.started_at if boundary.valid else None,
            boundary.completed_at if boundary.valid else None,
            observation.field.observed_at,
            observation.field.source,
            observation.server_time,
        ),
    ).rowcount
