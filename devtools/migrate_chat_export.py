"""Deterministically migrate a complete export to the current schema.

Run: uv run python -m devtools.migrate_chat_export SOURCE.json DESTINATION.json
Both schema validation and migration run offline. Existing files are never overwritten.
"""

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import TextIO

from mcp_telegram.chat_export_checkpoint import base_records, census, fingerprint
from mcp_telegram.chat_export_schema import CURRENT_FORMAT_VERSION, migrate_record, read_export_version


def _write_migration(path: Path, stream: TextIO, version: int) -> None:
    stream.write(f'{{"format_version":{CURRENT_FORMAT_VERSION}')
    admins = messages = 0
    messages_started = False
    for kind, source in base_records(path):
        record = migrate_record(version, kind, source)
        if kind in {"group", "metadata"}:
            stream.write(f',"{kind}":')
            json.dump(record, stream, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            if kind == "metadata":
                stream.write(',"admin_events":[')
        elif kind == "admin_events.item":
            if admins:
                stream.write(",")
            json.dump(record, stream, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            admins += 1
        elif kind == "messages.item":
            if not messages_started:
                stream.write('],"messages":[')
                messages_started = True
            if messages:
                stream.write(",")
            json.dump(record, stream, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            messages += 1
        elif kind == "export":
            if not messages_started:
                stream.write('],"messages":[')
            stream.write('],"export":')
            json.dump(record, stream, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    stream.write("}\n")


def migrate_export(path: Path, output: Path) -> dict[str, int]:
    """Publish a new validated file; retain the source on every failure."""
    if path.resolve() == output.resolve():
        raise ValueError("Migration output must differ from its source")
    if output.exists() or output.is_symlink():
        raise FileExistsError("Migration destination already exists")
    before = fingerprint(path)
    info = census(path, 100)
    version = read_export_version(path)
    descriptor, name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            _write_migration(path, stream, version)
            stream.flush()
            os.fsync(stream.fileno())
        migrated = census(temporary, 100)
        if migrated != {**info, "format_version": CURRENT_FORMAT_VERSION}:
            raise ValueError("Migration changed incremental history boundaries")
        if fingerprint(path) != before:
            raise ValueError("Migration source changed while reading")
        os.link(temporary, output)
        directory = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return {"from_version": version, "to_version": CURRENT_FORMAT_VERSION}
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(json.dumps(migrate_export(args.source, args.output)))
