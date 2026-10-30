"""State directory helpers shared by daemon, CLI, and auth code."""

from __future__ import annotations

import fcntl
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

PRIVATE_STATE_DIR_MODE = 0o700
_LEASE_ISSUER = object()


class TelegramSessionLease:
    """Opaque process-local capability for the currently held session flock."""

    __slots__ = ("_active", "_descriptor", "_device", "_inode", "_owner_id", "_pid")

    def __init__(self, issuer: object, descriptor: int) -> None:
        if issuer is not _LEASE_ISSUER:
            raise RuntimeError("Telegram session lease can only be issued by the lock owner")
        info = os.fstat(descriptor)
        self._active = True
        self._descriptor = descriptor
        self._device = info.st_dev
        self._inode = info.st_ino
        self._owner_id = uuid.uuid4().hex
        self._pid = os.getpid()

    def is_held(self) -> bool:
        if not self._active or os.getpid() != self._pid:
            return False
        try:
            info = os.fstat(self._descriptor)
        except OSError:
            return False
        return (info.st_dev, info.st_ino) == (self._device, self._inode)

    def _revoke(self) -> None:
        self._active = False

    def identity(self) -> dict[str, int]:
        if not self.is_held():
            raise RuntimeError("Telegram session lease is unavailable")
        return {"version": 1, "device": self._device, "inode": self._inode}

    def owner_id(self) -> str:
        if not self.is_held():
            raise RuntimeError("Telegram session lease is unavailable")
        return self._owner_id

    def _pin(self) -> TelegramSessionLeasePin:
        if not self.is_held():
            raise RuntimeError("Telegram session lease is no longer held")
        descriptor = os.dup(self._descriptor)
        try:
            os.set_inheritable(descriptor, False)
        except OSError:
            os.close(descriptor)
            raise
        return TelegramSessionLeasePin(_LEASE_ISSUER, descriptor, self._device, self._inode, self._owner_id)


class TelegramSessionLeasePin:
    """Gate-owned duplicate of the actual flock's open file description."""

    __slots__ = ("_descriptor", "_device", "_inode", "_owner_id", "_pid")

    def __init__(self, issuer: object, descriptor: int, device: int, inode: int, owner_id: str) -> None:
        if issuer is not _LEASE_ISSUER:
            raise RuntimeError("Telegram session pin can only be issued by the lock owner")
        self._descriptor: int | None = descriptor
        self._device = device
        self._inode = inode
        self._owner_id = owner_id
        self._pid = os.getpid()

    def identity(self) -> dict[str, int]:
        if self._descriptor is None or os.getpid() != self._pid:
            raise RuntimeError("Telegram session pin is unavailable")
        return {"version": 1, "device": self._device, "inode": self._inode}

    def _release(self) -> None:
        descriptor = self._descriptor
        if descriptor is not None:
            self._descriptor = None
            os.close(descriptor)

    def owner_id(self) -> str:
        if self._descriptor is None or os.getpid() != self._pid:
            raise RuntimeError("Telegram session pin is unavailable")
        return self._owner_id


@contextmanager
def telegram_session_lease(state_dir: Path) -> Iterator[TelegramSessionLease]:
    """One daemon or stopped maintenance owner may use the Session at a time."""
    descriptor = os.open(state_dir / "telegram-session.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Telegram session is already owned by another process") from None
        proof = TelegramSessionLease(_LEASE_ISSUER, descriptor)
        try:
            yield proof
        finally:
            proof._revoke()
    finally:
        os.close(descriptor)


@dataclass(frozen=True, slots=True)
class StatePaths:
    """Concrete runtime paths derived once from an explicit configured state dir."""

    state_dir: Path
    sync_db_path: Path
    feedback_db_path: Path
    daemon_socket_path: Path

    @classmethod
    def from_state_dir(cls, state_dir: Path) -> StatePaths:
        """Build the canonical state-path bundle without loading configuration."""
        return cls(
            state_dir=state_dir,
            sync_db_path=state_dir / "sync.db",
            feedback_db_path=state_dir / "feedback.db",
            daemon_socket_path=state_dir / "daemon.sock",
        )


def ensure_private_state_dir(path: Path, *, mode: int = PRIVATE_STATE_DIR_MODE) -> Path:
    """Create or tighten a state directory before storing Telegram state."""
    path.mkdir(parents=True, exist_ok=True, mode=mode)
    path.chmod(mode)
    if not path.is_dir():
        raise NotADirectoryError(path)
    return path
