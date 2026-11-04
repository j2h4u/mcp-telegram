"""Atomic SQLite projection of observed folder rules onto canonical dialogs."""

from __future__ import annotations

import json
import sqlite3
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
                "(SELECT 1 FROM telegram_folder_rules r WHERE r.account_id=telegram_folder_projection_state.account_id "
                "AND r.deleted=0 AND r.catalog_member IS NOT 0 AND json_extract(r.rule_json,'$.complete') IS NOT 1) "
                "FROM telegram_folder_projection_state WHERE singleton=1"
            ).fetchone(),
        )
        return (
            None if row[0] is None else _as_int(row[0]),
            _as_int(row[1]),
            None if row[2] is None else str(row[2]),
            bool(row[3]),
        )

    def refine_receipts(self, observation: FolderRuleObservation, *, account_id: int, order: int) -> None:
        from .command_receipts import refine_from_observation

        refine_from_observation(self._conn, account_id, order, folder_definitions(observation.rules), complete=True)

    def apply_supplied_update(self, update: FolderRuleUpdate, *, account_id: int, order: int, now: int) -> bool:
        """Persist a supplied rule even when catalogue enumeration is incomplete."""
        require_write_transaction(self._conn)
        self._bind_projection_account(account_id)
        if update.kind == "filter":
            return self._apply_rule_delta(update, account_id=account_id, order=order, now=now)
        return self._apply_order_delta(update, account_id=account_id, order=order)

    def _apply_order_delta(self, update: FolderRuleUpdate, *, account_id: int, order: int) -> bool:
        if update.kind != "order":
            return False
        last_order = _int_state(self._state("filter_order_observation_order")) or 0
        if order <= last_order:
            return True
        if len(update.order) != len(set(update.order)):
            return False
        self._apply_known_order_positions(update.order, account_id=account_id, order=order)
        self._conn.execute(
            "UPDATE telegram_folder_projection_state SET membership_revision=membership_revision+1,"
            "catalog_revision=catalog_revision+1,coverage_status='unavailable',rule_enumeration_complete=0,filter_order_json=?,"
            "filter_order_observation_order=? WHERE singleton=1",
            (json.dumps(update.order), order),
        )
        self._conn.execute(
            "INSERT INTO daemon_state(key,value) VALUES ('folder_observation_order',?) "
            "ON CONFLICT(key) DO UPDATE SET value=MAX(CAST(value AS INTEGER),CAST(excluded.value AS INTEGER))",
            (str(order),),
        )
        return True

    def _apply_known_order_positions(self, supplied_order: tuple[int, ...], *, account_id: int, order: int) -> None:
        rules = read_folder_rules(self._conn)
        by_id = {
            rule.folder_id: rule for rule in rules if rule.namespace == "filter" or rule.folder_id in supplied_order
        }
        supplied = [folder_id for folder_id in supplied_order if folder_id in by_id]
        slots = sorted(rule.source_position for rule in by_id.values() if rule.source_position is not None)
        positions_known = len(slots) == len(by_id) == len(supplied) == len(supplied_order)
        for index, folder_id in enumerate(supplied):
            position = slots[index] if positions_known else None
            self._conn.execute(
                "UPDATE telegram_folder_rules SET source_position=?,rule_json=json_set(rule_json,'$.position',?),"
                "membership_order=? WHERE account_id=? AND namespace=? AND folder_id=?",
                (position, position, order, account_id, by_id[folder_id].namespace, folder_id),
            )

    def _apply_rule_delta(self, update: FolderRuleUpdate, *, account_id: int, order: int, now: int) -> bool:
        folder_id, rule = update.folder_id, update.rule
        if not _valid_rule_delta(folder_id, rule):
            return False
        assert folder_id is not None
        row = self._read_rule_state(account_id, folder_id)
        if row is not None and order <= row[1]:
            return True
        known_member, value = _retained_rule_value(row, rule)
        self._store_rule_delta(folder_id, value, account_id=account_id, order=order)
        membership_current, visible = _delta_membership(row, order)
        canonical = self.read_catalog_identity()
        self._replace_rule_members(
            folder_id, value if visible else None, canonical is not None and canonical[0] == account_id, now=now
        )
        membership_changed = membership_current and (not known_member or value is None)
        self._conn.execute(
            "UPDATE telegram_folder_projection_state SET account_id=?,catalog_revision=catalog_revision+1,"
            "membership_revision=membership_revision+?,coverage_status=CASE WHEN ? THEN 'unavailable' ELSE coverage_status END,"
            "rule_enumeration_complete=CASE WHEN ? THEN 0 ELSE rule_enumeration_complete END "
            "WHERE singleton=1",
            (account_id, int(membership_changed), int(membership_changed), int(membership_changed)),
        )
        self._conn.execute(
            "INSERT INTO daemon_state(key,value) VALUES ('folder_observation_order',?) "
            "ON CONFLICT(key) DO UPDATE SET value=MAX(CAST(value AS INTEGER),CAST(excluded.value AS INTEGER))",
            (str(order),),
        )
        _refine_supplied_receipt(self._conn, update, account_id, order, () if value is None else (value,))
        return True

    def _read_rule_state(
        self, account_id: int, folder_id: int
    ) -> tuple[int, int, int, int | None, int | None, int] | None:
        return cast(
            tuple[int, int, int, int | None, int | None, int] | None,
            self._conn.execute(
                "SELECT account_id,observation_order,deleted,source_position,catalog_member,membership_order FROM telegram_folder_rules "
                "WHERE account_id=? AND namespace='filter' AND folder_id=?",
                (account_id, folder_id),
            ).fetchone(),
        )

    def _store_rule_delta(self, folder_id: int, value: FolderRule | None, *, account_id: int, order: int) -> None:
        self._conn.execute(
            "INSERT INTO telegram_folder_rules(namespace,folder_id,title,rule_kind,source_position,rule_json,"
            "account_id,observation_order,deleted,catalog_member,membership_order) VALUES ('filter',?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(account_id,namespace,folder_id) DO UPDATE SET title=excluded.title,rule_kind=excluded.rule_kind,"
            "source_position=CASE WHEN membership_order>excluded.membership_order THEN source_position ELSE excluded.source_position END,"
            "rule_json=excluded.rule_json,account_id=excluded.account_id,"
            "observation_order=excluded.observation_order,deleted=excluded.deleted,"
            "catalog_member=CASE WHEN membership_order>excluded.membership_order THEN catalog_member ELSE excluded.catalog_member END,"
            "membership_order=MAX(membership_order,excluded.membership_order)",
            (
                folder_id,
                None if value is None else value.title,
                None if value is None else value.kind.value,
                None if value is None else value.source_position,
                None if value is None else _encode_rule(value),
                account_id,
                order,
                int(value is None),
                1 if value is not None else 0,
                order,
            ),
        )

    def _replace_rule_members(self, folder_id: int, value: FolderRule | None, has_directory: bool, *, now: int) -> None:
        self._conn.execute(
            "DELETE FROM telegram_folder_local_members WHERE namespace='filter' AND folder_id=?", (folder_id,)
        )
        if value is not None and has_directory:
            members = _evaluate_rules(
                self._conn, (value,), _canonical_facts(self._conn), _published_main_pins(self._conn), now=now
            )
            self._conn.executemany(
                "INSERT INTO telegram_folder_local_members(namespace,folder_id,dialog_id,state,pin_position) VALUES (?,?,?,?,?)",
                [(m.namespace, m.folder_id, m.dialog_id, m.state.value, m.pin_position) for m in members],
            )

    def _bind_projection_account(self, account_id: int) -> None:
        owner = self.read_catalog_version()[0]
        if owner == account_id:
            return
        if owner is None:
            self._conn.execute("DELETE FROM telegram_folder_local_members")
            self._conn.execute(
                "UPDATE telegram_folder_projection_state SET account_id=? WHERE singleton=1", (account_id,)
            )
            return
        self._conn.execute("DELETE FROM telegram_folder_local_members")
        self._conn.execute(
            "UPDATE telegram_folder_projection_state SET account_id=?,canonical_generation=NULL,accepted_rule_token=NULL,"
            "rule_observation_started_at=NULL,completed_at=NULL,canonical_observed_at=NULL,next_mute_expiry=NULL,"
            "filter_order_json=NULL,filter_order_observation_order=0,"
            "rule_enumeration_complete=0,"
            "coverage_status='unavailable',catalog_revision=catalog_revision+1,membership_revision=membership_revision+1 "
            "WHERE singleton=1",
            (account_id,),
        )

    def rules_are_fresh(self, *, now: int) -> bool:
        started_at = self.read_last_success_at()
        return (
            self._state("rule_enumeration_complete") == "1"
            and self._known_rules_complete()
            and started_at is not None
            and now < started_at + RULE_TTL_SECONDS
        )

    def _known_rules_complete(self) -> bool:
        row = cast(
            tuple[int],
            self._conn.execute(
                "SELECT NOT EXISTS(SELECT 1 FROM telegram_folder_rules r WHERE r.account_id=(SELECT account_id FROM "
                "telegram_folder_projection_state WHERE singleton=1) AND r.deleted=0 AND r.catalog_member=1 "
                "AND json_extract(r.rule_json,'$.complete') IS NOT 1)"
            ).fetchone(),
        )
        return bool(row[0])

    def invalidate(self) -> None:
        require_write_transaction(self._conn)
        self._conn.execute(
            "UPDATE telegram_folder_projection_state SET catalog_revision=catalog_revision + "
            "CASE WHEN coverage_status='complete' THEN 1 ELSE 0 END,membership_revision=membership_revision+1,"
            "coverage_status='unavailable',rule_enumeration_complete=0 WHERE singleton=1"
        )

    def next_mute_expiry(self) -> int | None:
        return _int_state(self._state("next_mute_expiry"))

    def project_observation(
        self,
        observation: FolderRuleObservation,
        *,
        completed_at: int,
        observation_order: int = 0,
        membership_revision: int | None = None,
        owner_account_id: int | None = None,
    ) -> int | None:
        """Publish a rule receipt only when a coherent canonical directory exists."""
        with write_savepoint(self._conn):
            if not observation.complete or not all(rule.complete for rule in observation.rules):
                self.invalidate()
                return None
            observation = replace(
                observation, rules=tuple(replace(rule, source_position=i) for i, rule in enumerate(observation.rules))
            )
            canonical = _canonical_receipt(self._conn)
            account_id = _snapshot_owner(owner_account_id, canonical)
            if account_id is None:
                return None
            self._bind_projection_account(account_id)
            if canonical is not None and canonical[0] != account_id:
                canonical = None
            self._conn.execute(
                "UPDATE telegram_folder_projection_state SET account_id=? WHERE singleton=1", (account_id,)
            )
            observation = self._merge_snapshot_order(observation, observation_order)
            complete = self._snapshot_complete(observation.rules, account_id, observation_order, membership_revision)
            self._merge_snapshot_rules(
                observation.rules, account_id=account_id, order=observation_order, complete_enumeration=complete
            )
            if canonical is None:
                self._conn.execute(
                    "UPDATE telegram_folder_projection_state SET accepted_rule_token=?,rule_observation_started_at=?,"
                    "rule_enumeration_complete=?,coverage_status='unavailable',last_attempt_at=?,last_outcome='success',"
                    "next_retry_at=NULL,consecutive_failures=0 WHERE singleton=1",
                    (observation.token, observation.started_at, int(complete), completed_at),
                )
                return None
            _, generation, canonical_observed_at = canonical
            observation = self._merged_observation(observation, account_id, observation_order)
            self._replace_projection(
                observation,
                account_id=account_id,
                generation=generation,
                canonical_observed_at=canonical_observed_at,
                completed_at=completed_at,
                complete=complete,
            )
            return generation

    def _merge_snapshot_order(self, observation: FolderRuleObservation, order: int) -> FolderRuleObservation:
        last_order = _int_state(self._state("filter_order_observation_order")) or 0
        if last_order > order:
            return replace(
                observation,
                rules=tuple(
                    replace(rule, source_position=None) if rule.namespace == "filter" else rule
                    for rule in observation.rules
                ),
            )
        self._conn.execute(
            "UPDATE telegram_folder_projection_state SET filter_order_json=?,filter_order_observation_order=? WHERE singleton=1",
            (json.dumps([rule.folder_id for rule in observation.rules]), order),
        )
        return observation

    def _snapshot_complete(
        self, rules: tuple[FolderRule, ...], account_id: int, order: int, membership_revision: int | None
    ) -> bool:
        return (membership_revision is None or membership_revision == self.read_membership_revision()) and (
            self._snapshot_membership_compatible(rules, account_id=account_id, order=order)
        )

    def _merged_observation(
        self, observation: FolderRuleObservation, account_id: int, order: int
    ) -> FolderRuleObservation:
        newer = cast(
            tuple[int],
            self._conn.execute(
                "SELECT EXISTS(SELECT 1 FROM telegram_folder_rules WHERE account_id=? AND observation_order>?)",
                (account_id, order),
            ).fetchone(),
        )[0]
        return (
            replace(observation, token=folder_observation_token(read_folder_rules(self._conn)))
            if newer
            else observation
        )

    def read_membership_revision(self) -> int:
        return _int_state(self._state("membership_revision")) or 0

    def _snapshot_membership_compatible(self, rules: tuple[FolderRule, ...], *, account_id: int, order: int) -> bool:
        keys = {(rule.namespace, rule.folder_id) for rule in rules}
        rows = cast(
            list[tuple[str, int, int | None]],
            self._conn.execute(
                "SELECT namespace,folder_id,catalog_member FROM telegram_folder_rules "
                "WHERE account_id=? AND membership_order>?",
                (account_id, order),
            ).fetchall(),
        )
        return all(((namespace, folder_id) in keys) == bool(member) for namespace, folder_id, member in rows)

    def _merge_snapshot_rules(
        self, rules: tuple[FolderRule, ...], *, account_id: int, order: int, complete_enumeration: bool
    ) -> None:
        keys = {(rule.namespace, rule.folder_id) for rule in rules}
        rows = cast(
            list[tuple[str, int, int]],
            self._conn.execute(
                "SELECT namespace,folder_id,membership_order FROM telegram_folder_rules WHERE account_id=?",
                (account_id,),
            ).fetchall(),
        )
        if complete_enumeration:
            for namespace, folder_id, membership_order in rows:
                if (namespace, folder_id) not in keys and membership_order <= order:
                    self._conn.execute(
                        "UPDATE telegram_folder_rules SET catalog_member=0,membership_order=? "
                        "WHERE account_id=? AND namespace=? AND folder_id=?",
                        (order, account_id, namespace, folder_id),
                    )
        for rule in rules:
            self._conn.execute(
                "INSERT INTO telegram_folder_rules(namespace,folder_id,title,rule_kind,source_position,rule_json,"
                "account_id,observation_order,deleted,catalog_member,membership_order) VALUES (?,?,?,?,?,?,?,?,0,1,?) "
                "ON CONFLICT(account_id,namespace,folder_id) DO UPDATE SET "
                "title=CASE WHEN observation_order<=excluded.observation_order THEN excluded.title ELSE title END,"
                "rule_kind=CASE WHEN observation_order<=excluded.observation_order THEN excluded.rule_kind ELSE rule_kind END,"
                "rule_json=CASE WHEN observation_order<=excluded.observation_order THEN "
                "json_set(excluded.rule_json,'$.position',CASE WHEN membership_order>excluded.membership_order "
                "THEN source_position ELSE excluded.source_position END) "
                "WHEN membership_order<=excluded.membership_order AND deleted=0 THEN json_set(rule_json,'$.position',excluded.source_position) "
                "ELSE rule_json END,"
                "deleted=CASE WHEN observation_order<=excluded.observation_order THEN 0 ELSE deleted END,"
                "observation_order=MAX(observation_order,excluded.observation_order),"
                "source_position=CASE WHEN membership_order<=excluded.membership_order THEN excluded.source_position ELSE source_position END,"
                "catalog_member=CASE WHEN membership_order<=excluded.membership_order THEN 1 ELSE catalog_member END,"
                "membership_order=MAX(membership_order,excluded.membership_order) "
                "WHERE account_id=excluded.account_id",
                (
                    rule.namespace,
                    rule.folder_id,
                    rule.title,
                    rule.kind.value,
                    rule.source_position,
                    _encode_rule(rule),
                    account_id,
                    order,
                    order,
                ),
            )

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
        observation = accepted
        if canonical is None:
            return None
        account_id, generation, canonical_observed_at = canonical
        if observation is None:
            if revision is None or revision[0] != account_id:
                return None
            self._project_retained_members(now=now, generation=generation, canonical_observed_at=canonical_observed_at)
            return generation
        self._replace_projection(
            observation,
            account_id=account_id,
            generation=generation,
            canonical_observed_at=canonical_observed_at,
            completed_at=now,
        )
        return generation

    def _project_retained_members(self, *, now: int, generation: int, canonical_observed_at: int) -> None:
        members = _evaluate_rules(
            self._conn,
            read_folder_rules(self._conn),
            _canonical_facts(self._conn),
            _published_main_pins(self._conn),
            now=now,
        )
        self._conn.execute("DELETE FROM telegram_folder_local_members")
        self._conn.executemany(
            "INSERT INTO telegram_folder_local_members(namespace,folder_id,dialog_id,state,pin_position) VALUES (?,?,?,?,?)",
            [(m.namespace, m.folder_id, m.dialog_id, m.state.value, m.pin_position) for m in members],
        )
        self._conn.execute(
            "UPDATE telegram_folder_projection_state SET canonical_generation=?,canonical_observed_at=? WHERE singleton=1",
            (generation, canonical_observed_at),
        )

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

    def _replace_projection(  # noqa: PLR0913 - publication facts have independent provenance
        self,
        observation: FolderRuleObservation,
        *,
        account_id: int,
        generation: int,
        canonical_observed_at: int,
        completed_at: int,
        complete: bool = True,
    ) -> None:
        require_write_transaction(self._conn)
        facts = _canonical_facts(self._conn)
        members = _evaluate_rules(
            self._conn, read_folder_rules(self._conn), facts, _published_main_pins(self._conn), now=completed_at
        )
        self._conn.execute("DELETE FROM telegram_folder_local_members")
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
            "rule_observation_started_at=?,completed_at=?,canonical_observed_at=?,next_mute_expiry=?,coverage_status=?,rule_enumeration_complete=?,"
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
                "complete" if complete else "unavailable",
                int(complete),
                completed_at,
            ),
        )

    def _accepted_observation(self) -> FolderRuleObservation | None:
        if self._state("rule_enumeration_complete") != "1":
            return None
        rows = cast(
            list[tuple[str]],
            self._conn.execute(
                "SELECT rule_json FROM telegram_folder_rules WHERE deleted=0 AND catalog_member=1 "
                "AND account_id=(SELECT account_id FROM telegram_folder_projection_state WHERE singleton=1) "
                "ORDER BY source_position"
            ).fetchall(),
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


def _snapshot_owner(owner_account_id: int | None, canonical: tuple[int, int, int] | None) -> int | None:
    return owner_account_id if owner_account_id is not None else None if canonical is None else canonical[0]


def _valid_rule_delta(folder_id: int | None, rule: FolderRule | None) -> bool:
    return folder_id is not None and (rule is None or (rule.complete and rule.folder_id == folder_id))


def _delta_membership(row: tuple[int, int, int, int | None, int | None, int] | None, order: int) -> tuple[bool, bool]:
    if row is None or order > row[5]:
        return True, True
    return False, row[4] != 0


def _retained_rule_value(
    row: tuple[int, int, int, int | None, int | None, int] | None, rule: FolderRule | None
) -> tuple[bool, FolderRule | None]:
    known_member = row is not None and row[2] == 0 and row[4] == 1
    position = row[3] if row is not None and known_member else None
    return known_member, None if rule is None else replace(rule, source_position=position)


def _refine_supplied_receipt(
    conn: sqlite3.Connection, update: FolderRuleUpdate, account_id: int, order: int, rules: tuple[FolderRule, ...]
) -> None:
    from .command_receipts import refine_from_observation

    if update.kind == "filter":
        refine_from_observation(
            conn, account_id, order, folder_definitions(rules), complete=False, affected_folder_id=update.folder_id
        )


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
        None if value["position"] is None else _as_int(value["position"]),
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
        conn.execute(
            "SELECT rule_json FROM telegram_folder_rules WHERE deleted=0 AND catalog_member IS NOT 0 "
            "AND account_id=(SELECT account_id FROM telegram_folder_projection_state WHERE singleton=1) "
            "ORDER BY source_position IS NULL,source_position,"
            "COALESCE((SELECT CAST(j.key AS INTEGER) FROM json_each((SELECT filter_order_json FROM "
            "telegram_folder_projection_state WHERE singleton=1)) j WHERE CAST(j.value AS INTEGER)=folder_id),2147483647),"
            "folder_id"
        ).fetchall(),
    )
    return tuple(_decode_rule(raw) for (raw,) in rows)


def encode_folder_definition(rule: FolderRule) -> str:
    # Command semantics do not include catalogue order; zero is a comparison sentinel only.
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
