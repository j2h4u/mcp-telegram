"""Project already accepted FullUser story facts into the existing page owner."""

from collections.abc import Mapping
from typing import cast

from .collection_repository import EffectiveProfileMerge
from .contracts import ProfileCollectionItem, ProfileCollectionPage, SuppliedProfileUser


def _story_vector(merge: EffectiveProfileMerge) -> list[object] | None:
    root = "full_user.stories"
    value = merge.after.get(root)
    if not isinstance(value, Mapping) or not merge.snapshot.observation.valid:
        return None
    evidence = merge.snapshot.coverage.get(root + ".stories")
    if evidence is None or evidence.state.value != "present" or root + ".stories" not in merge.accepted_paths:
        return None
    peer = value.get("peer")
    if not isinstance(peer, Mapping) or peer.get("user_id") != merge.snapshot.target_id:
        return None
    stories = value.get("stories")
    return cast(list[object], stories) if isinstance(stories, list) else None


def _story_item(merge: EffectiveProfileMerge, story: object, index: int) -> ProfileCollectionItem | None:
    if not isinstance(story, dict) or type(story.get("id")) is not int:
        return None
    prefix = f"full_user.stories.stories[{index}]"
    coverage = {
        "item" + path[len(prefix) :]: fact
        for path, fact in merge.snapshot.coverage.items()
        if path == prefix or path.startswith((prefix + ".", prefix + "["))
    }
    complete = (
        story.get("constructor") != "StoryItemSkipped"
        and bool(coverage)
        and all(item.authoritative or item.state.value == "inapplicable" for item in coverage.values())
    )
    return ProfileCollectionItem(str(story["id"]), story, coverage, detail_complete=complete)


def supplied_current_stories(merge: EffectiveProfileMerge) -> ProfileCollectionPage | None:
    stories = _story_vector(merge)
    if stories is None:
        return None
    items: list[ProfileCollectionItem] = []
    missing: list[int] = []
    for index, story in enumerate(stories):
        item = _story_item(merge, story, index)
        if item is None:
            return None
        if not item.detail_complete:
            missing.append(int(item.key))
        items.append(item)
    continuation = (
        {"phase": "details", "ids": missing, "after": None, "reported_count": None, "listing_terminal": True}
        if missing
        else None
    )
    value = cast(Mapping[str, object], merge.after["full_user.stories"])
    return ProfileCollectionPage(
        merge.snapshot.target_id,
        "current_stories",
        tuple(items),
        merge.snapshot.observation,
        next_cursor=continuation,
        terminal=not missing,
        detail_ids=tuple(missing),
        metadata={key: value[key] for key in ("max_read_id", "chat_notifications_enabled") if key in value},
    )


def summary_fingerprint(path: str, value: object) -> object:
    """Only configured membership/role summaries invalidate catalog acquisition."""
    if not isinstance(value, Mapping):
        return value
    if path in {"user.photo", "full_user.profile_photo", "full_user.personal_photo", "full_user.fallback_photo"}:
        return value.get("photo_id", value.get("id")), value.get("personal")
    if path == "user.stories_max_id":
        return value.get("max_id")
    if path == "full_user.saved_music":
        return value.get("id")
    if path == "full_user.stories":
        stories = value.get("stories", [])
        return (
            [(item.get("id"), item.get("pinned")) for item in stories if isinstance(item, dict)]
            if isinstance(stories, list)
            else None
        )
    return value


def authorized_legacy_user_fields(user: SuppliedProfileUser) -> dict[str, object]:
    """Apply the semantic authority mask to compatible legacy projection fields."""
    return {
        name: value
        for name, value in user.legacy_fields.items()
        if _legacy_authority(user, name) or (user.minimal and name != "name" and _positive_partial(value))
    }


def _positive_partial(value: object) -> bool:
    return value is not None and value is not False and value not in ("", [], {})


def _legacy_authority(user: SuppliedProfileUser, name: str) -> bool:
    if name == "name":
        return all(
            path in user.coverage and user.coverage[path].authoritative
            for path in ("user.first_name", "user.last_name")
        )
    alias = {"current_photo": "photo", "extra_usernames": "usernames", "emoji_status_id": "emoji_status"}.get(
        name, name
    )
    for prefix in ("user.", "full_user."):
        evidence = user.coverage.get(prefix + alias)
        if evidence is not None:
            if evidence.authoritative:
                return True
            component = {"current_photo": "photo_id", "emoji_status_id": "document_id"}.get(name)
            leaf = user.coverage.get(prefix + alias + "." + component) if component else None
            return leaf is not None and leaf.authoritative
    return False
