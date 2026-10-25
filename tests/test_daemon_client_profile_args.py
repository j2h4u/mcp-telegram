"""Profile convenience calls preserve the daemon request contract."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_telegram.daemon_client import DaemonConnection, DaemonNotRunningError


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("section", "cursor", "limit", "optional_payload"),
    [
        (None, None, 50, {}),
        ("history", "next-page", 25, {"section": "history", "cursor": "next-page", "limit": 25}),
        ("avatar_history", None, 50, {"section": "avatar_history"}),
        (None, "opaque-cursor", 50, {"cursor": "opaque-cursor"}),
        (None, None, 100, {"limit": 100}),
        ("", "", 50, {"section": "", "cursor": ""}),
    ],
)
async def test_profile_request_forwards_only_explicit_optional_values(
    section: str | None, cursor: str | None, limit: int, optional_payload: dict[str, object]
) -> None:
    response: dict[str, object] = {"ok": True, "data": {"retained": True}}
    request = AsyncMock(return_value=response)
    connection = DaemonConnection(MagicMock(), MagicMock())
    with patch.object(DaemonConnection, "request", request):
        result = await connection.get_entity_info(entity_id=42, section=section, cursor=cursor, limit=limit)
    request.assert_awaited_once_with({"method": "get_entity_info", "entity_id": 42, **optional_payload})
    assert result is response


@pytest.mark.asyncio
async def test_profile_default_call_preserves_existing_request_shape() -> None:
    request = AsyncMock(return_value={"ok": True})
    connection = DaemonConnection(MagicMock(), MagicMock())
    with patch.object(DaemonConnection, "request", request):
        await connection.get_entity_info(entity_id=42)
    request.assert_awaited_once_with({"method": "get_entity_info", "entity_id": 42})


@pytest.mark.asyncio
async def test_profile_call_preserves_transport_error() -> None:
    error = DaemonNotRunningError("connection closed", kind="connection_broken")
    request = AsyncMock(side_effect=error)
    connection = DaemonConnection(MagicMock(), MagicMock())
    with patch.object(DaemonConnection, "request", request), pytest.raises(DaemonNotRunningError) as caught:
        await connection.get_entity_info(entity_id=42, section="history", cursor="next-page", limit=25)
    assert caught.value is error
    assert caught.value.kind == "connection_broken"
    request.assert_awaited_once_with(
        {"method": "get_entity_info", "entity_id": 42, "section": "history", "cursor": "next-page", "limit": 25}
    )
