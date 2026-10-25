"""Canonical merge-to-journal adapter; never records an attempted observation."""

import sqlite3
from collections.abc import Mapping

from .collection_repository import EffectiveProfileMerge
from .contracts import ProfileFieldObservation, ProfileFieldState
from .profile_history import HistoryObservation, ProfileHistoryMerge, append_profile_history


def append_effective_history(
    conn: sqlite3.Connection,
    merge: EffectiveProfileMerge,
    observation: ProfileFieldObservation,
    legacy_before: Mapping[str, object],
    legacy_evidence: Mapping[str, object] | None = None,
) -> int:
    previous_paths = merge.previous_provenance.get("fields", {})
    previous_paths = previous_paths if isinstance(previous_paths, dict) else {}
    comparisons = _comparison_bases(merge, previous_paths)
    change = ProfileHistoryMerge(
        HistoryObservation(merge.snapshot.target_id, observation, merge.snapshot.observation),
        merge.before,
        merge.after,
        frozenset(path for path in merge.accepted_paths if _authoritative(merge, path)),
        frozenset(comparisons),
        previous_paths,
        comparison_values=comparisons,
        cleared_paths=frozenset(
            path
            for path, value in merge.snapshot.coverage.items()
            if value.authoritative and value.state is ProfileFieldState.CLEARED
        ),
        previous_cleared_paths=_previous_clears(merge.previous_coverage),
    )
    return append_profile_history(conn, change) + _legacy_history(conn, change, legacy_before, legacy_evidence or {})


_MISSING = object()


def _prior_authority(value: object) -> bool:
    return (
        isinstance(value, dict) and value.get("authoritative") is True and value.get("state") in {"present", "cleared"}
    )


def _comparison_bases(
    merge: EffectiveProfileMerge, previous_paths: Mapping[str, object]
) -> dict[str, tuple[object, object]]:
    result: dict[str, tuple[object, object]] = {}
    for root in merge.before:
        if root not in merge.after or _bridged(previous_paths.get(root)):
            continue
        old = merge.previous_coverage.get(root)
        if isinstance(old, dict) and old.get("state") == "api_not_returned":
            continue
        basis = _root_comparison(merge, root)
        if basis is not None:
            result[root] = basis
    return result


def _whole_root_known(coverage: Mapping[str, object], root: str) -> bool:
    children = [value for path, value in coverage.items() if path.startswith((root + ".", root + "["))]
    return _prior_authority(coverage.get(root)) and all(
        _prior_authority(value) or (isinstance(value, dict) and value.get("state") == "inapplicable")
        for value in children
    )


def _known_leaf_paths(merge: EffectiveProfileMerge, root: str) -> set[str]:
    # Positional children of an incomplete vector cannot establish cross-object comparability.
    return {
        path
        for path, value in merge.previous_coverage.items()
        if path.startswith(root + ".")
        and _prior_authority(value)
        and path in merge.accepted_paths
        and _authoritative(merge, path)
        and "[" not in path
    }


def _root_comparison(merge: EffectiveProfileMerge, root: str) -> tuple[object, object] | None:
    if _whole_root_known(merge.previous_coverage, root):
        return merge.before[root], merge.after[root]
    before = {path: _path_value(merge.before[root], root, path) for path in _known_leaf_paths(merge, root)}
    before = {path: value for path, value in before.items() if value is not _MISSING}
    if not before:
        return None
    after = {path: _path_value(merge.after[root], root, path) for path in before}
    return before, after


def _path_value(value: object, root: str, path: str) -> object:
    for name in path[len(root) + 1 :].split("."):
        if not isinstance(value, Mapping) or name not in value:
            return _MISSING
        value = value[name]
    return value


def _authoritative(merge: EffectiveProfileMerge, path: str) -> bool:
    return path in merge.snapshot.coverage and merge.snapshot.coverage[path].authoritative


def _previous_clears(coverage: Mapping[str, object]) -> frozenset[str]:
    return frozenset(
        path
        for path, value in coverage.items()
        if isinstance(value, dict) and value.get("state") == "cleared" and value.get("authoritative") is True
    )


def _bridged(value: object) -> bool:
    return isinstance(value, dict) and value.get("reason") == "legacy_alias_bridge"


def _accepted(change: ProfileHistoryMerge, root: str) -> bool:
    return any(path == root or path.startswith((root + ".", root + "[")) for path in change.accepted_paths)


def _legacy_name(change: ProfileHistoryMerge, legacy: Mapping[str, object]) -> tuple[object, object, object] | None:
    if not isinstance(legacy.get("name"), str) or "user.first_name" in change.comparable_paths:
        return None
    if not (_accepted(change, "user.first_name") and _accepted(change, "user.last_name")):
        return None
    parts = [change.after.get("user.first_name"), change.after.get("user.last_name")]
    if not all(part is None or isinstance(part, str) for part in parts):
        return None
    raw, normalized = _name_representations(parts)
    return legacy["name"], raw, normalized


def _name_representations(parts: list[object]) -> tuple[str, str]:
    text = [part for part in parts if isinstance(part, str)]
    return " ".join(part for part in text if part), " ".join(part.strip() for part in text if part.strip())


def _legacy_history(
    conn: sqlite3.Connection, change: ProfileHistoryMerge, legacy: Mapping[str, object], evidence: Mapping[str, object]
) -> int:
    before: dict[str, object] = {}
    after: dict[str, object] = {}
    comparisons: dict[str, tuple[object, object]] = {}
    name = _legacy_name(change, legacy)
    if name is not None:
        old, raw, normalized = name
        before["legacy.name"], after["legacy.name"] = old, raw
        comparisons["legacy.name"] = (old, normalized)
    _legacy_aliases(change, legacy, before, after, comparisons)
    return append_profile_history(
        conn,
        ProfileHistoryMerge(
            change.observation,
            before,
            after,
            frozenset(after),
            frozenset(before),
            {key: evidence[key] for key in before if key in evidence},
            comparisons,
            cleared_paths=frozenset({"legacy.current_photo"}) if "user.photo" in change.cleared_paths else frozenset(),
        ),
    )


def _legacy_aliases(
    change: ProfileHistoryMerge,
    legacy: Mapping[str, object],
    before: dict[str, object],
    after: dict[str, object],
    comparisons: dict[str, tuple[object, object]],
) -> None:
    for alias, root in (("username", "user.username"), ("current_photo", "user.photo")):
        if alias not in legacy or root in change.comparable_paths or not _accepted(change, root):
            continue
        if alias == "username" and not isinstance(legacy[alias], str):
            continue
        key = "legacy." + alias
        before[key], after[key] = legacy[alias], change.after.get(root)
        if alias == "username":
            value = after[key]
            comparisons[key] = (legacy[alias], value.strip() if isinstance(value, str) else value)
