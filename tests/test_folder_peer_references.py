import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path

from telethon import types
from telethon.errors import ChatAdminRequiredError, PeerIdInvalidError
from telethon.tl.functions.messages import GetPeerDialogsRequest

from mcp_telegram.auth_scope import TelegramAuthScope
from mcp_telegram.dialog_fact_observations import claim_dialog_fact
from mcp_telegram.folders.reference_demand import FolderPeerMetadataDemandAdapter
from mcp_telegram.folders.sqlite_repository import SQLiteFolderSnapshotRepository
from mcp_telegram.folders.telegram_adapter import TelethonTelegramFolderGateway, folder_update_from_native
from mcp_telegram.image_authorization import ScopeCell, ScopeToken
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.sync_transactions import write_savepoint
from mcp_telegram.telegram_demand import RpcAttemptBudget
from tests.sqlite_writer_helpers import inline_writer


def _requested_user(request: GetPeerDialogsRequest) -> types.InputPeerUser:
    item = request.peers[0]
    assert isinstance(item, types.InputDialogPeer)
    assert isinstance(item.peer, types.InputPeerUser)
    return item.peer


def test_native_folder_peers_survive_restart_without_changing_membership(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    scope = TelegramAuthScope(1, 42, 2, 123)
    native = types.DialogFilter(
        4,
        types.TextWithEntities("Folder", []),
        [types.InputPeerUser(11, 111)],
        [types.InputPeerUser(11, 111)],
        [types.InputPeerChannel(22, 222)],
    )
    update = replace(folder_update_from_native(types.UpdateDialogFilter(4, native)), auth_scope=scope)
    with closing(sqlite3.connect(path)) as conn:
        with write_savepoint(conn):
            assert SQLiteFolderSnapshotRepository(conn).apply_supplied_update(update, account_id=42, order=1, now=1)
    with closing(sqlite3.connect(path)) as conn:
        rows = conn.execute(
            "SELECT dialog_id,kind,access_hash,metadata_state FROM folder_peer_references ORDER BY dialog_id"
        ).fetchall()
        assert rows == [(-1000000000022, "channel", 222, "pending"), (11, "user", 111, "pending")]
        assert not conn.execute("SELECT 1 FROM telegram_folder_local_members WHERE dialog_id=-1000000000022").fetchall()
        with write_savepoint(conn):
            assert SQLiteFolderSnapshotRepository(conn).apply_supplied_update(update, account_id=42, order=2, now=2)
        assert conn.execute("SELECT COUNT(*) FROM folder_peer_references").fetchone() == (2,)


async def test_metadata_response_without_dialog_keeps_entity_and_settles(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    scope = TelegramAuthScope(1, 42, 2, 123)
    cell = ScopeCell()
    assert cell.publish(scope)

    class Client:
        session = None

        def retained_auth_scope(self):
            return scope

        def capture_image_scope(self):
            return cell.capture()

        def claim_image_scope(self, token: ScopeToken):
            return cell.claim(token)

        async def __call__(self, request: GetPeerDialogsRequest):
            assert _requested_user(request).user_id == 11
            return types.messages.PeerDialogs(
                [],
                [],
                [],
                [types.User(11, first_name="Known", access_hash=111)],
                types.updates.State(pts=0, qts=0, date=None, seq=0, unread_count=0),  # pyright: ignore[reportArgumentType] - Telethon annotates this State as its module
            )

    native = types.DialogFilter(4, types.TextWithEntities("Folder", []), [], [types.InputPeerUser(11, 111)], [])
    update = replace(folder_update_from_native(types.UpdateDialogFilter(4, native)), auth_scope=scope)
    with closing(sqlite3.connect(path)) as conn:
        with write_savepoint(conn):
            SQLiteFolderSnapshotRepository(conn).apply_supplied_update(update, account_id=42, order=1, now=1)
        worker = FolderPeerMetadataDemandAdapter(Client(), conn, inline_writer(conn), 42)
        status = worker.status(0)
        assert status is not None and status.selected is not None
        await worker.run_slice(RpcAttemptBudget(1, selected=status.selected))
        assert conn.execute("SELECT name FROM entities WHERE id=11").fetchone() == ("Known",)
        assert conn.execute("SELECT metadata_state FROM folder_peer_references WHERE dialog_id=11").fetchone() == (
            "omitted",
        )
        assert worker.status(0) is None


async def test_metadata_response_after_account_change_is_discarded(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    scope = TelegramAuthScope(1, 42, 2, 123)
    cell = ScopeCell()
    assert cell.publish(scope)

    class Client:
        session = None

        def retained_auth_scope(self):
            return scope

        def capture_image_scope(self):
            return cell.capture()

        def claim_image_scope(self, token: ScopeToken):
            return cell.claim(token)

        async def __call__(self, request: GetPeerDialogsRequest):
            assert cell.publish(TelegramAuthScope(1, 43, 2, 999))
            return types.messages.PeerDialogs(
                [],
                [],
                [],
                [types.User(11, first_name="Wrong")],
                types.updates.State(pts=0, qts=0, date=None, seq=0, unread_count=0),  # pyright: ignore[reportArgumentType] - Telethon annotates this State as its module
            )

    native = types.DialogFilter(4, types.TextWithEntities("Folder", []), [], [types.InputPeerUser(11, 111)], [])
    update = replace(folder_update_from_native(types.UpdateDialogFilter(4, native)), auth_scope=scope)
    with closing(sqlite3.connect(path)) as conn:
        with write_savepoint(conn):
            SQLiteFolderSnapshotRepository(conn).apply_supplied_update(update, account_id=42, order=1, now=1)
        worker = FolderPeerMetadataDemandAdapter(Client(), conn, inline_writer(conn), 42)
        status = worker.status(0)
        assert status is not None and status.selected is not None
        result = await worker.run_slice(RpcAttemptBudget(1, selected=status.selected))
        assert result.kind.value == "stale"
        assert conn.execute("SELECT 1 FROM entities WHERE id=11").fetchone() is None
        assert conn.execute("SELECT metadata_state FROM folder_peer_references").fetchone() == ("pending",)


async def test_equal_folder_snapshot_recovers_missing_hash(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    scope = TelegramAuthScope(1, 42, 2, 123)
    native = types.DialogFilter(4, types.TextWithEntities("Folder", []), [], [types.InputPeerUser(11, 111)], [])

    class Client:
        async def __call__(self, request: object):
            return types.messages.DialogFilters([native])

    gateway = TelethonTelegramFolderGateway(Client())
    observation = replace(await gateway.fetch_rules(started_at=1), auth_scope=scope)
    with closing(sqlite3.connect(path)) as conn:
        repo = SQLiteFolderSnapshotRepository(conn)
        with write_savepoint(conn):
            repo.project_observation(observation, completed_at=1, observation_order=1, owner_account_id=42)
        conn.execute("UPDATE folder_peer_references SET access_hash=NULL,kind='unknown',auth_scope=NULL")
        conn.commit()
        second = replace(await gateway.fetch_rules(started_at=2), auth_scope=scope)
        assert second.token == observation.token
        with write_savepoint(conn):
            repo.project_observation(second, completed_at=2, observation_order=2, owner_account_id=42)
        assert conn.execute("SELECT kind,access_hash FROM folder_peer_references").fetchone() == ("user", 111)


async def test_metadata_denial_does_not_mark_history_inaccessible(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    scope = TelegramAuthScope(1, 42, 2, 123)
    cell = ScopeCell()
    assert cell.publish(scope)

    class Client:
        session = None

        def retained_auth_scope(self):
            return scope

        def capture_image_scope(self):
            return cell.capture()

        def claim_image_scope(self, token: ScopeToken):
            return cell.claim(token)

        async def __call__(self, request: GetPeerDialogsRequest):
            raise ChatAdminRequiredError(request)

    native = types.DialogFilter(4, types.TextWithEntities("Folder", []), [], [types.InputPeerUser(11, 111)], [])
    update = replace(folder_update_from_native(types.UpdateDialogFilter(4, native)), auth_scope=scope)
    with closing(sqlite3.connect(path)) as conn:
        with write_savepoint(conn):
            SQLiteFolderSnapshotRepository(conn).apply_supplied_update(update, account_id=42, order=1, now=1)
        worker = FolderPeerMetadataDemandAdapter(Client(), conn, inline_writer(conn), 42)
        status = worker.status(0)
        assert status is not None and status.selected is not None
        await worker.run_slice(RpcAttemptBudget(1, selected=status.selected))
        assert conn.execute("SELECT metadata_state FROM folder_peer_references").fetchone() == ("denied",)
        assert conn.execute("SELECT status FROM synced_dialogs WHERE dialog_id=11").fetchone() is None
        assert worker.status(0) is None


async def test_returned_dialog_publishes_observed_facts(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    scope = TelegramAuthScope(1, 42, 2, 123)
    cell = ScopeCell()
    assert cell.publish(scope)
    dialog = types.Dialog(types.PeerUser(11), 0, 7, 8, 3, 1, 0, 0, types.PeerNotifySettings(), folder_id=1)

    class Client:
        session = None

        def retained_auth_scope(self):
            return scope

        def capture_image_scope(self):
            return cell.capture()

        def claim_image_scope(self, token: ScopeToken):
            return cell.claim(token)

        async def __call__(self, request: GetPeerDialogsRequest):
            return types.messages.PeerDialogs(
                [dialog],
                [],
                [],
                [types.User(11, first_name="Known")],
                types.updates.State(pts=0, qts=0, date=None, seq=0, unread_count=0),  # pyright: ignore[reportArgumentType] - Telethon annotates this State as its module
            )

    native = types.DialogFilter(4, types.TextWithEntities("Folder", []), [], [types.InputPeerUser(11, 111)], [])
    update = replace(folder_update_from_native(types.UpdateDialogFilter(4, native)), auth_scope=scope)
    with closing(sqlite3.connect(path)) as conn:
        with write_savepoint(conn):
            SQLiteFolderSnapshotRepository(conn).apply_supplied_update(update, account_id=42, order=1, now=1)
        worker = FolderPeerMetadataDemandAdapter(Client(), conn, inline_writer(conn), 42)
        status = worker.status(0)
        assert status is not None and status.selected is not None
        await worker.run_slice(RpcAttemptBudget(1, selected=status.selected))
        assert conn.execute(
            "SELECT archived,unread_count,unread_mentions_count,read_inbox_max_id,read_outbox_max_id FROM dialogs WHERE dialog_id=11"
        ).fetchone() == (1, 3, 1, 7, 8)
        assert conn.execute("SELECT metadata_state FROM folder_peer_references").fetchone() == ("returned",)


async def test_newer_unread_event_wins_over_inflight_dialog(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    scope = TelegramAuthScope(1, 42, 2, 123)
    cell = ScopeCell()
    assert cell.publish(scope)
    dialog = types.Dialog(types.PeerUser(11), 0, 7, 8, 3, 0, 0, 0, types.PeerNotifySettings())
    with closing(sqlite3.connect(path)) as conn:

        class Client:
            session = None

            def retained_auth_scope(self):
                return scope

            def capture_image_scope(self):
                return cell.capture()

            def claim_image_scope(self, token: ScopeToken):
                return cell.claim(token)

            async def __call__(self, request: GetPeerDialogsRequest):
                with write_savepoint(conn):
                    conn.execute("INSERT INTO dialogs(dialog_id,unread_count) VALUES(11,99)")
                    claim_dialog_fact(conn, 11, "unread_count", 999)
                return types.messages.PeerDialogs(
                    [dialog],
                    [],
                    [],
                    [types.User(11, first_name="Known")],
                    types.updates.State(pts=0, qts=0, date=None, seq=0, unread_count=0),  # pyright: ignore[reportArgumentType] - Telethon annotates this State as its module
                )

        native = types.DialogFilter(4, types.TextWithEntities("Folder", []), [], [types.InputPeerUser(11, 111)], [])
        update = replace(folder_update_from_native(types.UpdateDialogFilter(4, native)), auth_scope=scope)
        with write_savepoint(conn):
            SQLiteFolderSnapshotRepository(conn).apply_supplied_update(update, account_id=42, order=1, now=1)
        worker = FolderPeerMetadataDemandAdapter(Client(), conn, inline_writer(conn), 42)
        status = worker.status(0)
        assert status is not None and status.selected is not None
        await worker.run_slice(RpcAttemptBudget(1, selected=status.selected))
        assert conn.execute("SELECT unread_count FROM dialogs WHERE dialog_id=11").fetchone() == (99,)


def test_not_returned_is_excluded_automatically_but_explicit_membership_remains(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    with closing(sqlite3.connect(path)) as conn:
        with write_savepoint(conn):
            conn.execute(
                "UPDATE dialog_directory_publication SET account_id=42,generation=1,observation_started_at=1 WHERE singleton=1"
            )
            conn.executemany("INSERT INTO dialogs(dialog_id,type) VALUES(?,'group')", [(-11,), (-12,)])
            conn.executemany(
                "INSERT INTO dialog_directory_facts(dialog_id,category) VALUES(?,'group')", [(-11,), (-12,)]
            )
            conn.executemany(
                "INSERT INTO dialog_listing_observations VALUES(42,?,'not_returned','completed_directory',10,1)",
                [(-11,), (-12,)],
            )
            auto = types.DialogFilter(4, types.TextWithEntities("Auto", []), [], [], [], groups=True)
            manual = types.DialogFilter(5, types.TextWithEntities("Manual", []), [], [types.InputPeerChat(11)], [])
            repo = SQLiteFolderSnapshotRepository(conn)
            assert repo.apply_supplied_update(
                folder_update_from_native(types.UpdateDialogFilter(4, auto)), account_id=42, order=1, now=10
            )
            assert repo.apply_supplied_update(
                folder_update_from_native(types.UpdateDialogFilter(5, manual)), account_id=42, order=2, now=10
            )
        rows = conn.execute(
            "SELECT folder_id,dialog_id FROM telegram_folder_local_members ORDER BY folder_id,dialog_id"
        ).fetchall()
        assert rows == [(5, -11)]


def test_complete_retained_dialog_does_not_schedule_metadata_rpc(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    with closing(sqlite3.connect(path)) as conn:
        with write_savepoint(conn):
            conn.execute("INSERT INTO dialogs(dialog_id,name,type,identity_complete) VALUES(11,'Known','user',1)")
            native = types.DialogFilter(4, types.TextWithEntities("Folder", []), [], [types.InputPeerUser(11, 111)], [])
            update = folder_update_from_native(types.UpdateDialogFilter(4, native))
            SQLiteFolderSnapshotRepository(conn).apply_supplied_update(update, account_id=42, order=1, now=1)
        worker = FolderPeerMetadataDemandAdapter(object(), conn, inline_writer(conn), 42)
        assert worker.status(0) is None
        assert conn.execute("SELECT metadata_state,metadata_observed_at FROM folder_peer_references").fetchone() == (
            "retained",
            None,
        )


def test_replayed_rule_keeps_work_revision_but_new_capability_rearms(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    scope = TelegramAuthScope(1, 42, 2, 123)

    def native(hash_value: int):
        rule = types.DialogFilter(
            4, types.TextWithEntities("Folder", []), [], [types.InputPeerUser(11, hash_value)], []
        )
        return replace(folder_update_from_native(types.UpdateDialogFilter(4, rule)), auth_scope=scope)

    with closing(sqlite3.connect(path)) as conn:
        repo = SQLiteFolderSnapshotRepository(conn)
        with write_savepoint(conn):
            repo.apply_supplied_update(native(111), account_id=42, order=1, now=1)
        worker = FolderPeerMetadataDemandAdapter(object(), conn, inline_writer(conn), 42)
        before = worker.status(0)
        with write_savepoint(conn):
            repo.apply_supplied_update(native(111), account_id=42, order=2, now=2)
        assert worker.status(0) == before
        with write_savepoint(conn):
            conn.execute(
                "UPDATE folder_peer_references SET metadata_state='denied',metadata_source='telegram_get_peer_dialogs',metadata_observed_at=3,metadata_error_code='ChatAdminRequiredError'"
            )
            repo.apply_supplied_update(native(222), account_id=42, order=3, now=3)
        assert worker.status(0) != before
        assert conn.execute(
            "SELECT metadata_state,metadata_source,metadata_observed_at,metadata_error_code FROM folder_peer_references"
        ).fetchone() == ("pending", None, None, None)


async def test_metadata_attempt_uses_new_folder_hash_over_old_session_hash(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    scope = TelegramAuthScope(1, 42, 2, 123)
    cell = ScopeCell()
    assert cell.publish(scope)

    class Session:
        def get_input_entity(self, peer: int):
            return types.InputPeerUser(peer, 111)

    class Client:
        session = Session()

        def retained_auth_scope(self):
            return scope

        def capture_image_scope(self):
            return cell.capture()

        def claim_image_scope(self, token: ScopeToken):
            return cell.claim(token)

        async def __call__(self, request: GetPeerDialogsRequest):
            assert _requested_user(request).access_hash == 222
            return types.messages.PeerDialogs(
                [],
                [],
                [],
                [types.User(11, first_name="Known")],
                types.updates.State(pts=0, qts=0, date=None, seq=0, unread_count=0),  # pyright: ignore[reportArgumentType] - Telethon annotates this State as its module
            )

    with closing(sqlite3.connect(path)) as conn:
        native = types.DialogFilter(4, types.TextWithEntities("Folder", []), [], [types.InputPeerUser(11, 222)], [])
        update = replace(folder_update_from_native(types.UpdateDialogFilter(4, native)), auth_scope=scope)
        with write_savepoint(conn):
            SQLiteFolderSnapshotRepository(conn).apply_supplied_update(update, account_id=42, order=1, now=1)
        worker = FolderPeerMetadataDemandAdapter(Client(), conn, inline_writer(conn), 42)
        status = worker.status(0)
        assert status is not None and status.selected is not None
        await worker.run_slice(RpcAttemptBudget(1, selected=status.selected))
        assert conn.execute("SELECT metadata_state FROM folder_peer_references").fetchone() == ("omitted",)


async def test_invalid_folder_hash_retries_distinct_session_hash_once(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    scope = TelegramAuthScope(1, 42, 2, 123)
    cell = ScopeCell()
    assert cell.publish(scope)
    attempted = []

    class Session:
        def get_input_entity(self, peer: int):
            return types.InputPeerUser(peer, 222)

    class Client:
        session = Session()

        def retained_auth_scope(self):
            return scope

        def capture_image_scope(self):
            return cell.capture()

        def claim_image_scope(self, token: ScopeToken):
            return cell.claim(token)

        async def __call__(self, request: GetPeerDialogsRequest):
            attempted.append(_requested_user(request).access_hash)
            if len(attempted) == 1:
                raise PeerIdInvalidError(request)
            return types.messages.PeerDialogs(
                [],
                [],
                [],
                [types.User(11, first_name="Known")],
                types.updates.State(pts=0, qts=0, date=None, seq=0, unread_count=0),  # pyright: ignore[reportArgumentType] - Telethon annotates this State as its module
            )

    with closing(sqlite3.connect(path)) as conn:
        native = types.DialogFilter(4, types.TextWithEntities("Folder", []), [], [types.InputPeerUser(11, 111)], [])
        update = replace(folder_update_from_native(types.UpdateDialogFilter(4, native)), auth_scope=scope)
        with write_savepoint(conn):
            repo = SQLiteFolderSnapshotRepository(conn)
            repo.apply_supplied_update(update, account_id=42, order=1, now=1)
        worker = FolderPeerMetadataDemandAdapter(Client(), conn, inline_writer(conn), 42)
        first = worker.status(0)
        assert first is not None and first.selected is not None
        await worker.run_slice(RpcAttemptBudget(1, selected=first.selected))
        assert attempted == [111]
        assert conn.execute(
            "SELECT metadata_state,metadata_candidate,metadata_error_code FROM folder_peer_references"
        ).fetchone() == ("pending", "session", "PeerIdInvalidError")
        with write_savepoint(conn):
            repo.apply_supplied_update(update, account_id=42, order=2, now=2)
        second = worker.status(0)
        assert second is not None and second.selected is not None and second.selected != first.selected
        await worker.run_slice(RpcAttemptBudget(1, selected=second.selected))
        assert attempted == [111, 222]
        assert worker.status(0) is None
        assert conn.execute("SELECT metadata_state FROM folder_peer_references").fetchone() == ("omitted",)
