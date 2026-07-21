"""State directory helpers shared by daemon, CLI, and auth code."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

PRIVATE_STATE_DIR_MODE = 0o700


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
