"""Create a matched offline SQLite backup before a schema migration."""

import argparse
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import cast

from mcp_telegram.state import telegram_session_lease


def readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def snapshot(source: Path, target: Path) -> None:
    with closing(readonly(source)) as src, closing(sqlite3.connect(target)) as dst:
        src.backup(dst)
        if dst.execute("PRAGMA journal_mode=DELETE").fetchone() != ("delete",):
            raise ValueError(f"Could not make backup standalone: {target.name}")


def validate(path: Path, tables: set[str], version_query: str, expected: int) -> None:
    with closing(readonly(path)) as db:
        rows = cast("list[tuple[str]]", db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall())
        found = {row[0] for row in rows}
        if not tables <= found or db.execute(version_query).fetchone() != (expected,):
            raise ValueError(f"Invalid schema or version in {path.name}")
    for suffix in ("-wal", "-shm", "-journal"):
        if Path(f"{path}{suffix}").exists():
            raise ValueError(f"Unexpected backup sidecar: {path.name}{suffix}")


def backup(state: Path, output: Path, schema: int, references: dict[str, str]) -> None:
    output.mkdir(mode=0o700)
    with telegram_session_lease(state):
        names = ("sync.db", "mcp_telegram_session.session")
        source_sidecars = {
            name: [suffix for suffix in ("-wal", "-shm", "-journal") if (state / f"{name}{suffix}").exists()]
            for name in names
        }
        for name in names:
            snapshot(state / name, output / name)
            (output / name).chmod(0o600)
        validate(
            output / names[0],
            {"schema_version", "synced_dialogs", "messages"},
            "SELECT MAX(version) FROM schema_version",
            schema,
        )
        validate(
            output / names[1],
            {"version", "sessions", "entities", "sent_files", "update_state"},
            "SELECT version FROM version",
            8,
        )
        manifest = {"schema": schema, **references, "files": list(names), "source_sidecars": source_sidecars}
        descriptor = os.open(output / "manifest.tmp", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(manifest, stream)
            stream.write("\n")
        (output / "manifest.tmp").rename(output / "manifest.json")


class Arguments(argparse.Namespace):
    state: Path
    output: Path
    schema: int
    release: str
    image: str
    config: str


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("schema", type=int)
    parser.add_argument("release")
    parser.add_argument("image")
    parser.add_argument("config")
    args = parser.parse_args(namespace=Arguments())
    backup(args.state, args.output, args.schema, {"release": args.release, "image": args.image, "config": args.config})


if __name__ == "__main__":
    main()
