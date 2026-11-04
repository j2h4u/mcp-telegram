"""Pin updates invalidate catalog enumeration only for unknown membership."""

from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest
from telethon.tl.types import PeerChannel, UpdatePinnedForumTopic, UpdatePinnedForumTopics

from mcp_telegram.auth_scope import TelegramAuthScope
from mcp_telegram.delivered_update import DeliveredUpdate, delivered_update_context
from mcp_telegram.image_authorization import ScopeCell
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema
from mcp_telegram.topics.catalog import TopicCatalogRepository
from tests.test_topic_event_completeness import _manager, _seed_topics

DIALOG = -1000000000123


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["single", "list", "empty"])
@pytest.mark.parametrize("pending", [0, 1])
async def test_known_pins_preserve_catalog_progress_and_request_no_acquisition(
    tmp_path: Path, kind: str, pending: int
) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    db = _open_sync_db(path)
    try:
        client = MagicMock()
        manager = _manager(client, db)
        _seed_topics(db, 1, 2)
        db.execute(
            "INSERT INTO topic_catalog_state(dialog_id,revision,pending,reason) VALUES (?,7,?,?)",
            (DIALOG, pending, "recovery_gap" if pending else None),
        )
        db.commit()
        before = cast(
            tuple[object, ...] | None,
            db.execute("SELECT * FROM topic_catalog_state WHERE dialog_id=?", (DIALOG,)).fetchone(),
        )
        update = (
            UpdatePinnedForumTopic(peer=PeerChannel(123), topic_id=1, pinned=True)
            if kind == "single"
            else UpdatePinnedForumTopics(peer=PeerChannel(123), order=[1, 2] if kind == "list" else [])
        )
        await manager.on_raw_forum_topic_pinned(update)
        assert db.execute("SELECT * FROM topic_catalog_state WHERE dialog_id=?", (DIALOG,)).fetchone() == before
        assert TopicCatalogRepository(db).next_pending() == ((DIALOG, 0) if pending else None)
        pins = db.execute("SELECT topic_id,pinned FROM topic_metadata ORDER BY topic_id").fetchall()
        assert pins == (
            [(1, 1), (2, 1)] if kind == "list" else [(1, 0 if kind == "empty" else 1), (2, 0 if kind == "empty" else 1)]
        )
        cast(MagicMock, client.get_entity).assert_not_called()
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["single", "list"])
@pytest.mark.parametrize("membership", ["missing", "deleted", "absent"])
async def test_unknown_pin_target_requests_catalog(tmp_path: Path, kind: str, membership: str) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    db = _open_sync_db(path)
    try:
        client = MagicMock()
        manager = _manager(client, db)
        manager.set_self_id(42)
        scope = TelegramAuthScope(1, 42, 2, 99)
        cell = ScopeCell()
        assert cell.publish(scope)
        cast(MagicMock, client.retained_auth_scope).return_value = scope
        cast(MagicMock, client.claim_image_scope).side_effect = cell.claim
        if membership != "missing":
            _seed_topics(db, 9)
            column = "is_deleted" if membership == "deleted" else "catalog_present"
            db.execute(
                f"UPDATE topic_metadata SET {column}=?,membership_order=50 WHERE dialog_id=? AND topic_id=9",
                (int(membership == "deleted"), DIALOG),
            )
        db.execute("INSERT INTO topic_catalog_state(dialog_id,revision,pending) VALUES (?,7,0)", (DIALOG,))
        db.commit()
        update = (
            UpdatePinnedForumTopic(peer=PeerChannel(123), topic_id=9, pinned=True)
            if kind == "single"
            else UpdatePinnedForumTopics(peer=PeerChannel(123), order=[9])
        )
        with delivered_update_context(DeliveredUpdate(10, 10, 1, auth_scope=scope, scope_token=cell.capture())):
            await manager.on_raw_forum_topic_pinned(update)
        assert db.execute(
            "SELECT revision,pending FROM topic_catalog_state WHERE dialog_id=?", (DIALOG,)
        ).fetchone() == (8, 1)
        assert TopicCatalogRepository(db).next_pending() == (DIALOG, 0)
        if membership == "deleted":
            assert db.execute(
                "SELECT is_deleted FROM topic_metadata WHERE dialog_id=? AND topic_id=9", (DIALOG,)
            ).fetchone() == (1,)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_older_known_pin_does_not_replace_newer_fact(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    db = _open_sync_db(path)
    try:
        client = MagicMock()
        manager = _manager(client, db)
        manager.set_self_id(42)
        scope = TelegramAuthScope(1, 42, 2, 99)
        cell = ScopeCell()
        assert cell.publish(scope)
        cast(MagicMock, client.retained_auth_scope).return_value = scope
        cast(MagicMock, client.claim_image_scope).side_effect = cell.claim
        _seed_topics(db, 1)
        db.execute(
            "UPDATE topic_metadata SET pinned=1,pinned_order=50,known_fields=known_fields|8 WHERE dialog_id=? AND topic_id=1",
            (DIALOG,),
        )
        db.execute("INSERT INTO topic_catalog_state(dialog_id,revision,pending) VALUES (?,7,0)", (DIALOG,))
        db.commit()
        delivery = DeliveredUpdate(10, 10, 1, auth_scope=scope, scope_token=cell.capture())
        with delivered_update_context(delivery):
            await manager.on_raw_forum_topic_pinned(
                UpdatePinnedForumTopic(peer=PeerChannel(123), topic_id=1, pinned=False)
            )
        assert not delivery.failed
        cast(MagicMock, client.claim_image_scope).assert_called()
        assert db.execute(
            "SELECT pinned,pinned_order FROM topic_metadata WHERE dialog_id=? AND topic_id=1", (DIALOG,)
        ).fetchone() == (1, 50)
        assert db.execute(
            "SELECT revision,pending FROM topic_catalog_state WHERE dialog_id=?", (DIALOG,)
        ).fetchone() == (7, 0)
    finally:
        db.close()
