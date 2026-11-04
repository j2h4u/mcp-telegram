"""Publish identities supplied by one authorized history observation."""

import sqlite3
from typing import Protocol

from ..dialog_identity import publish_dialog_identity, publish_supplied_identities
from ..entity_profile.repository import bound_profile_account
from ..image_authorization import ScopeToken
from ..messages.sqlite_bundle import ReceivedMessageScopeChangedError
from ..sync_transactions import write_transaction
from .contracts import ForwardGapPage, FullHistoryPage


class _ScopeClaim(Protocol):
    def claim_scope(self, token: ScopeToken) -> bool: ...


def publish_page_identities(  # noqa: PLR0913 - transaction observation context
    conn: sqlite3.Connection,
    port: _ScopeClaim,
    scope: ScopeToken,
    page: FullHistoryPage | ForwardGapPage,
    *,
    dialog_id: int,
    identity_revision: int,
    observation_order: int,
) -> bool:
    """Use the original scope at transaction entry and exit; reject a revoked page."""
    try:
        with write_transaction(conn):
            _require_scope(conn, port, scope)
            if page.identity is not None:
                publish_dialog_identity(conn, dialog_id, page.identity, identity_revision)
            publish_supplied_identities(conn, page.referenced_identities, observation_order=observation_order)
            _require_scope(conn, port, scope)
    except ReceivedMessageScopeChangedError:
        return False
    return True


def _require_scope(conn: sqlite3.Connection, port: _ScopeClaim, scope: ScopeToken) -> None:
    if bound_profile_account(conn) != scope.scope.account_id or not port.claim_scope(scope):
        raise ReceivedMessageScopeChangedError
