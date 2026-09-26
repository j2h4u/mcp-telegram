"""Persistence owner for canonical dialog identity."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from typing import cast

from .dialog_identity_contracts import (
    IDENTITY_OMITTED,
    DialogIdentity,
    DialogIdentityObservation,
    ObservedDialogType,
    ObservedText,
)
from .models import DialogType

_SOURCES = frozenset({"directory", "realtime", "profile"})
type _JoinedIdentityRow = tuple[
    int,
    str | None,
    str | None,
    str | None,
    int | None,
    int,
    str | None,
    str | None,
    str | None,
    str | None,
]
type _ProfileRow = tuple[int, str, str | None, str | None]
type _StoredIdentityRow = tuple[
    str | None,
    str | None,
    str | None,
    int | None,
    str | None,
    int,
    int,
]


def _dialog_id(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("dialog_id must be an integer")
    return value


def _bundle(  # noqa: PLR0913, PLR0917
    dialog_id: int,
    name: str | None,
    username: str | None,
    raw_type: str | None,
    observed_at: int | None,
    complete: bool,
    source: str | None,
) -> DialogIdentity:
    dialog_type = DialogType.parse(raw_type)
    clean_name = name.strip() if isinstance(name, str) and name.strip() else None
    clean_username = username.strip().lstrip("@") if isinstance(username, str) and username.strip(" @") else None
    if clean_name:
        display_name, display_source = clean_name, "name"
    elif clean_username:
        display_name, display_source = f"@{clean_username}", "username"
    else:
        display_name, display_source = str(dialog_id), "numeric"
    return DialogIdentity(
        dialog_id,
        clean_name,
        clean_username,
        dialog_type,
        observed_at,
        bool(complete),
        source,
        display_name,
        display_source,
    )


def _is_material(name: str | None, username: str | None, raw_type: str | None) -> bool:
    return bool(
        (name and name.strip())
        or (username and username.strip())
        or DialogType.parse(raw_type) is not DialogType.UNKNOWN
    )


def _complete_profile(raw_type: str | None) -> bool:
    return DialogType.parse(raw_type) is not DialogType.UNKNOWN


def _select(rows: list[_JoinedIdentityRow]) -> dict[int, DialogIdentity]:
    result: dict[int, DialogIdentity] = {}
    for raw in rows:
        (raw_id, name, raw_type, username, observed_at, complete, source, entity_type, entity_name, entity_username) = (
            raw
        )
        dialog_id = _dialog_id(raw_id)
        canonical = (
            source is not None or observed_at is not None or bool(complete) or _is_material(name, username, raw_type)
        )
        if canonical:
            result[dialog_id] = _bundle(dialog_id, name, username, raw_type, observed_at, bool(complete), source)
        elif _complete_profile(entity_type):
            result[dialog_id] = _bundle(dialog_id, entity_name, entity_username, entity_type, None, False, "profile")
        else:
            result[dialog_id] = _bundle(dialog_id, None, None, None, None, False, None)
    return result


_READ_SQL = """SELECT d.dialog_id,d.name,d.type,d.username,d.identity_observed_at,d.identity_complete,
       d.identity_source,e.type,e.name,e.username
FROM dialogs d LEFT JOIN entities e ON e.id=d.dialog_id WHERE d.dialog_id IN ({})"""
_LOCAL_SQL = """SELECT d.dialog_id,d.name,d.type,d.username,d.identity_observed_at,d.identity_complete,
       d.identity_source,e.type,e.name,e.username
FROM dialogs d LEFT JOIN entities e ON e.id=d.dialog_id ORDER BY d.dialog_id"""


def capture_identity_baseline(conn: sqlite3.Connection, dialog_id: int) -> int | None:
    dialog_id = _dialog_id(dialog_id)
    row = cast(
        tuple[int] | None,
        conn.execute("SELECT identity_revision FROM dialogs WHERE dialog_id=?", (dialog_id,)).fetchone(),
    )
    return None if row is None else int(row[0])


def read_dialog_identities(conn: sqlite3.Connection, dialog_ids: Iterable[int]) -> dict[int, DialogIdentity]:
    ids = list(dict.fromkeys(_dialog_id(value) for value in dialog_ids))
    result: dict[int, DialogIdentity] = {}
    batch_size = conn.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)
    if batch_size < 1:
        raise ValueError("SQLite variable parameter limit must be positive")
    for start in range(0, len(ids), batch_size):
        chunk = ids[start : start + batch_size]
        placeholders = ",".join("?" for _ in chunk)
        result.update(_read_identity_chunk(conn, chunk, placeholders))
    return result


def _read_identity_chunk(conn: sqlite3.Connection, chunk: list[int], placeholders: str) -> dict[int, DialogIdentity]:
    rows = cast(list[_JoinedIdentityRow], conn.execute(_READ_SQL.format(placeholders), chunk).fetchall())
    result = _select(rows)
    missing = [dialog_id for dialog_id in chunk if dialog_id not in result]
    if not missing:
        return result
    marks = ",".join("?" for _ in missing)
    profiles = cast(
        list[_ProfileRow],
        conn.execute(f"SELECT id,type,name,username FROM entities WHERE id IN ({marks})", missing).fetchall(),
    )
    by_id = {row[0]: row for row in profiles}
    for dialog_id in missing:
        row = by_id.get(dialog_id)
        if row is not None and _complete_profile(row[1]):
            result[dialog_id] = _bundle(dialog_id, row[2], row[3], row[1], None, False, "profile")
        else:
            result[dialog_id] = _bundle(dialog_id, None, None, None, None, False, None)
    return result


def read_local_dialog_identities(conn: sqlite3.Connection) -> dict[int, DialogIdentity]:
    rows = cast(list[_JoinedIdentityRow], conn.execute(_LOCAL_SQL).fetchall())
    return _select(rows)


def _valid_field(value: object, name: str) -> None:
    if value is IDENTITY_OMITTED or value is None:
        return
    if name == "dialog_type":
        if not isinstance(value, (str, DialogType)):
            raise TypeError("dialog_type must be DialogType, str, None, or omitted")
    elif not isinstance(value, str):
        raise TypeError(f"{name} must be str, None, or omitted")


def _parse_observed_type(value: ObservedDialogType) -> DialogType:
    if value is IDENTITY_OMITTED:
        raise ValueError("dialog_type is omitted")
    return DialogType.parse(cast(str | DialogType | None, value))


def _validate_observation_identity(dialog_id: int, observation: DialogIdentityObservation) -> None:
    if not isinstance(observation, DialogIdentityObservation) or observation.dialog_id != dialog_id:
        raise ValueError("observation must describe the dialog_id being published")
    if observation.source not in _SOURCES:
        raise ValueError("source must be directory, realtime, or profile")


def _validate_observation_fields(observation: DialogIdentityObservation) -> bool:
    fields = (observation.name, observation.username, observation.dialog_type)
    for name, value in zip(("name", "username", "dialog_type"), fields, strict=True):
        _valid_field(value, name)
    complete_fields = all(value is not IDENTITY_OMITTED for value in fields)
    if observation.complete and not complete_fields:
        raise ValueError("complete identity observation must include name, username, and dialog_type")
    if observation.complete and observation.observed_at is None:
        raise ValueError("complete identity observation requires its observation time")
    return not all(value is IDENTITY_OMITTED for value in fields)


def _validate_observation(dialog_id: int, observation: DialogIdentityObservation) -> bool:
    _validate_observation_identity(dialog_id, observation)
    return _validate_observation_fields(observation)


def _validate_publication(
    dialog_id: int, observation: DialogIdentityObservation, expected_revision: int | None
) -> bool:
    if not _validate_observation(dialog_id, observation):
        return False
    if expected_revision is None:
        return False
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
        raise ValueError("expected_revision must be a non-negative integer or None")
    observed_at = observation.observed_at
    if observed_at is not None and (
        isinstance(observed_at, bool) or not isinstance(observed_at, int) or observed_at < 0
    ):
        raise ValueError("observed_at must be a non-negative integer or None")
    return True


def _observation_boundary(prior_at: int | None, observed_at: int | None, retained: bool) -> int | None:
    if retained and prior_at is None:
        return None
    if prior_at is None:
        return observed_at
    return prior_at if observed_at is None else min(prior_at, observed_at)


def _retain_or_observe(prior: str | None, value: ObservedText) -> str | None:
    return prior if value is IDENTITY_OMITTED else cast(str | None, value)


def _retains_prior_identity(
    observation: DialogIdentityObservation,
    prior_name: str | None,
    prior_username: str | None,
    prior_type: str | None,
) -> bool:
    return (
        (observation.name is IDENTITY_OMITTED and prior_name is not None)
        or (observation.username is IDENTITY_OMITTED and prior_username is not None)
        or (observation.dialog_type is IDENTITY_OMITTED and prior_type is not None)
    )


def _partial_identity_values(
    prior: _StoredIdentityRow,
    observation: DialogIdentityObservation,
    expected_revision: int,
) -> tuple[object, ...]:
    prior_name, prior_username, prior_type, prior_at, prior_source, _, prior_complete = prior
    name = _retain_or_observe(prior_name, observation.name)
    username = _retain_or_observe(prior_username, observation.username)
    raw_type = (
        prior_type
        if observation.dialog_type is IDENTITY_OMITTED
        else _parse_observed_type(observation.dialog_type).value
    )
    retained = _retains_prior_identity(observation, prior_name, prior_username, prior_type)
    mixed = retained and (
        prior_source is not None
        or prior_at is not None
        or bool(prior_complete)
        or _is_material(prior_name, prior_username, prior_type)
    )
    return (
        name,
        username,
        raw_type,
        _observation_boundary(prior_at, observation.observed_at, retained),
        0,
        "mixed" if mixed else observation.source,
        observation.dialog_id,
        expected_revision,
    )


def _publication_values(
    prior: _StoredIdentityRow,
    observation: DialogIdentityObservation,
    expected_revision: int,
) -> tuple[str, tuple[object, ...]]:
    values: tuple[object, ...]
    if observation.complete:
        values = (
            cast(str | None, observation.name),
            cast(str | None, observation.username),
            _parse_observed_type(observation.dialog_type).value,
            observation.observed_at,
            1,
            observation.source,
            observation.dialog_id,
            expected_revision,
        )
    else:
        values = _partial_identity_values(prior, observation, expected_revision)
    sql = """UPDATE dialogs SET name=?,username=?,type=?,identity_observed_at=?,identity_complete=?,
identity_source=?,identity_revision=identity_revision+1 WHERE dialog_id=? AND identity_revision=?"""
    return sql, values


def publish_dialog_identity(
    conn: sqlite3.Connection,
    dialog_id: int,
    observation: DialogIdentityObservation,
    expected_revision: int | None,
) -> bool:
    dialog_id = _dialog_id(dialog_id)
    if not _validate_publication(dialog_id, observation, expected_revision):
        return False
    if expected_revision is None:
        return False
    prior = cast(
        _StoredIdentityRow | None,
        conn.execute(
            "SELECT name,username,type,identity_observed_at,identity_source,identity_revision,identity_complete "
            "FROM dialogs WHERE dialog_id=?",
            (dialog_id,),
        ).fetchone(),
    )
    if prior is None or prior[5] != expected_revision:
        return False
    sql, values = _publication_values(prior, observation, expected_revision)
    return conn.execute(sql, values).rowcount == 1
