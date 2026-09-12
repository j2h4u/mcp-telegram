"""Atomic SQLite projection of observed folder rules onto canonical dialogs."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from typing import cast

from .contracts import (
    RULE_TTL_SECONDS,
    DialogCategory,
    DialogFacts,
    FolderMembership,
    FolderRule,
    FolderRuleKind,
    FolderRuleObservation,
    MembershipState,
)
from .membership import evaluate, pin_position
from .ports import FolderSnapshotRepository


def replace_folder_snapshot(
    conn: sqlite3.Connection, folders: Iterable[tuple[int, str]], memberships: Iterable[tuple[int, int]]
) -> None:
    """Compatibility fixture helper for pre-v67 callers."""
    folder_rows = tuple(folders)
    member_rows = tuple(memberships)
    with conn:
        conn.execute("DELETE FROM telegram_folder_members")
        conn.execute("DELETE FROM telegram_folders")
        conn.executemany("INSERT INTO telegram_folders(folder_id, title) VALUES (?, ?)", folder_rows)
        conn.executemany("INSERT INTO telegram_folder_members(folder_id,dialog_id) VALUES (?,?)", member_rows)
        conn.execute("DELETE FROM telegram_folder_local_members")
        conn.execute("DELETE FROM telegram_folder_rules")
        conn.executemany(
            "INSERT INTO telegram_folder_rules(namespace,folder_id,title,rule_kind,source_position,rule_json) VALUES ('legacy',?,?, 'filter',?, '{}')",
            [(folder_id, title, position) for position, (folder_id, title) in enumerate(folder_rows)],
        )
        conn.executemany(
            "INSERT INTO telegram_folder_local_members(namespace,folder_id,dialog_id,state,pin_position) VALUES ('legacy',?,?, 'present',NULL)",
            member_rows,
        )


class SQLiteFolderSnapshotRepository(FolderSnapshotRepository):
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def read_consecutive_failures(self) -> int:
        return _int_state(self._state("consecutive_failures")) or 0

    def read_last_outcome(self) -> str | None:
        return self._state("last_outcome")

    def read_last_success_at(self) -> int | None:
        accepted = _int_state(self._state("rule_observation_started_at"))
        if accepted is not None:
            return accepted
        row = cast(
            tuple[object] | None,
            self._conn.execute(
                "SELECT started_at FROM telegram_folder_pending_observation WHERE singleton=1"
            ).fetchone(),
        )
        return None if row is None or row[0] is None else _as_int(row[0])

    def read_next_retry_at(self) -> int | None:
        return _int_state(self._state("next_retry_at"))

    def rules_are_fresh(self, *, now: int) -> bool:
        started_at = self.read_last_success_at()
        return started_at is not None and now < started_at + RULE_TTL_SECONDS

    def next_mute_expiry(self) -> int | None:
        return _int_state(self._state("next_mute_expiry"))

    def project_observation(self, observation: FolderRuleObservation, *, completed_at: int) -> int | None:
        """Publish a rule receipt only when a coherent canonical directory exists."""
        with self._conn:
            canonical = _canonical_receipt(self._conn)
            if canonical is None:
                self._save_pending(observation)
                return None
            account_id, generation, canonical_observed_at = canonical
            self._replace_projection(
                observation,
                account_id=account_id,
                generation=generation,
                canonical_observed_at=canonical_observed_at,
                completed_at=completed_at,
            )
            return generation

    def reproject_current_rules(self, *, now: int) -> int | None:
        """Re-evaluate retained accepted rules with no Telegram RPC or clock rewrite."""
        with self._conn:
            return self.reproject_current_rules_in_transaction(now=now)

    def reproject_current_rules_in_transaction(self, *, now: int) -> int | None:
        """Local publication hook for the canonical directory transaction."""
        canonical = _canonical_receipt(self._conn)
        observation = self._accepted_observation() or self._pending_observation()
        if canonical is None or observation is None:
            return None
        account_id, generation, canonical_observed_at = canonical
        self._replace_projection(
            observation,
            account_id=account_id,
            generation=generation,
            canonical_observed_at=canonical_observed_at,
            completed_at=now,
        )
        return generation

    def ensure_mute_projection(self, *, now: int) -> int | None:
        expiry = self.next_mute_expiry()
        if expiry is None or now < expiry:
            return None
        return self.reproject_current_rules(now=now)

    def record_attempt(
        self, *, attempted_at: int, outcome: str, next_retry_at: int | None, consecutive_failures: int
    ) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE telegram_folder_projection_state SET last_attempt_at=?,last_outcome=?,next_retry_at=?,consecutive_failures=? WHERE singleton=1",
                (attempted_at, outcome, next_retry_at, consecutive_failures),
            )

    def _replace_projection(
        self,
        observation: FolderRuleObservation,
        *,
        account_id: int,
        generation: int,
        canonical_observed_at: int,
        completed_at: int,
    ) -> None:
        facts = _canonical_facts(self._conn)
        members = _evaluate_rules(
            self._conn, observation.rules, facts, _published_main_pins(self._conn), now=completed_at
        )
        self._conn.execute("DELETE FROM telegram_folder_local_members")
        self._conn.execute("DELETE FROM telegram_folder_rules")
        self._conn.executemany(
            "INSERT INTO telegram_folder_rules(namespace,folder_id,title,rule_kind,source_position,rule_json) VALUES (?,?,?,?,?,?)",
            [
                (rule.namespace, rule.folder_id, rule.title, rule.kind.value, rule.source_position, _encode_rule(rule))
                for rule in observation.rules
            ],
        )
        self._conn.executemany(
            "INSERT INTO telegram_folder_local_members(namespace,folder_id,dialog_id,state,pin_position) VALUES (?,?,?,?,?)",
            [
                (member.namespace, member.folder_id, member.dialog_id, member.state.value, member.pin_position)
                for member in members
            ],
        )
        next_mute_expiry = min(
            (
                facts_item.mute_until
                for facts_item in facts.values()
                if facts_item.mute_until is not None and facts_item.mute_until > completed_at
            ),
            default=None,
        )
        self._conn.execute(
            "UPDATE telegram_folder_projection_state SET account_id=?,canonical_generation=?,accepted_rule_token=?,"
            "rule_observation_started_at=?,completed_at=?,canonical_observed_at=?,next_mute_expiry=?,coverage_status='complete',"
            "last_attempt_at=?,last_outcome='success',next_retry_at=NULL,consecutive_failures=0 WHERE singleton=1",
            (
                account_id,
                generation,
                observation.token,
                observation.started_at,
                completed_at,
                canonical_observed_at,
                next_mute_expiry,
                completed_at,
            ),
        )
        self._conn.execute("DELETE FROM telegram_folder_pending_observation")

    def _save_pending(self, observation: FolderRuleObservation) -> None:
        row = cast(
            tuple[object, object] | None,
            self._conn.execute(
                "SELECT token,started_at FROM telegram_folder_pending_observation WHERE singleton=1"
            ).fetchone(),
        )
        if row is None:
            self._conn.execute(
                "INSERT INTO telegram_folder_pending_observation(singleton,token,started_at,rules_json) VALUES (1,?,?,?)",
                (observation.token, observation.started_at, _encode_observation(observation)),
            )
        elif observation.started_at >= _as_int(row[1]):
            self._conn.execute(
                "UPDATE telegram_folder_pending_observation SET token=?,started_at=?,rules_json=? WHERE singleton=1",
                (observation.token, observation.started_at, _encode_observation(observation)),
            )
        self._conn.execute(
            "UPDATE telegram_folder_projection_state SET last_attempt_at=?,last_outcome='success',next_retry_at=NULL,consecutive_failures=0,coverage_status='unavailable' WHERE singleton=1",
            (observation.started_at,),
        )

    def _accepted_observation(self) -> FolderRuleObservation | None:
        rows = cast(
            list[tuple[str]],
            self._conn.execute("SELECT rule_json FROM telegram_folder_rules ORDER BY source_position").fetchall(),
        )
        if not rows:
            return None
        state = cast(
            tuple[str | None, int | None] | None,
            self._conn.execute(
                "SELECT accepted_rule_token,rule_observation_started_at FROM telegram_folder_projection_state WHERE singleton=1"
            ).fetchone(),
        )
        if state is None or state[0] is None or state[1] is None:
            return None
        return FolderRuleObservation(tuple(_decode_rule(raw) for (raw,) in rows), str(state[0]), int(state[1]))

    def _pending_observation(self) -> FolderRuleObservation | None:
        row = cast(
            tuple[object, object, object] | None,
            self._conn.execute(
                "SELECT token,started_at,rules_json FROM telegram_folder_pending_observation WHERE singleton=1"
            ).fetchone(),
        )
        if row is None:
            return None
        payload = cast(dict[str, object], json.loads(str(row[2])))
        rules = tuple(_decode_rule(str(item)) for item in cast(list[object], payload["rules"]))
        return FolderRuleObservation(rules, str(row[0]), _as_int(row[1]))

    def _state(self, column: str) -> str | None:
        row = cast(
            tuple[object] | None,
            self._conn.execute(f"SELECT {column} FROM telegram_folder_projection_state WHERE singleton=1").fetchone(),
        )
        return None if row is None or row[0] is None else str(row[0])


def _canonical_receipt(conn: sqlite3.Connection) -> tuple[int, int, int] | None:
    row = cast(
        tuple[object, object, object] | None,
        conn.execute(
            "SELECT account_id,generation,observation_started_at FROM dialog_directory_publication WHERE singleton=1"
        ).fetchone(),
    )
    if row is None or any(value is None for value in row):
        return None
    return _as_int(row[0]), _as_int(row[1]), _as_int(row[2])


def _canonical_facts(conn: sqlite3.Connection) -> dict[int, DialogFacts]:
    rows = cast(
        list[tuple[int, str | None, int | None, int | None, int | None, int | None]],
        conn.execute(
            "SELECT dialog_id,category,archived,unread,mute_until,observed_at FROM dialog_directory_facts"
        ).fetchall(),
    )
    return {
        dialog_id: DialogFacts(
            dialog_id,
            None if category is None else DialogCategory(category),
            None if archived is None else bool(archived),
            None if unread is None else bool(unread),
            mute_until,
            observed_at,
        )
        for dialog_id, category, archived, unread, mute_until, observed_at in rows
    }


def _published_main_pins(conn: sqlite3.Connection) -> dict[int, int]:
    rows = cast(
        list[tuple[int, int]],
        conn.execute("SELECT dialog_id,position FROM dialog_directory_published_pins WHERE folder_id=0").fetchall(),
    )
    return dict(rows)


def _evaluate_rules(
    conn: sqlite3.Connection,
    rules: tuple[FolderRule, ...],
    facts: dict[int, DialogFacts],
    main_pins: dict[int, int],
    *,
    now: int,
) -> tuple[FolderMembership, ...]:
    visible_dialogs = {
        dialog_id: None if archived is None else bool(archived)
        for dialog_id, archived in cast(
            list[tuple[int, int | None]],
            # A published dialog can lack optional eligibility facts. It must
            # still be represented in three-valued folder membership.
            conn.execute("SELECT dialog_id,archived FROM dialogs WHERE hidden=0").fetchall(),
        )
    }
    result: list[FolderMembership] = []
    for rule in rules:
        candidate_ids = set(visible_dialogs)
        candidate_ids.update(rule.explicit_ids)
        for dialog_id in candidate_ids:
            facts_item = facts.get(dialog_id, DialogFacts(dialog_id, archived=visible_dialogs.get(dialog_id)))
            if facts_item.archived is None and dialog_id in visible_dialogs:
                facts_item = DialogFacts(
                    dialog_id,
                    facts_item.category,
                    visible_dialogs[dialog_id],
                    facts_item.unread,
                    facts_item.mute_until,
                    facts_item.observed_at,
                )
            state = evaluate(rule, facts_item, now=now)
            if state is MembershipState.ABSENT:
                continue
            position = pin_position(rule, dialog_id)
            if rule.kind is FolderRuleKind.DEFAULT:
                position = main_pins.get(dialog_id)
            result.append(FolderMembership(rule.namespace, rule.folder_id, dialog_id, state, position))
    return tuple(result)


def _encode_rule(rule: FolderRule) -> str:
    return json.dumps(
        {
            "id": rule.folder_id,
            "title": rule.title,
            "namespace": rule.namespace,
            "kind": rule.kind.value,
            "position": rule.source_position,
            "include": rule.included_ids,
            "pins": rule.pinned_ids,
            "exclude": rule.excluded_ids,
            "categories": [item.value for item in rule.categories],
            "exclude_archived": rule.exclude_archived,
            "exclude_read": rule.exclude_read,
            "exclude_muted": rule.exclude_muted,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _decode_rule(raw: str) -> FolderRule:
    value = cast(dict[str, object], json.loads(raw))
    return FolderRule(
        _as_int(value["id"]),
        str(value["title"]),
        str(value["namespace"]),
        FolderRuleKind(str(value["kind"])),
        _as_int(value["position"]),
        tuple(_as_int(item) for item in cast(list[object], value["include"])),
        tuple(_as_int(item) for item in cast(list[object], value["pins"])),
        tuple(_as_int(item) for item in cast(list[object], value["exclude"])),
        frozenset(DialogCategory(str(item)) for item in cast(list[object], value["categories"])),
        bool(value["exclude_archived"]),
        bool(value["exclude_read"]),
        bool(value["exclude_muted"]),
    )


def _encode_observation(observation: FolderRuleObservation) -> str:
    return json.dumps(
        {
            "token": observation.token,
            "started_at": observation.started_at,
            "rules": [_encode_rule(rule) for rule in observation.rules],
        }
    )


def _int_state(value: str | None) -> int | None:
    return None if value is None else int(value)


def _as_int(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        return int(value)
    raise TypeError("folder projection integer field has an invalid type")


def reproject_due_mutes(conn: sqlite3.Connection, *, now: int) -> int | None:
    """Read-side boundary hook: a mute expiry changes local truth without RPC."""
    try:
        return SQLiteFolderSnapshotRepository(conn).ensure_mute_projection(now=now)
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        return None
