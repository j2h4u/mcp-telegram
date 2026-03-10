from __future__ import annotations

import pytest
from pathlib import Path


@pytest.fixture()
def tmp_db_path(tmp_path: Path) -> Path:
    """Return a path to a temporary SQLite file (not yet created)."""
    return tmp_path / "entity_cache.db"


@pytest.fixture()
def sample_entities() -> dict[int, str]:
    """Return {entity_id: display_name} mapping for resolver tests."""
    return {
        101: "Иван Петров",
        102: "Ivan's Team",
        103: "Анна Иванова",
        104: "Work Group",
    }
