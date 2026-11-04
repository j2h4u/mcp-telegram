"""FullUser folder labels use the captured account, never another account's catalog."""

from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from mcp_telegram.auth_scope import AUTH_SCOPE_VERSION, TelegramAuthScope
from mcp_telegram.daemon_entity_info import DaemonEntityInfoService
from mcp_telegram.entity_profile.contracts import (
    ProjectionOutcome,
    ProjectionStatus,
    TargetKind,
    UserProfileObservation,
)
from mcp_telegram.models import DialogType
from tests.folder_fixtures import seed_folder_snapshot
from tests.test_entity_profile_full_user_pair import _PairClient, _prepare


class FolderProfilePort(_PairClient):
    async def fetch_user_profile(
        self, user_id: int, target_kind: TargetKind, *, owner_account_id: int | None = None
    ) -> UserProfileObservation:
        del owner_account_id
        full = ProjectionOutcome(ProjectionStatus.USABLE, {"folder_id": 7}, None, None)
        personal = ProjectionOutcome(ProjectionStatus.ABSENT, None, None, None)
        return UserProfileObservation(user_id, target_kind, full, personal)


@pytest.mark.parametrize(
    ("account_id", "projection_account", "expected"),
    [(42, 42, "Account A folder"), (43, 42, None), (None, 42, None), (43, 43, None)],
)
@pytest.mark.parametrize("paired", [False, True])
async def test_full_user_folder_name_uses_only_original_account(
    tmp_path: Path, account_id: int | None, projection_account: int, expected: str | None, paired: bool
) -> None:
    conn, raw_service = _prepare(tmp_path / "folder-name.db", migrated=True)
    service = cast(DaemonEntityInfoService, raw_service)
    seed_folder_snapshot(conn, [(7, "Account A folder")], [])
    conn.execute("UPDATE telegram_folder_projection_state SET account_id=? WHERE singleton=1", (projection_account,))
    conn.commit()
    scope = TelegramAuthScope(AUTH_SCOPE_VERSION, account_id, 2, 99) if account_id is not None else None
    service._deps = replace(
        service._deps,
        user_profile_port=FolderProfilePort(),
        full_user_auth_scope=(lambda: scope) if scope is not None else None,
    )
    try:
        if paired:
            cursor = service._profiles.next_due_refresh(now=100)
            assert cursor is not None
            commit, _ = await service._acquire_full_user_pair(cursor)
        else:
            commit = await service._acquire_user_full_profile(42, DialogType.USER, scope=scope)
        assert commit.detail_patch["folder_name"] == expected
        assert commit.detail_patch["folder_id"] == 7
    finally:
        await service.shutdown()
        conn.close()
