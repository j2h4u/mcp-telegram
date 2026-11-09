import fcntl
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from devtools import migration_backup


@pytest.fixture
def state(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    state.mkdir()
    with closing(sqlite3.connect(state / "sync.db")) as db:
        db.executescript(
            "CREATE TABLE schema_version(version); INSERT INTO schema_version VALUES(102); CREATE TABLE synced_dialogs(id); CREATE TABLE messages(id);"
        )
        db.commit()
    with closing(sqlite3.connect(state / "mcp_telegram_session.session")) as db:
        db.executescript(
            "CREATE TABLE version(version); INSERT INTO version VALUES(8); CREATE TABLE sessions(id); CREATE TABLE entities(id); CREATE TABLE sent_files(id); CREATE TABLE update_state(id);"
        )
        db.commit()
    return state


def run_backup(state: Path, out: Path) -> None:
    migration_backup.backup(
        state, out, 102, {"release": "release", "image": "sha256:old", "config": "config-reference"}
    )


def test_pair_and_manifest(state: Path, tmp_path: Path) -> None:
    out = tmp_path / "backup"
    run_backup(state, out)
    assert json.loads((out / "manifest.json").read_text())["schema"] == 102
    assert (out / "manifest.json").stat().st_mode & 0o777 == 0o600
    with closing(migration_backup.readonly(out / "sync.db")) as db:
        assert db.execute("SELECT version FROM schema_version").fetchone() == (102,)
    with closing(migration_backup.readonly(out / "mcp_telegram_session.session")) as db:
        assert db.execute("SELECT version FROM version").fetchone() == (8,)


@pytest.mark.parametrize("failure", ["second", "session", "messages", "lease"])
def test_failure_never_publishes_manifest(
    state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    paths = [state / "sync.db", state / "mcp_telegram_session.session"]
    if failure == "session":
        with closing(sqlite3.connect(paths[1])) as db:
            db.execute("DROP TABLE sessions")
            db.commit()
    if failure == "messages":
        with closing(sqlite3.connect(paths[0])) as db:
            db.execute("DROP TABLE messages")
            db.commit()
    before = [p.read_bytes() for p in paths]
    lock = state / "telegram-session.lock"
    lock.touch()
    inode = lock.stat().st_ino
    if failure == "second":
        real = migration_backup.snapshot

        def fail_second(source: Path, target: Path) -> None:
            if source == paths[1]:
                raise OSError("injected failure")
            real(source, target)

        monkeypatch.setattr(migration_backup, "snapshot", fail_second)
    with lock.open("r+") as held:
        if failure == "lease":
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises((RuntimeError, ValueError, OSError)):
            run_backup(state, tmp_path / "backup")
    assert not (tmp_path / "backup" / "manifest.json").exists()
    assert [p.read_bytes() for p in paths] == before
    assert lock.stat().st_ino == inode


def test_backup_includes_committed_wal_data_without_modifying_source(state: Path, tmp_path: Path) -> None:
    source = state / "sync.db"
    with closing(sqlite3.connect(source)) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO messages VALUES(42)")
        writer.commit()
        wal = state / "sync.db-wal"
        assert wal.stat().st_size > 0
        original_db, original_wal = source.read_bytes(), wal.read_bytes()
        out = tmp_path / "backup"
        run_backup(state, out)
        assert source.read_bytes() == original_db
        assert wal.read_bytes() == original_wal
        with closing(migration_backup.readonly(out / "sync.db")) as copy:
            assert copy.execute("SELECT id FROM messages").fetchone() == (42,)
