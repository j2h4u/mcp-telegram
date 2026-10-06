"""Disk-backed identity interning for the v4 export wire format."""

import json
import sqlite3
import tempfile
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import TextIO, cast

from .chat_export_projection import compact_export_metadata

Payload = dict[str, object]
RecordFactory = Callable[[], Iterator[tuple[str, Payload]]]


class IdentityIndex:
    """Store snapshots on disk so export memory stays bounded by one record."""

    def __init__(self) -> None:
        self.frozen = False
        self._directory = tempfile.TemporaryDirectory(prefix="mcp-telegram-identities-")
        self.db = sqlite3.connect(Path(self._directory.name) / "identities.sqlite3")
        self.db.execute("CREATE TABLE identities (id INTEGER PRIMARY KEY, snapshot TEXT NOT NULL)")
        self.db.execute("CREATE INDEX identity_snapshot ON identities(snapshot)")

    def __enter__(self) -> IdentityIndex:
        return self

    def __exit__(self, *_: object) -> None:
        self.db.close()
        self._directory.cleanup()

    @staticmethod
    def _json(identity: Mapping[str, object]) -> str:
        return json.dumps(dict(identity), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)

    def intern(self, identity: Mapping[str, object]) -> int:
        snapshot = self._json(identity)
        row = cast(
            tuple[int] | None,
            self.db.execute("SELECT id FROM identities WHERE snapshot=? ORDER BY id LIMIT 1", (snapshot,)).fetchone(),
        )
        if row is None:
            if self.frozen:
                raise ValueError("Export identities changed between publication passes")
            self.db.execute("INSERT INTO identities(snapshot) VALUES (?)", (snapshot,))
            row = cast(tuple[int], self.db.execute("SELECT last_insert_rowid()").fetchone())
        return row[0] - 1

    def put_at(self, index: int, identity: Mapping[str, object]) -> None:
        if type(index) is not int or index < 0:
            raise ValueError("Invalid identity index")
        snapshot = self._json(identity)
        try:
            self.db.execute("INSERT INTO identities(id,snapshot) VALUES (?,?)", (index + 1, snapshot))
        except sqlite3.IntegrityError as exc:
            raise ValueError("Duplicate or out-of-order identity index") from exc

    def get(self, index: object) -> Payload:
        if type(index) is not int or index < 0:
            raise ValueError("Invalid identity reference")
        row = cast(
            tuple[str] | None, self.db.execute("SELECT snapshot FROM identities WHERE id=?", (index + 1,)).fetchone()
        )
        if row is None:
            raise ValueError("Identity reference is out of range")
        return cast(Payload, json.loads(row[0]))

    def records(self) -> Iterator[Payload]:
        rows = cast(Iterator[tuple[str]], self.db.execute("SELECT snapshot FROM identities ORDER BY id"))
        for (snapshot,) in rows:
            yield cast(Payload, json.loads(snapshot))

    def count(self) -> int:
        return cast(tuple[int], self.db.execute("SELECT count(*) FROM identities").fetchone())[0]


def _snapshot(record: Mapping[str, object], prefix: str) -> Payload:
    return {key[len(prefix) :]: value for key, value in record.items() if key.startswith(prefix)}


def _identity_ref(record: Payload, prefix: str, index: IdentityIndex) -> None:
    if prefix[:-1] in record:
        raise ValueError("Flat identity contains a reserved reference field")
    identity = _snapshot(record, prefix)
    for key in tuple(record):
        if key.startswith(prefix):
            record.pop(key)
    if identity:
        record[prefix[:-1]] = index.intern(identity)


def _expand_ref(record: Payload, field: str, prefix: str, index: IdentityIndex) -> None:
    if field not in record:
        return
    identity = index.get(record.pop(field))
    record.update({prefix + key: value for key, value in identity.items()})


def _related_refs(record: Payload, index: IdentityIndex) -> None:
    users = record.get("related_users")
    if isinstance(users, list):
        record["related_users"] = [index.intern(cast(Mapping[str, object], user)) for user in users]


def _related_expand(record: Payload, index: IdentityIndex) -> None:
    users = record.get("related_users")
    if isinstance(users, list):
        record["related_users"] = [index.get(ref) for ref in users]


def _peer_id(identity: Mapping[str, object]) -> Payload | None:  # noqa: PLR0911
    value, kind = identity.get("id"), identity.get("kind")
    if not isinstance(value, str):
        return None
    try:
        identifier = int(value)
    except ValueError:
        return None
    if str(identifier) != value:
        return None
    if kind == "user" and identifier > 0:
        return {"_": "PeerUser", "user_id": identifier}
    if kind == "chat" and identifier < 0:
        return {"_": "PeerChat", "chat_id": -identifier}
    if kind == "channel" and identifier <= -(10**12):
        return {"_": "PeerChannel", "channel_id": -identifier - 10**12}
    return None


def _recent_candidate(reactor: Mapping[str, object], identity: Mapping[str, object]) -> Payload:
    raw = reactor.get("raw")
    candidate: Payload = dict(cast(Mapping[str, object], raw)) if isinstance(raw, Mapping) else {}
    if reactor.get("date") is not None:
        candidate.setdefault("date", reactor["date"])
    if reactor.get("reaction") is not None:
        candidate.setdefault("reaction", reactor["reaction"])
    peer = _peer_id(identity)
    if peer is not None:
        candidate.setdefault("peer_id", peer)
    return candidate


def compact_reaction_events(record: Payload, index: IdentityIndex) -> None:
    reactions = record.get("reactions")
    reactors = record.get("reactors")
    if not isinstance(reactions, Mapping) or not isinstance(reactors, list):
        return
    aggregate = reactions.get("aggregate")
    if not isinstance(aggregate, Mapping) or not isinstance(aggregate.get("recent_reactions"), list):
        return
    candidates: dict[str, int] = {}
    for position, value in enumerate(reactors):
        if not isinstance(value, Mapping) or "actor" not in value:
            continue
        identity = index.get(value["actor"])
        candidate = _recent_candidate(value, identity)
        candidates.setdefault(
            json.dumps(candidate, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False), position
        )
    result = dict(aggregate)
    events = []
    for event in cast(list[object], aggregate["recent_reactions"]):
        if isinstance(event, Mapping) and "reactor" in event:
            raise ValueError("Flat reaction contains a reserved reference field")
        key = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        position = candidates.get(key) if isinstance(event, Mapping) else None
        events.append({"reactor": position} if position is not None else event)
    result["recent_reactions"] = events
    record["reactions"] = {**dict(reactions), "aggregate": result}


def expand_reaction_events(record: Payload) -> None:
    reactions = record.get("reactions")
    reactors = record.get("reactors")
    if not isinstance(reactions, Mapping) or not isinstance(reactors, list):
        return
    aggregate = reactions.get("aggregate")
    if not isinstance(aggregate, Mapping) or not isinstance(aggregate.get("recent_reactions"), list):
        return
    result = dict(aggregate)
    events = []
    for event in cast(list[object], aggregate["recent_reactions"]):
        if isinstance(event, Mapping) and set(event) == {"reactor"}:
            reference = event["reactor"]
            if type(reference) is not int or not 0 <= reference < len(reactors):
                raise ValueError("Recent reaction reference is out of range")
            reactor = reactors[reference]
            if not isinstance(reactor, Mapping):
                raise ValueError("Recent reaction reference is invalid")
            events.append(_recent_candidate(reactor, _snapshot(reactor, "actor_")))
        else:
            events.append(event)
    result["recent_reactions"] = events
    record["reactions"] = {**dict(reactions), "aggregate": result}


def pack_record(kind: str, flat: Mapping[str, object], index: IdentityIndex) -> Payload:
    result = dict(flat)
    if kind in {"messages.item", "admin_events.item"}:
        _identity_ref(result, "author_" if kind == "messages.item" else "actor_", index)
        _related_refs(result, index)
    if kind == "messages.item" and isinstance(result.get("reactors"), list):
        packed = []
        for value in cast(list[object], result["reactors"]):
            reactor = dict(cast(Mapping[str, object], value))
            _identity_ref(reactor, "actor_", index)
            packed.append(reactor)
        result["reactors"] = packed
        compact_reaction_events(result, index)
    return result


def expand_record(kind: str, wire: Mapping[str, object], index: IdentityIndex) -> Payload:
    result = dict(wire)
    if kind in {"messages.item", "admin_events.item"}:
        _expand_ref(
            result,
            "author" if kind == "messages.item" else "actor",
            "author_" if kind == "messages.item" else "actor_",
            index,
        )
        _related_expand(result, index)
    if kind == "messages.item" and isinstance(result.get("reactors"), list):
        expanded = []
        for value in cast(list[object], result["reactors"]):
            reactor = dict(cast(Mapping[str, object], value))
            _expand_ref(reactor, "actor", "actor_", index)
            expanded.append(reactor)
        result["reactors"] = expanded
        expand_reaction_events(result)
    return result


def write_export(record_factory: RecordFactory, stream: TextIO) -> None:  # noqa: PLR0912, PLR0915
    """Write v4 JSON using a deterministic two-pass, disk-backed identity index."""
    from .chat_export_schema import CURRENT_FORMAT_VERSION, validate_record

    group: Payload | None = None
    metadata: Payload | None = None
    footer: Payload | None = None
    with IdentityIndex() as index:
        order = {"group": 0, "metadata": 1, "admin_events.item": 2, "messages.item": 3, "export": 4}
        previous = -1
        counts = {"messages": 0, "admin_events": 0, "reactors": 0}
        for kind, record in record_factory():
            position = order.get(kind, -1)
            if (
                position < previous
                or position == -1
                or (position == previous and kind in {"group", "metadata", "export"})
            ):
                raise ValueError("Invalid export record order")
            previous = position
            if kind == "group":
                group = dict(record)
            elif kind == "metadata":
                metadata = dict(record)
            elif kind in {"messages.item", "admin_events.item"}:
                counts["messages" if kind == "messages.item" else "admin_events"] += 1
                if kind == "messages.item":
                    counts["reactors"] += len(cast(list[object], record.get("reactors", [])))
                packed = pack_record(kind, record, index)
                validate_record(kind, packed, CURRENT_FORMAT_VERSION)
            elif kind == "export":
                footer = dict(record)
        if group is None or metadata is None or footer is None:
            raise ValueError("Export record stream is missing a header or footer")
        if footer != counts:
            raise ValueError("Export counts do not match its records")
        index.frozen = True
        metadata = compact_export_metadata(group, metadata)
        validate_record("group", group, CURRENT_FORMAT_VERSION)
        validate_record("metadata", metadata, CURRENT_FORMAT_VERSION)
        validate_record("export", footer, CURRENT_FORMAT_VERSION)
        stream.write('{"format_version":4,"group":')
        _dump(stream, group)
        stream.write(',"metadata":')
        _dump(stream, metadata)
        stream.write(',"identities":[')
        for position, identity in enumerate(index.records()):
            if position:
                stream.write(",")
            validate_record("identities.item", identity, CURRENT_FORMAT_VERSION)
            _dump(stream, identity)
        stream.write('],"admin_events":[')
        state = "admin_events.item"
        first = True
        for kind, record in record_factory():
            if kind in {"group", "metadata", "export"}:
                continue
            if kind == "messages.item":
                if state == "messages.item":
                    pass
                else:
                    stream.write('],"messages":[')
                    state = "messages.item"
                    first = True
            elif kind != "admin_events.item":
                continue
            elif state == "messages.item":
                raise ValueError("Admin events follow messages")
            if not first:
                stream.write(",")
            packed = pack_record(kind, record, index)
            validate_record(kind, packed, CURRENT_FORMAT_VERSION)
            _dump(stream, packed)
            first = False
        if state == "admin_events.item":
            stream.write('],"messages":[')
        stream.write('],"export":')
        _dump(stream, footer)
        stream.write("}")


def _dump(stream: TextIO, value: object) -> None:
    stream.write(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
