"""Crash-safe local export records and bounded-memory reading of v1 exports."""

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import ijson  # type: ignore[import-untyped]
from ijson.common import ObjectBuilder  # type: ignore[import-untyped]

type Payload = dict[str, object]
ORDER = "newest_to_oldest within each peer; primary then migrated predecessors"


def _top_shape(prefix: str, event: str, value: object) -> bool:
    if prefix == "format_version":
        if event != "number" or type(value) is not int or value != 1:
            raise ValueError("Unsupported base export format")
        return True
    if prefix in {"messages", "admin_events"}:
        if event not in {"start_array", "end_array"}:
            raise ValueError("Base record collections must be arrays")
        return True
    return prefix in {"group", "metadata", "export"} and event in {"start_map", "end_map"}


def _validated_events(path: Path) -> Iterator[tuple[str, str, object]]:
    keys: list[str] = []
    shapes: set[str] = set()
    with path.open("rb") as stream:
        events = cast(Iterator[tuple[str, str, object]], ijson.parse(stream, use_float=True))  # pyright: ignore[reportAny]
        for prefix, event, value in events:
            if prefix == "" and event == "map_key":
                keys.append(cast(str, value))
            if _top_shape(prefix, event, value):
                shapes.add(prefix)
            yield prefix, event, value
    if shapes != {"format_version", "messages", "admin_events", "group", "metadata", "export"}:
        raise ValueError("Malformed base export field types")
    if keys != ["format_version", "group", "metadata", "admin_events", "messages", "export"]:
        raise ValueError("Malformed base export structure")


def _record_start(prefix: str, event: str) -> bool:
    if prefix in {"messages.item", "admin_events.item"} and event != "start_map":
        raise ValueError("Base export records must be objects")
    return prefix in {"group", "metadata", "export", "messages.item", "admin_events.item"} and event == "start_map"


def base_records(path: Path) -> Iterator[tuple[str, Payload]]:
    """Read one projected record at a time, including the small header/footer."""
    builder = None
    active = ""
    depth = 0
    depth_changes = {"start_map": 1, "start_array": 1, "end_map": -1, "end_array": -1}
    for prefix, event, value in _validated_events(path):
        if builder is None:
            if not _record_start(prefix, event):
                continue
            builder = ObjectBuilder()
            active = prefix
        builder.event(event, value)
        depth += depth_changes.get(event, 0)
        if depth == 0:
            yield active, cast(Payload, builder.value)
            builder = None


def canonical_id(value: object, *, negative: bool = False) -> int:
    if not isinstance(value, str):
        raise ValueError("Base export identifiers must be canonical strings")
    number = int(value)
    if str(number) != value or (number >= 0 if negative else number <= 0):
        raise ValueError("Invalid base export identifier")
    return number


class _Census:
    """Only per-peer counts/cursors are retained while validating a large base."""

    def __init__(self, refresh: int) -> None:
        self.refresh = refresh
        self.peers: list[int] = []
        self.counts: dict[int, int] = {}
        self.boundaries: dict[int, int] = {}
        self.last: dict[int, int] = {}
        self.messages = self.admins = self.reactors = 0
        self.admin_max = self.admin_last = 0
        self.group: Payload = {}
        self.footer: Payload = {}
        self.peer_index = 0

    def metadata(self, record: Payload) -> None:
        if record.get("order") != ORDER or not isinstance(record.get("peers"), list):
            raise ValueError("Malformed base peer order")
        peer_records = cast(list[object], record["peers"])
        if any(not isinstance(peer, dict) for peer in peer_records):
            raise ValueError("Malformed base peer directory")
        self.peers = [canonical_id(peer.get("dialog_id"), negative=True) for peer in cast(list[Payload], peer_records)]
        if (
            not self.peers
            or len(set(self.peers)) != len(self.peers)
            or str(self.peers[0]) != self.group.get("dialog_id")
        ):
            raise ValueError("Malformed base group/peer directory")
        self.counts = dict.fromkeys(self.peers, 0)
        self.boundaries = dict.fromkeys(self.peers, 0)

    def admin(self, record: Payload) -> None:
        identifier = canonical_id(record.get("event_id"))
        if record.get("dialog_id") != self.group.get("dialog_id") or (
            self.admin_last and identifier >= self.admin_last
        ):
            raise ValueError("Malformed base admin event order")
        self.admin_max = self.admin_max or identifier
        self.admin_last = identifier
        self.admins += 1

    def message(self, record: Payload) -> None:
        peer = canonical_id(record.get("dialog_id"), negative=True)
        identifier = canonical_id(record.get("message_id"))
        if peer not in self.counts or record.get("message_key") != f"{peer}:{identifier}":
            raise ValueError("Malformed base message identity")
        while self.peer_index < len(self.peers) and self.peers[self.peer_index] != peer:
            self.peer_index += 1
        if self.peer_index == len(self.peers) or (peer in self.last and identifier >= self.last[peer]):
            raise ValueError("Malformed base history order")
        self.last[peer] = identifier
        self.counts[peer] += 1
        if self.counts[peer] == self.refresh + 1:
            self.boundaries[peer] = identifier
        self.messages += 1
        if not isinstance(record.get("reactors"), list):
            raise ValueError("Malformed base reactors")
        self.reactors += len(cast(list[object], record["reactors"]))

    def finish(self) -> Payload:
        if any(type(value) is not int or value < 0 for value in self.footer.values()):
            raise ValueError("Malformed base export footer counts")
        if self.footer != {"messages": self.messages, "admin_events": self.admins, "reactors": self.reactors}:
            raise ValueError("Base export footer counts do not match records")
        return {
            "group": self.group,
            "peers": self.peers,
            "boundaries": {str(k): v for k, v in self.boundaries.items()},
            "admin_max": self.admin_max,
        }


def census(path: Path, refresh: int) -> Payload:
    state = _Census(refresh)
    for kind, record in base_records(path):
        if kind == "group":
            state.group = record
            canonical_id(record.get("dialog_id"), negative=True)
        elif kind == "metadata":
            state.metadata(record)
        elif kind == "admin_events.item":
            state.admin(record)
        elif kind == "messages.item":
            state.message(record)
        else:
            state.footer = record
    return state.finish()


class Checkpoint:
    """SQLite commits each fully enriched record together with its resume cursor."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.db = sqlite3.connect(path)
        tables = {
            row[0]
            for row in cast(Iterator[tuple[str]], self.db.execute("SELECT name FROM sqlite_master WHERE type='table'"))
        }
        if tables - {"state", "records"}:
            self.db.close()
            raise ValueError("Existing sidecar is not an export checkpoint")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS records (kind TEXT, peer INTEGER, id INTEGER, payload TEXT NOT NULL, "
            "PRIMARY KEY(kind,peer,id))"
        )
        self.db.commit()

    def state(self, key: str) -> object:
        row = cast(tuple[str] | None, self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone())
        return json.loads(row[0]) if row else None

    def set_state(self, key: str, value: object) -> None:
        self.db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, json.dumps(value, ensure_ascii=False)))

    def save(self, kind: str, peer: int, identifier: int, payload: str, cursor_key: str) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO records VALUES (?,?,?,?)", (kind, peer, identifier, payload))
            self.set_state(cursor_key, identifier)

    def mark(self, key: str, value: object) -> None:
        with self.db:
            self.set_state(key, value)

    def records(self, kind: str, peer: int) -> Iterator[str]:
        rows = cast(
            Iterator[tuple[str]],
            self.db.execute("SELECT payload FROM records WHERE kind=? AND peer=? ORDER BY id DESC", (kind, peer)),
        )
        for row in rows:
            yield row[0]

    def summary(self) -> Payload:
        messages = admins = reactors = 0
        for kind, payload in cast(Iterator[tuple[str, str]], self.db.execute("SELECT kind,payload FROM records")):
            if kind == "message":
                messages += 1
                record = cast(Payload, json.loads(payload))
                reactors += len(cast(list[object], record["reactors"]))
            else:
                admins += 1
        return {"messages": messages, "admin_events": admins, "reactors": reactors}
