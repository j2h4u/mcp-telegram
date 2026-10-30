"""Physical-claim and durable allowance fences for a selected image attempt."""

import sqlite3
from dataclasses import dataclass
from typing import cast

from ..entity_profile.repository import bound_profile_account
from ..image_authorization import ScopeToken, SourceLease
from ..sqlite_writer import SQLiteWriter
from ..sync_transactions import write_transaction
from .contracts import ImageScope
from .repository import ImageSource, current_source


@dataclass(frozen=True, slots=True)
class SourceToken:
    scope: ImageScope
    captured: ScopeToken
    lease: SourceLease

    def is_current(self) -> bool:
        return self.lease.is_current() and self.scope.is_current(self.captured)


@dataclass(frozen=True, slots=True)
class SourceFence:
    writer: SQLiteWriter
    scope: ImageScope
    captured: ScopeToken
    dialog_id: int
    message_id: int
    source: ImageSource
    attempt: int

    async def prepare(self) -> SourceToken:
        def preflight(conn: sqlite3.Connection) -> SourceToken:
            with write_transaction(conn):
                account = self.captured.scope.account_id
                if (
                    bound_profile_account(conn) != account
                    or current_source(conn, self.dialog_id, self.message_id) != self.source
                    or conn.execute(
                        "SELECT 1 FROM message_image_jobs WHERE account_id=? AND dialog_id=? AND message_id=? "
                        "AND attempt_generation=? AND source_generation=? AND state IN ('queued','running')",
                        (account, self.dialog_id, self.message_id, self.attempt, self.source.generation),
                    ).fetchone()
                    is None
                    or not self.scope.is_current(self.captured)
                ):
                    raise RuntimeError("image source changed")
                lease = self.scope.sources.register(account, self.dialog_id, self.message_id, self.attempt)
                return SourceToken(self.scope, self.captured, lease)

        return await self.writer.run(preflight)


@dataclass(frozen=True, slots=True)
class DurableBudget:
    writer: SQLiteWriter
    account_id: int
    dialog_id: int
    message_id: int
    attempt: int

    async def reserve(self, *, setup: bool) -> None:
        def spend(conn: sqlite3.Connection) -> bool:
            with write_transaction(conn):
                result = conn.execute(
                    "UPDATE message_image_jobs SET remaining=remaining-1, "
                    "setup_remaining=setup_remaining-? WHERE account_id=? AND dialog_id=? AND message_id=? "
                    "AND attempt_generation=? AND state IN ('queued','running') AND remaining>0 "
                    "AND (?=0 OR setup_remaining>0)",
                    (int(setup), self.account_id, self.dialog_id, self.message_id, self.attempt, int(setup)),
                )
                return result.rowcount == 1

        if not await self.writer.run(spend):
            raise RuntimeError("image acquisition allowance exhausted")

    async def reserve_redirect(self, dc_id: int) -> None:
        if type(dc_id) is not int or dc_id <= 0:
            raise ValueError("invalid image redirect")

        def reserve(conn: sqlite3.Connection) -> bool:
            with write_transaction(conn):
                return (
                    conn.execute(
                        "UPDATE message_image_jobs SET redirect_dc=? WHERE account_id=? AND dialog_id=? AND message_id=? "
                        "AND attempt_generation=? AND state IN ('queued','running') AND redirect_dc IS NULL",
                        (dc_id, self.account_id, self.dialog_id, self.message_id, self.attempt),
                    ).rowcount
                    == 1
                )

        if not await self.writer.run(reserve):
            raise RuntimeError("image redirect allowance exhausted")

    async def current_redirect(self) -> int | None:
        def read(conn: sqlite3.Connection) -> tuple[int | None] | None:
            return cast(
                tuple[int | None] | None,
                conn.execute(
                    "SELECT redirect_dc FROM message_image_jobs WHERE account_id=? AND dialog_id=? AND message_id=? "
                    "AND attempt_generation=? AND state IN ('queued','running')",
                    (self.account_id, self.dialog_id, self.message_id, self.attempt),
                ).fetchone(),
            )

        row = await self.writer.run(read)
        if row is None:
            raise RuntimeError("image attempt unavailable")
        return row[0]
