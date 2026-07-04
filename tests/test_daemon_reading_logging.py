"""Logging regressions for daemon reading fallbacks."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator

import pytest

from mcp_telegram.daemon_reading import (
    DaemonReadingDeps,
    DaemonReadingService,
    _ListMessagesTelegramRequest,
)
from mcp_telegram.pagination import HistoryDirection


class _EntityMissingClient:
    async def get_messages(self, entity: object, ids: list[int]) -> object:
        _ = (entity, ids)
        return None

    def iter_messages(self, dialog_id: int, **kwargs: object) -> AsyncIterator[object]:
        _ = (dialog_id, kwargs)

        async def _gen() -> AsyncIterator[object]:
            raise ValueError("Could not find the input entity for PeerUser(user_id=123)")
            yield object()

        return _gen()


class _TestLogger:
    def __init__(self) -> None:
        self.warning_calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
        self.exception_calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def debug(self, msg: str, *args: object, **kwargs: object) -> None:
        _ = (msg, args, kwargs)
        return

    def info(self, msg: str, *args: object, **kwargs: object) -> None:
        _ = (msg, args, kwargs)
        return

    def warning(self, msg: str, *args: object, **kwargs: object) -> None:
        self.warning_calls.append((msg, args, kwargs))

    def exception(self, msg: str, *args: object, **kwargs: object) -> None:
        self.exception_calls.append((msg, args, kwargs))


@pytest.mark.asyncio
async def test_list_messages_telegram_entity_miss_logs_structured_warning_without_traceback() -> None:
    logger = _TestLogger()
    service = DaemonReadingService(
        DaemonReadingDeps(
            conn=sqlite3.connect(":memory:"),
            client=_EntityMissingClient(),
            self_id=1,
            resolve_dialog_id=lambda _dialog_id, _dialog: asyncio.sleep(0, result=0),
            fetch_fragment_context=lambda _dialog_id, _message_id: asyncio.sleep(0, result=False),
            logger=logger,
            rid=lambda: " request_id=test-rid",
        )
    )

    result = await service._list_messages_from_telegram(
        _ListMessagesTelegramRequest(
            dialog_id=123,
            limit=10,
            direction="newest",
            direction_enum=HistoryDirection.NEWEST,
            anchor_msg_id=None,
            sender_id=None,
            topic_id=None,
            unread_after_id=None,
        )
    )

    assert result["ok"] is False
    assert result["error"] == "telegram_error"
    assert result["detail"] == {
        "error_type": "ValueError",
        "error_message": "Could not find the input entity for PeerUser(user_id=123)",
        "retryable": False,
    }
    assert len(logger.warning_calls) == 1
    _, _, kwargs = logger.warning_calls[0]
    assert kwargs.get("exc_info") is None
    assert logger.exception_calls == []
