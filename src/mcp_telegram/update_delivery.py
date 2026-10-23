"""Single pending delivery and authoritative completed checkpoint in sync.db."""

import sqlite3
from dataclasses import dataclass
from typing import cast

from .observation_order import allocate_observation_order
from .reactions.contracts import ReactionAggregateSource
from .reactions.persistence import allocate_observation_boundary
from .sqlite_writer import SQLiteWriter
from .sync_transactions import write_transaction

DELIVERY_FORMAT = 1


@dataclass(frozen=True, slots=True)
class DeliveryState:
    safe_snapshot: str
    pending: bytes | None
    orders: tuple[int, ...]
    reaction_sequences: tuple[int, ...]


class UpdateDeliveryRepository:
    """All writes settle through the daemon's existing SQLite owner."""

    def __init__(self, writer: SQLiteWriter) -> None:
        self._writer = writer

    async def bind(self, account_id: int, baseline: str) -> DeliveryState:
        if isinstance(account_id, bool) or account_id <= 0:
            raise ValueError("update delivery requires a validated positive account")

        def bind(conn: sqlite3.Connection) -> DeliveryState:
            with write_transaction(conn):
                conn.execute(
                    "INSERT OR IGNORE INTO update_delivery(singleton,account_id,format,safe_snapshot) VALUES(1,?,?,?)",
                    (account_id, DELIVERY_FORMAT, baseline),
                )
                row = cast(
                    tuple[int, int, str, bytes | None] | None,
                    conn.execute(
                        "SELECT account_id,format,safe_snapshot,pending FROM update_delivery WHERE singleton=1"
                    ).fetchone(),
                )
                if row is None or row[0] != account_id:
                    raise RuntimeError("update delivery account mismatch")
                if row[1] != DELIVERY_FORMAT:
                    raise RuntimeError("unsupported update delivery format")
                tokens = cast(
                    list[tuple[int, int, int]],
                    conn.execute(
                        "SELECT position,observation_order,reaction_sequence FROM update_delivery_tokens ORDER BY position"
                    ).fetchall(),
                )
                orders, sequences = _validate_tokens(row[3], tokens)
                return DeliveryState(row[2], row[3], orders, sequences)

        return await self._writer.run(bind)

    async def admit(self, payload: bytes, count: int) -> tuple[tuple[int, int], ...]:
        def admit(conn: sqlite3.Connection) -> tuple[tuple[int, int], ...]:
            with write_transaction(conn):
                changed = conn.execute(
                    "UPDATE update_delivery SET pending=? WHERE singleton=1 AND pending IS NULL", (payload,)
                ).rowcount
                if changed != 1:
                    raise RuntimeError("an update delivery is already pending")
                tokens = tuple(
                    (
                        allocate_observation_order(conn),
                        allocate_observation_boundary(conn, ReactionAggregateSource.RAW_UPDATE).sequence,
                    )
                    for _ in range(count)
                )
                conn.executemany(
                    "INSERT INTO update_delivery_tokens(position,observation_order,reaction_sequence) VALUES(?,?,?)",
                    ((i, order, sequence) for i, (order, sequence) in enumerate(tokens)),
                )
                return tokens

        return await self._writer.run(admit)

    async def complete(self, snapshot: str) -> None:
        def complete(conn: sqlite3.Connection) -> None:
            with write_transaction(conn):
                if (
                    conn.execute(
                        "UPDATE update_delivery SET safe_snapshot=?,pending=NULL WHERE singleton=1 AND pending IS NOT NULL",
                        (snapshot,),
                    ).rowcount
                    != 1
                ):
                    raise RuntimeError("update delivery completion has no pending obligation")
                conn.execute("DELETE FROM update_delivery_tokens")

        await self._writer.run(complete)


def _validate_tokens(
    pending: bytes | None, tokens: list[tuple[int, int, int]]
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    if pending is None and tokens:
        raise RuntimeError("completed Telegram delivery has pending tokens")
    if any(
        position != index or order <= 0 or sequence <= 0 for index, (position, order, sequence) in enumerate(tokens)
    ):
        raise RuntimeError("retained Telegram update tokens are invalid")
    orders, sequences = tuple(t[1] for t in tokens), tuple(t[2] for t in tokens)
    unique_sorted = tuple(sorted(set(orders))), tuple(sorted(set(sequences)))
    if unique_sorted != (orders, sequences):
        raise RuntimeError("retained Telegram update orders are not monotonic")
    return orders, sequences
