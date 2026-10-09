"""Crash-safe local records and bounded-memory reading of versioned exports."""

import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import closing
from pathlib import Path
from typing import cast

import ijson  # type: ignore[import-untyped]
from ijson.common import ObjectBuilder  # type: ignore[import-untyped]

from .chat_export_identity import IdentityIndex, expand_record
from .chat_export_schema import (
    IDENTITY_FORMAT_VERSION,
    INTERNAL_FORMAT_VERSION,
    SPARSE_FORMAT_VERSION,
    migrate_record,
    read_export_version,
    validate_record,
    validate_version,
)

type Payload = dict[str, object]
CheckpointError = sqlite3.Error
ORDER = "newest_to_oldest within each peer; primary then migrated predecessors"


def _top_shape(prefix: str, event: str, value: object) -> bool:
    if prefix == "format_version":
        if event != "number":
            raise ValueError("Unsupported base export format")
        validate_version(value)
        return True
    if prefix in {"messages", "admin_events", "identities"}:
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
    version = read_export_version(path)
    expected_keys = ["format_version", "group", "metadata"]
    if version >= IDENTITY_FORMAT_VERSION:
        expected_keys.append("identities")
    if version < SPARSE_FORMAT_VERSION or "admin_events" in keys:
        expected_keys.append("admin_events")
    expected_keys.extend(["messages", "export"])
    if shapes != set(expected_keys):
        raise ValueError("Malformed base export field types")
    if keys != expected_keys:
        raise ValueError("Malformed base export structure")


def _record_start(prefix: str, event: str) -> bool:
    if prefix in {"messages.item", "admin_events.item", "identities.item"} and event != "start_map":
        raise ValueError("Base export records must be objects")
    return (
        prefix in {"group", "metadata", "export", "messages.item", "admin_events.item", "identities.item"}
        and event == "start_map"
    )


def _expand_base_record(
    kind: str,
    record: Payload,
    file_version: int,
    identity_index: IdentityIndex | None,
    *,
    expand_identities: bool,
) -> Payload:
    if file_version >= IDENTITY_FORMAT_VERSION and kind in {"messages.item", "admin_events.item"}:
        if identity_index is None:
            raise ValueError("Missing export identity directory")
        expanded = expand_record(kind, record, identity_index)
        if file_version == SPARSE_FORMAT_VERSION and kind == "messages.item":
            expanded.setdefault("reactors", [])
        validate_record(kind, expanded, internal=True)
        return expanded if expand_identities else record
    return record


def base_records(path: Path, *, expand_identities: bool = True) -> Iterator[tuple[str, Payload]]:
    """Read one projected record at a time, including the small header/footer."""
    file_version = read_export_version(path)
    identity_index = IdentityIndex() if file_version >= IDENTITY_FORMAT_VERSION else None
    identity_position = 0
    builder = None
    active = ""
    depth = 0
    depth_changes = {"start_map": 1, "start_array": 1, "end_map": -1, "end_array": -1}
    try:
        for prefix, event, value in _validated_events(path):
            if builder is None:
                if not _record_start(prefix, event):
                    continue
                builder = ObjectBuilder()
                active = prefix
            builder.event(event, value)
            depth += depth_changes.get(event, 0)
            if depth == 0:
                record = cast(Payload, builder.value)
                validate_record(active, record, file_version)
                if active == "identities.item":
                    if identity_index is None:
                        raise ValueError("Unexpected export identity directory")
                    identity_index.put_at(identity_position, record)
                    identity_position += 1
                    if not expand_identities:
                        yield active, record
                else:
                    yield (
                        active,
                        _expand_base_record(
                            active, record, file_version, identity_index, expand_identities=expand_identities
                        ),
                    )
                builder = None
    finally:
        if identity_index is not None:
            identity_index.__exit__(None, None, None)


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
        self.format_version = 1

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

    def _validate_message_identity(self, record: Payload, peer: int, identifier: int) -> None:
        if (
            peer not in self.counts
            or (self.format_version < INTERNAL_FORMAT_VERSION and record.get("message_key") != f"{peer}:{identifier}")
            or ("message_key" in record and record["message_key"] != f"{peer}:{identifier}")
        ):
            raise ValueError("Malformed base message identity")

    def message(self, record: Payload) -> None:
        peer = canonical_id(record.get("dialog_id"), negative=True)
        identifier = canonical_id(record.get("message_id"))
        self._validate_message_identity(record, peer, identifier)
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
    state.format_version = read_export_version(path)
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
    return {**state.finish(), "format_version": read_export_version(path)}


def fingerprint(path: Path) -> list[int] | None:
    if path.is_symlink():
        raise FileExistsError(f"Export path is a symbolic link: {path}")
    if not path.exists():
        return None
    stat = path.stat()
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]


def read_checkpoint_options(path: Path) -> Payload:
    """Recover an existing database before inspecting its export identity."""
    with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True)) as db:
        row = cast(tuple[str] | None, db.execute("SELECT value FROM state WHERE key='options'").fetchone())
        value = cast(object, json.loads(row[0])) if row else {}
        if not isinstance(value, dict):
            raise ValueError("Malformed export checkpoint identity")
        return cast(Payload, value)


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
        saved_version = self.state("format_version")
        version = validate_version(1 if saved_version is None else saved_version)
        prefix = "messages.item" if kind == "message" else "admin_events.item"
        rows = cast(
            Iterator[tuple[str]],
            self.db.execute("SELECT payload FROM records WHERE kind=? AND peer=? ORDER BY id DESC", (kind, peer)),
        )
        for row in rows:
            record = cast(Payload, json.loads(row[0]))
            if version == INTERNAL_FORMAT_VERSION:
                validate_record(prefix, record, internal=True)
                yield row[0]
                continue
            yield json.dumps(
                migrate_record(version, prefix, record, internal=version >= IDENTITY_FORMAT_VERSION),
                ensure_ascii=False,
                allow_nan=False,
            )

    def upgrade_records(self) -> None:
        """Atomically migrate all saved rows before adding records in the current format."""
        version = validate_version(1 if self.state("format_version") is None else self.state("format_version"))
        if version == INTERNAL_FORMAT_VERSION:
            return
        with self.db:
            rows = cast(
                Iterator[tuple[str, int, int, str]], self.db.execute("SELECT kind,peer,id,payload FROM records")
            )
            for kind, peer, identifier, payload in rows:
                prefix = "messages.item" if kind == "message" else "admin_events.item"
                upgraded = migrate_record(
                    version,
                    prefix,
                    cast(Payload, json.loads(payload)),
                    internal=version >= IDENTITY_FORMAT_VERSION,
                )
                self.db.execute(
                    "UPDATE records SET payload=? WHERE kind=? AND peer=? AND id=?",
                    (json.dumps(upgraded, ensure_ascii=False, allow_nan=False), kind, peer, identifier),
                )
            self.set_state("format_version", INTERNAL_FORMAT_VERSION)

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

    def import_base(self, base: Path, info: Payload) -> None:
        if self.state("base_imported"):
            return
        self.upgrade_records()
        boundaries = cast(Payload, info["boundaries"])
        with self.db:
            for kind, record in base_records(base):
                if kind == "messages.item":
                    peer, identifier = int(cast(str, record["dialog_id"])), int(cast(str, record["message_id"]))
                    if identifier > cast(int, boundaries[str(peer)]):
                        continue
                    record_kind = "message"
                elif kind == "admin_events.item":
                    peer, identifier = int(cast(str, record["dialog_id"])), int(cast(str, record["event_id"]))
                    record_kind = "admin"
                else:
                    continue
                source_version = validate_version(info["format_version"])
                record = migrate_record(
                    source_version, kind, record, internal=source_version >= IDENTITY_FORMAT_VERSION
                )
                self.db.execute(
                    "INSERT OR IGNORE INTO records VALUES (?,?,?,?)",
                    (record_kind, peer, identifier, json.dumps(record, ensure_ascii=False, allow_nan=False)),
                )
            if fingerprint(base) != self.state("base_fingerprint"):
                raise ValueError("Incremental base changed while importing")
            self.set_state("base_imported", True)

    def mark_many(self, values: Mapping[str, object]) -> None:
        with self.db:
            for key, value in values.items():
                self.set_state(key, value)

    def is_empty(self) -> bool:
        return (
            self.db.execute("SELECT 1 FROM state LIMIT 1").fetchone() is None
            and self.db.execute("SELECT 1 FROM records LIMIT 1").fetchone() is None
        )

    def close(self) -> None:
        self.db.close()
