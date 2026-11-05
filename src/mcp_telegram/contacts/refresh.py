"""Bounded contact acquisition and account-fenced canonical publication."""

import sqlite3
import time
from collections.abc import Callable
from typing import Protocol, cast

from telethon.tl import functions, types

from ..entity_profile.contracts import (
    ObservationBoundary,
    ProfileFieldClass,
    ProfileFieldCoverage,
    ProfileFieldObservation,
    ProfileFieldState,
    SuppliedProfileUser,
)
from ..entity_profile.repository import EntityProfileRepository
from ..entity_store import PartialEntityIdentity, apply_partial_entity_identity
from ..image_authorization import ScopeToken
from ..observation_order import allocate_observation_order
from ..sqlite_writer import SQLiteWriter
from ..sync_transactions import write_transaction
from .contracts import ContactCatalogueRefreshResult
from .intake import catalogue_marker
from .receipts import refine_unknown_for_observation


class ContactCatalogueClient(Protocol):
    async def __call__(self, request: object) -> object: ...


class ContactCatalogueRefresher:
    def __init__(
        self,
        client: ContactCatalogueClient,
        reader: sqlite3.Connection,
        writer: SQLiteWriter,
        *,
        capture_scope: Callable[[], ScopeToken | None],
        claim_scope: Callable[[ScopeToken], bool],
    ) -> None:
        self._client, self._reader, self._writer = client, reader, writer
        self._capture_scope, self._claim_scope = capture_scope, claim_scope

    async def refresh(self, *, expected_revision: int | None = None) -> ContactCatalogueRefreshResult:
        token = self._capture_scope()
        if token is None:
            return ContactCatalogueRefreshResult(False)
        account_id = token.scope.account_id
        marker = catalogue_marker()
        initial = cast(
            tuple[int] | None,
            self._reader.execute(
                "SELECT requested_revision FROM contact_catalogue_state WHERE account_id=?", (account_id,)
            ).fetchone(),
        )
        if initial is None or (expected_revision is not None and initial[0] != expected_revision):
            return ContactCatalogueRefreshResult(False)
        order = await self._writer.run(allocate_observation_order)
        started_at = int(time.time())
        response = await self._client(functions.contacts.GetContactsRequest(hash=0))
        completed_at = int(time.time())
        normalized = _complete_contacts(response)
        if normalized is None:
            return ContactCatalogueRefreshResult(False)
        contacts, users = normalized

        def publish(conn: sqlite3.Connection) -> ContactCatalogueRefreshResult:
            with write_transaction(conn):
                row = cast(
                    tuple[int, int | None] | None,
                    conn.execute(
                        "SELECT requested_revision,observation_order FROM contact_catalogue_state WHERE account_id=?",
                        (account_id,),
                    ).fetchone(),
                )
                if row is None or row[0] != initial[0] or (row[1] is not None and row[1] >= order):
                    return ContactCatalogueRefreshResult(False)
                if catalogue_marker() != marker or not self._claim_scope(token) or catalogue_marker() != marker:
                    return ContactCatalogueRefreshResult(False)
                profiles = EntityProfileRepository(conn, section_ttl_seconds=300)
                observation = ProfileFieldObservation(account_id, order, completed_at, "rpc")
                _publish_catalogue(
                    conn,
                    profiles,
                    contacts,
                    users,
                    observation,
                    started_at,
                    lambda: self._claim_scope(token) and catalogue_marker() == marker,
                )
                conn.execute(
                    "UPDATE contact_catalogue_state SET accepted_revision=?,observed_at=?,observation_order=? WHERE account_id=?",
                    (row[0], completed_at, order, account_id),
                )
                return ContactCatalogueRefreshResult(True, order)

        result = await self._writer.run(publish)
        if catalogue_marker() != marker or not self._claim_scope(token):
            return ContactCatalogueRefreshResult(False)
        return result


def _complete_contacts(response: object) -> tuple[dict[int, object], dict[int, types.User]] | None:
    if not isinstance(response, types.contacts.Contacts):
        return None
    users = {user.id: user for user in response.users if isinstance(user, types.User) and not user.min}
    contacts: dict[int, object] = {
        contact.user_id: contact.mutual for contact in response.contacts if isinstance(contact, types.Contact)
    }
    if len(contacts) != len(response.contacts) or not contacts.keys() <= users.keys() or min(contacts, default=1) <= 0:
        return None
    return contacts, users


def _publish_catalogue(  # noqa: PLR0913, PLR0917 - one transaction and captured acquisition facts
    conn: sqlite3.Connection,
    profiles: EntityProfileRepository,
    contacts: dict[int, object],
    users: dict[int, types.User],
    observation: ProfileFieldObservation,
    started_at: int,
    guard: Callable[[], bool],
) -> None:
    previous = cast(
        list[tuple[int]],
        conn.execute(
            "SELECT entity_id FROM entity_profile_semantic_snapshots WHERE owner_account_id=? "
            "AND json_type(fields_json, '$.\"user.contact\"')='true'",
            (observation.owner_account_id,),
        ).fetchall(),
    )
    for entity_id, mutual in contacts.items():
        _publish_contact(profiles, conn, (users[entity_id], mutual), observation, started_at)
        refine_unknown_for_observation(conn, observation.owner_account_id, entity_id, observation.order, guard)
    for (entity_id,) in previous:
        if entity_id not in contacts:
            _publish_membership(profiles, entity_id, False, observation, started_at)


def _publish_membership(
    profiles: EntityProfileRepository,
    entity_id: int,
    member: bool,
    observation: ProfileFieldObservation,
    started_at: int,
) -> None:
    coverage = ProfileFieldCoverage(ProfileFieldClass.PRIVATE_FACT, ProfileFieldState.PRESENT, True)
    profiles.apply_supplied_user(
        SuppliedProfileUser(entity_id, {"user.contact": member}, {"user.contact": coverage}, {}, False, False),
        observation=observation,
        boundary=ObservationBoundary(started_at, observation.observed_at),
    )


def _publish_contact(
    profiles: EntityProfileRepository,
    conn: sqlite3.Connection,
    contact: tuple[types.User, object],
    observation: ProfileFieldObservation,
    started_at: int,
) -> None:
    user, mutual = contact
    apply_partial_entity_identity(
        conn, user.id, PartialEntityIdentity(observed_at=observation.observed_at, observed_type="user")
    )
    fields: dict[str, object] = {
        "user.contact": True,
        "owner.saved_contact_name": {"first_name": user.first_name or "", "last_name": user.last_name or ""},
        "owner.saved_contact_phone_access": bool(user.phone),
    }
    if isinstance(mutual, bool):
        fields["user.mutual_contact"] = mutual
    if user.phone:
        fields["user.phone"] = user.phone
    coverage = {
        path: ProfileFieldCoverage(ProfileFieldClass.PRIVATE_FACT, ProfileFieldState.PRESENT, True) for path in fields
    }
    profiles.apply_supplied_user(
        SuppliedProfileUser(user.id, fields, coverage, {}, False, False),
        observation=observation,
        boundary=ObservationBoundary(started_at, observation.observed_at),
    )
