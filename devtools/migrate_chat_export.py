"""Deterministically migrate a complete export to the current schema.

Run: uv run python -m devtools.migrate_chat_export SOURCE.json DESTINATION.json
Both schema validation and migration run offline. Existing files are never overwritten.
"""

import argparse
import json
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import TextIO

from mcp_telegram.chat_export_checkpoint import Payload, base_records, census, fingerprint
from mcp_telegram.chat_export_identity import write_export
from mcp_telegram.chat_export_projection import omit_empty_fields
from mcp_telegram.chat_export_schema import (
    CURRENT_FORMAT_VERSION,
    IDENTITY_FORMAT_VERSION,
    migrate_record,
    read_export_version,
)


def _write_migration(path: Path, stream: TextIO, version: int) -> None:
    def records() -> Iterator[tuple[str, Payload]]:
        for kind, source in base_records(path):
            if kind == "identities.item":
                continue
            yield kind, migrate_record(version, kind, source, internal=version >= IDENTITY_FORMAT_VERSION)

    write_export(records, stream)


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
        if migrated != {**info, "group": omit_empty_fields(info["group"]), "format_version": CURRENT_FORMAT_VERSION}:
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
