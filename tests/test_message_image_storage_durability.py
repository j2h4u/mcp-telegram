"""Image directory entries are durable before an association may be written."""

import os
from pathlib import Path

import pytest

from mcp_telegram.message_images import store


def test_first_image_fsyncs_ancestor_entries_before_return(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = os.fsync
    synced: list[Path] = []

    def record(descriptor: int) -> None:
        synced.append(Path(f"/proc/self/fd/{descriptor}").readlink())
        original(descriptor)

    monkeypatch.setattr(store.os, "fsync", record)
    root = tmp_path / "images"
    artifact = store.publish_image(root, 42, b"\xff\xd8\xfftest\xff\xd9")
    assert synced[0] == tmp_path
    assert synced[1] == root
    assert synced[2].parent == root / "42" and synced[2].suffix == ".partial"
    assert synced[3] == root / "42"
    assert (root / "42" / artifact.filename).is_file()


def test_ancestor_fsync_failure_never_publishes_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(_descriptor: int) -> None:
        raise OSError("injected sync failure")

    monkeypatch.setattr(store.os, "fsync", fail)
    root = tmp_path / "images"
    with pytest.raises(OSError, match="injected sync failure"):
        store.publish_image(root, 42, b"\xff\xd8\xfftest\xff\xd9")
    assert not list(root.rglob("*.jpg"))
    assert not list(root.rglob("*.partial"))
