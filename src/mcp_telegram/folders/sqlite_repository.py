"""Atomic SQLite projection of observed folder rules onto canonical dialogs."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import replace
from typing import cast

from ..sync_transactions import require_write_transaction, write_savepoint
from .contracts import (
    RULE_TTL_SECONDS,
    DialogCategory,
    DialogFacts,
    FolderMembership,
    FolderRule,
    FolderRuleKind,
    FolderRuleObservation,
    FolderRuleUpdate,
    MembershipState,
    folder_observation_token,
)
from .membership import evaluate, pin_position
from .ports import FolderSnapshotRepository


def replace_folder_snapshot(
    conn: sqlite3.Connection, folders: Iterable[tuple[int, str]], memberships: Iterable[tuple[int, int]]
) -> None:
    """Compatibility fixture helper for pre-v67 callers."""
    folder_rows = tuple(folders)
    member_rows = tuple(memberships)
    with write_savepoint(conn):
        conn.execute("DELETE FROM telegram_folder_members")
        conn.execute("DELETE FROM telegram_folders")
        conn.executemany("INSERT INTO telegram_folders(folder_id, title) VALUES (?, ?)", folder_rows)
        conn.executemany("INSERT INTO telegram_folder_members(folder_id,dialog_id) VALUES (?,?)", member_rows)
        conn.execute("DELETE FROM telegram_folder_local_members")
        conn.execute("DELETE FROM telegram_folder_rules")
        conn.executemany(
            "INSERT INTO telegram_folder_rules(namespace,folder_id,title,rule_kind,source_position,rule_json) VALUES ('legacy',?,?, 'filter',?, ?)",
            [
                (
                    folder_id,
                    title,
                    position,
                    _encode_rule(FolderRule(folder_id, title, namespace="legacy", source_position=position)),
                )
                for position, (folder_id, title) in enumerate(folder_rows)
            ],
        )
        conn.executemany(
            "INSERT INTO telegram_folder_local_members(namespace,folder_id,dialog_id,state,pin_position) VALUES ('legacy',?,?, 'present',NULL)",
            member_rows,
        )


class SQLiteFolderSnapshotRepository(FolderSnapshotRepository):
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def for_connection(self, conn: sqlite3.Connection) -> SQLiteFolderSnapshotRepository:
        return SQLiteFolderSnapshotRepository(conn)

    def read_consecutive_failures(self) -> int:
        return _int_state(self._state("consecutive_failures")) or 0

    def read_last_outcome(self) -> str | None:
        return self._state("last_outcome")

    def read_last_success_at(self) -> int | None:
        return _int_state(self._state("rule_observation_started_at"))

    def read_next_retry_at(self) -> int | None:
        return _int_state(self._state("next_retry_at"))

    def read_snapshot_revision(self) -> tuple[int | None, int | None, str | None, int | None] | None:
        row = cast(
            tuple[object, object, object, object] | None,
            self._conn.execute(
                "SELECT account_id,canonical_generation,accepted_rule_token,rule_observation_started_at "
                "FROM telegram_folder_projection_state WHERE singleton=1"
            ).fetchone(),
        )
        if row is None:
            return None
        return (
            None if row[0] is None else _as_int(row[0]),
            None if row[1] is None else _as_int(row[1]),
            None if row[2] is None else str(row[2]),
            None if row[3] is None else _as_int(row[3]),
        )

    def read_catalog_identity(self) -> tuple[int, int, int] | None:
        return _canonical_receipt(self._conn)

    def read_catalog_version(self) -> tuple[int | None, int, str | None, bool]:
        row = cast(
            tuple[object, object, object, object],
            self._conn.execute(
                "SELECT account_id,catalog_revision,accepted_rule_token,coverage_status='complete' AND NOT EXISTS "
                "(SELECT 1 FROM telegram_folder_rules WHERE json_extract(rule_json,'$.complete') IS NOT 1) "
                "FROM telegram_folder_projection_state WHERE singleton=1"
            ).fetchone(),
        )
        return (
            None if row[0] is None else _as_int(row[0]),
            _as_int(row[1]),
            None if row[2] is None else str(row[2]),
            bool(row[3]),
        )

    def read_observation_order(self) -> int:
        row = cast(
            tuple[object] | None,
            self._conn.execute("SELECT value FROM daemon_state WHERE key='folder_observation_order'").fetchone(),
        )
        return 0 if row is None else _as_int(row[0])

    def refine_receipts(self, observation: FolderRuleObservation, *, account_id: int, order: int) -> None:
        from .command_receipts import refine_from_observation

        refine_from_observation(self._conn, account_id, order, folder_definitions(observation.rules), complete=True)

    def apply_supplied_update(self, update: FolderRuleUpdate, *, account_id: int, order: int, now: int) -> bool:
        """Apply a supplied filter delta only to its coherent retained catalog."""
        require_write_transaction(self._conn)
        canonical = self.read_catalog_identity()
        if canonical is None or canonical[0] != account_id or order <= self.read_observation_order():
            return False
        observation = self._accepted_observation()
        revision = self.read_snapshot_revision()
        if observation is None or revision is None or revision[0] != account_id:
            return False
        rules = _updated_folder_rules(observation.rules, update)
        if rules is None:
            return False
        rules = [replace(rule, source_position=i) for i, rule in enumerate(rules)]
        new_rules = tuple(rules)
        invalidated = self._state("coverage_status") == "unavailable"
        version = self.read_catalog_version()
        token = folder_observation_token(new_rules)
        self.project_observation(FolderRuleObservation(new_rules, token, observation.started_at), completed_at=now)
        if invalidated:
            self._conn.execute(
                "UPDATE telegram_folder_projection_state SET coverage_status='unavailable',catalog_revision=? WHERE singleton=1",
                (version[1] + int(token != version[2]),),
            )
        self._conn.execute(
            "INSERT INTO daemon_state(key,value) VALUES ('folder_observation_order',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(order),),
        )
        _refine_supplied_receipt(self._conn, update, account_id, order, new_rules)
        return True

    def rules_are_fresh(self, *, now: int) -> bool:
        started_at = self.read_last_success_at()
        covered = self._state("coverage_status") == "complete" or self._state("accepted_rule_token") is None
        return covered and started_at is not None and now < started_at + RULE_TTL_SECONDS

    def invalidate(self) -> None:
        require_write_transaction(self._conn)
        self._conn.execute(
            "UPDATE telegram_folder_projection_state SET catalog_revision=catalog_revision + "
            "CASE WHEN coverage_status='complete' THEN 1 ELSE 0 END,coverage_status='unavailable' WHERE singleton=1"
        )

    def next_mute_expiry(self) -> int | None:
        return _int_state(self._state("next_mute_expiry"))

    def project_observation(self, observation: FolderRuleObservation, *, completed_at: int) -> int | None:
        """Publish a rule receipt only when a coherent canonical directory exists."""
        with write_savepoint(self._conn):
            if not observation.complete or not all(rule.complete for rule in observation.rules):
                self.invalidate()
                return None
            canonical = _canonical_receipt(self._conn)
            if canonical is None:
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
        with write_savepoint(self._conn):
            return self.reproject_current_rules_in_transaction(now=now)

    def reproject_current_rules_in_transaction(self, *, now: int) -> int | None:
        """Local publication hook for the canonical directory transaction."""
        require_write_transaction(self._conn)
        canonical = _canonical_receipt(self._conn)
        accepted = self._accepted_observation()
        revision = self.read_snapshot_revision()
        if accepted is not None and canonical is not None and revision is not None and revision[0] != canonical[0]:
            self.invalidate()
            return None
        invalidated = accepted is not None and self._state("coverage_status") == "unavailable"
        previous_version = self.read_catalog_version()
        observation = accepted
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
        if invalidated:
            self._conn.execute(
                "UPDATE telegram_folder_projection_state SET coverage_status='unavailable',catalog_revision=? WHERE singleton=1",
                (previous_version[1],),
            )
        return generation

    def ensure_mute_projection(self, *, now: int) -> int | None:
        expiry = self.next_mute_expiry()
        if expiry is None or now < expiry:
            return None
        with write_savepoint(self._conn):
            expiry = self.next_mute_expiry()
            if expiry is None or now < expiry:
                return None
            return self.reproject_current_rules_in_transaction(now=now)

    def record_attempt(
        self, *, attempted_at: int, outcome: str, next_retry_at: int | None, consecutive_failures: int
    ) -> None:
        with write_savepoint(self._conn):
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
        require_write_transaction(self._conn)
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
            "UPDATE telegram_folder_projection_state SET catalog_revision=catalog_revision + CASE WHEN "
            "account_id IS NOT ? OR accepted_rule_token IS NOT ? OR coverage_status != 'complete' THEN 1 ELSE 0 END, "
            "account_id=?,canonical_generation=?,accepted_rule_token=?,"
            "rule_observation_started_at=?,completed_at=?,canonical_observed_at=?,next_mute_expiry=?,coverage_status='complete',"
            "last_attempt_at=?,last_outcome='success',next_retry_at=NULL,consecutive_failures=0 WHERE singleton=1",
            (
                account_id,
                observation.token,
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

    def _accepted_observation(self) -> FolderRuleObservation | None:
        rows = cast(
            list[tuple[str]],
            self._conn.execute("SELECT rule_json FROM telegram_folder_rules ORDER BY source_position").fetchall(),
        )
        state = cast(
            tuple[str | None, int | None] | None,
            self._conn.execute(
                "SELECT accepted_rule_token,rule_observation_started_at FROM telegram_folder_projection_state WHERE singleton=1"
            ).fetchone(),
        )
        if state is None or state[0] is None or state[1] is None:
            return None
        return FolderRuleObservation(tuple(_decode_rule(raw) for (raw,) in rows), str(state[0]), int(state[1]))

    def _state(self, column: str) -> str | None:
        row = cast(
            tuple[object] | None,
            self._conn.execute(f"SELECT {column} FROM telegram_folder_projection_state WHERE singleton=1").fetchone(),
        )
        return None if row is None or row[0] is None else str(row[0])


def _refine_supplied_receipt(
    conn: sqlite3.Connection, update: FolderRuleUpdate, account_id: int, order: int, rules: tuple[FolderRule, ...]
) -> None:
    from .command_receipts import refine_from_observation

    if update.kind == "filter":
        refine_from_observation(
            conn, account_id, order, folder_definitions(rules), complete=False, affected_folder_id=update.folder_id
        )


def _updated_folder_rules(rules: tuple[FolderRule, ...], update: FolderRuleUpdate) -> list[FolderRule] | None:
    if update.kind == "filter":
        return _update_one_folder(list(rules), update)
    if update.kind == "order":
        by_id = {rule.folder_id: rule for rule in rules if rule.namespace == "filter"}
        if len(update.order) != len(by_id) or set(update.order) != set(by_id):
            return None
        ordered = iter(by_id[folder_id] for folder_id in update.order)
        return [next(ordered) if rule.namespace == "filter" else rule for rule in rules]
    return None


def _update_one_folder(rules: list[FolderRule], update: FolderRuleUpdate) -> list[FolderRule] | None:
    folder_id = update.folder_id
    value = update.rule
    if folder_id is None:
        return None
    index = next(
        (i for i, rule in enumerate(rules) if rule.namespace == "filter" and rule.folder_id == folder_id), len(rules)
    )
    if value is None:
        return rules[:index] + rules[index + 1 :]
    rule = value
    if not rule.complete or rule.folder_id != folder_id:
        return None
    return [*rules[:index], rule, *rules[index + 1 :]]


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
            "categories": sorted(item.value for item in rule.categories),
            "exclude_archived": rule.exclude_archived,
            "exclude_read": rule.exclude_read,
            "exclude_muted": rule.exclude_muted,
            "title_entities": rule.title_entities,
            "title_noanimate": rule.title_noanimate,
            "emoticon": rule.emoticon,
            "color": rule.color,
            "has_my_invites": rule.has_my_invites,
            "complete": rule.complete,
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
        tuple(cast(list[dict[str, object]], value["title_entities"])),
        bool(value["title_noanimate"]),
        cast(str | None, value["emoticon"]),
        cast(int | None, value["color"]),
        bool(value["has_my_invites"]),
        bool(value["complete"]),
    )


def read_folder_rules(conn: sqlite3.Connection) -> tuple[FolderRule, ...]:
    rows = cast(
        list[tuple[str]],
        conn.execute("SELECT rule_json FROM telegram_folder_rules ORDER BY source_position").fetchall(),
    )
    return tuple(_decode_rule(raw) for (raw,) in rows)


def encode_folder_definition(rule: FolderRule) -> str:
    return _encode_rule(replace(rule, source_position=0))


def folder_definitions(rules: tuple[FolderRule, ...]) -> dict[int, str]:
    return {rule.folder_id: encode_folder_definition(rule) for rule in rules if rule.namespace == "filter"}


def decode_folder_definition(raw: str) -> FolderRule:
    return _decode_rule(raw)


def _int_state(value: str | None) -> int | None:
    return None if value is None else int(value)


def _as_int(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        return int(value)
    raise TypeError("folder projection integer field has an invalid type")
