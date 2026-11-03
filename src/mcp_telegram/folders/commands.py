"""Native folder command calculations."""

import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Protocol, SupportsBytes, cast

from telethon.tl.types import TypeInputPeer, TypeMessageEntity

from ..auth_scope import TelegramAuthScope
from ..image_authorization import ScopeToken
from ..native_write_fence import NativeWriteReceiptBinding
from ..sqlite_writer import SQLiteWriter
from ..telegram_demand import DemandStatus, RpcAttemptBudget
from ..telegram_demand_progress import SliceOutcome, WorkRevision
from .contracts import FolderRule

_FIRST_CUSTOM_FOLDER_ID = 2


def _validate_membership_changes(groups: tuple[set[int], ...]) -> None:
    if any(0 in group for group in groups) or any(
        groups[left] & groups[right] for left, right in ((0, 1), (0, 2), (1, 2))
    ):
        raise ValueError("dialog IDs must be nonzero and disjoint across actions")


def _append_missing(
    original: tuple[int, ...], additions: tuple[int, ...], *, existing: tuple[int, ...] = ()
) -> tuple[int, ...]:
    return original + tuple(
        value for value in dict.fromkeys(additions) if value not in original and value not in existing
    )


def apply_membership_changes(
    rule: FolderRule,
    *,
    add_dialog_ids: tuple[int, ...] = (),
    remove_dialog_ids: tuple[int, ...] = (),
    clear_excluded_dialog_ids: tuple[int, ...] = (),
) -> FolderRule:
    """Apply explicit native membership intent while retaining all other rules."""
    groups = tuple(map(set, (add_dialog_ids, remove_dialog_ids, clear_excluded_dialog_ids)))
    _validate_membership_changes(groups)
    add, remove, clear = groups
    included = tuple(value for value in rule.included_ids if value not in remove)
    included = _append_missing(included, add_dialog_ids, existing=rule.pinned_ids)
    pinned = tuple(value for value in rule.pinned_ids if value not in remove)
    excluded = tuple(value for value in rule.excluded_ids if value not in add and value not in clear)
    excluded = _append_missing(excluded, remove_dialog_ids)
    return replace(rule, included_ids=included, pinned_ids=pinned, excluded_ids=excluded)


def _entity_constructor(entity: dict[str, object]) -> Callable[..., object]:
    from telethon.tl import types

    name = entity.get("_")
    constructor = getattr(types, name, None) if isinstance(name, str) and name.startswith("MessageEntity") else None
    if not callable(constructor) or name == "MessageEntityUnknown":
        raise ValueError("unsupported title entity")
    return cast(Callable[..., object], constructor)


def _title_entity(entity: dict[str, object], boundaries: set[int], resolve: Callable[[int], object | None]) -> object:
    constructor = _entity_constructor(entity)
    start, length = entity.get("offset"), entity.get("length")
    if type(start) is not int or type(length) is not int:
        raise ValueError("unsupported title entity")
    if length <= 0 or start not in boundaries or start + length not in boundaries:
        raise ValueError("unsupported title entity")
    if entity.get("_") == "MessageEntityMentionName":
        return _mention_entity(entity, resolve, start, length)
    try:
        return constructor(**_entity_values(entity))
    except (TypeError, ValueError) as exc:
        raise ValueError("unsupported title entity") from exc


def _entity_values(entity: dict[str, object]) -> dict[str, object]:
    values = {key: value for key, value in entity.items() if key != "_"}
    if entity.get("_") == "MessageEntityFormattedDate" and isinstance(values.get("date"), str):
        from datetime import datetime

        values["date"] = datetime.fromisoformat(cast(str, values["date"]))
    return values


def _mention_entity(
    entity: dict[str, object], resolve: Callable[[int], object | None], start: int, length: int
) -> object:
    from telethon.tl import types

    user_id = entity.get("user_id")
    if type(user_id) is not int or user_id <= 0:
        raise ValueError("invalid title mention")
    peer = resolve(user_id)
    if not isinstance(peer, types.InputPeerUser) or peer.user_id != user_id:
        raise ValueError("title mention peer unavailable")
    return types.InputMessageEntityMentionName(start, length, types.InputUser(peer.user_id, peer.access_hash))


def _title_entities(rule: FolderRule, resolve: Callable[[int], object | None]) -> list[TypeMessageEntity]:
    boundaries = {0}
    offset = 0
    for char in rule.title:
        offset += len(char.encode("utf-16-le")) // 2
        boundaries.add(offset)
    entities = [_title_entity(entity, boundaries, resolve) for entity in rule.title_entities]
    if not all(isinstance(entity, TypeMessageEntity) for entity in entities):
        raise ValueError("unsupported title entity")
    return cast(list[TypeMessageEntity], entities)


def _input_peers(ids: tuple[int, ...], resolve: Callable[[int], object | None]) -> list[TypeInputPeer]:
    peers = [resolve(dialog_id) for dialog_id in ids]
    if not all(isinstance(peer, TypeInputPeer) for peer in peers):
        raise ValueError("peer access unavailable")
    return cast(list[TypeInputPeer], peers)


def native_folder_request(
    action: str, target: int, rule: FolderRule | None, resolve: Callable[[int], object | None]
) -> object:
    """Reconstruct one ordinary-folder request from complete retained semantics."""
    from telethon.tl import types
    from telethon.tl.functions.messages import UpdateDialogFilterRequest

    from .contracts import DialogCategory, FolderRuleKind

    if target < _FIRST_CUSTOM_FOLDER_ID:
        raise ValueError("invalid folder target")
    if action == "delete" and rule is None:
        return UpdateDialogFilterRequest(target, None)
    if action not in {"create", "update"} or rule is None or rule.folder_id != target:
        raise ValueError("invalid folder intent")
    if not rule.complete or rule.kind != FolderRuleKind.FILTER:
        raise ValueError("folder definition cannot be safely edited")
    title = types.TextWithEntities(rule.title, _title_entities(rule, resolve))
    native = types.DialogFilter(
        id=target,
        title=title,
        pinned_peers=_input_peers(rule.pinned_ids, resolve),
        include_peers=_input_peers(rule.included_ids, resolve),
        exclude_peers=_input_peers(rule.excluded_ids, resolve),
        contacts=DialogCategory.CONTACT in rule.categories,
        non_contacts=DialogCategory.NON_CONTACT in rule.categories,
        bots=DialogCategory.BOT in rule.categories,
        groups=DialogCategory.GROUP in rule.categories,
        broadcasts=DialogCategory.BROADCAST in rule.categories,
        exclude_archived=rule.exclude_archived,
        exclude_read=rule.exclude_read,
        exclude_muted=rule.exclude_muted,
        title_noanimate=rule.title_noanimate,
        emoticon=rule.emoticon,
        color=rule.color,
    )
    return UpdateDialogFilterRequest(target, native)


def folder_intent_fingerprint(action: str, target: int, before: str | None, intended: str | None) -> str:
    """Bind a mutation to its target and capability-free before/after facts."""
    import hashlib
    import json

    payload = json.dumps([action, target, before, intended], ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _intent_shape_valid(action: str, before: str | None, intended: str | None) -> bool:
    if action == "delete":
        return before is not None and intended is None
    return action in {"create", "update"} and intended is not None


def validate_folder_wire_request(
    binding: object,
    request: object,
    before: str | None,
    intended: str | None,
    resolve: Callable[[int], object | None],
) -> bool:
    """Independently reconstruct the complete expected request from durable intent."""
    from telethon.tl.functions.messages import UpdateDialogFilterRequest

    from ..native_write_fence import NativeWriteReceiptBinding
    from .sqlite_repository import decode_folder_definition

    if not isinstance(binding, NativeWriteReceiptBinding) or binding.domain != "folder":
        return False
    if not isinstance(request, UpdateDialogFilterRequest) or (
        folder_intent_fingerprint(binding.action, binding.target, before, intended) != binding.semantic_fingerprint
    ):
        return False
    if not _intent_shape_valid(binding.action, before, intended):
        return False
    try:
        rule = None if intended is None else decode_folder_definition(intended)
        expected = native_folder_request(binding.action, binding.target, rule, resolve)
        return bytes(cast(SupportsBytes, expected)) == bytes(request)
    except ValueError, TypeError, AttributeError, UnicodeError:
        return False


class FolderReceiptClient(Protocol):
    def retained_auth_scope(self) -> TelegramAuthScope | None: ...

    def retained_draft_peer(self, dialog_id: int) -> object | None: ...

    def capture_image_scope(self) -> ScopeToken | None: ...

    def claim_image_scope(self, token: ScopeToken) -> bool: ...


class FolderCommandClient(FolderReceiptClient, Protocol):
    async def __call__(self, _request: object, **_kwargs: object) -> object: ...


class FolderCommandWorker(Protocol):
    async def force_refresh(self, budget: RpcAttemptBudget | None = None) -> object: ...


class FolderReceiptOwner:
    """Authoritative gateway callbacks; never acquire Telegram or command locks."""

    def __init__(self, writer: SQLiteWriter, client: FolderReceiptClient) -> None:
        self._writer = writer
        self._client = client
        self._permits: dict[NativeWriteReceiptBinding, tuple[TelegramAuthScope, str, int, float]] = {}
        self._terminal_offer: Callable[[], None] | None = None

    def bind_terminal_offer(self, offer: Callable[[], None]) -> None:
        self._terminal_offer = offer

    async def notify_terminal(self, binding: NativeWriteReceiptBinding, outcome: str) -> None:
        scope = self._client.retained_auth_scope()
        if (
            outcome in {"acknowledged", "unknown"}
            and scope is not None
            and scope.account_id == binding.account_id
            and self._terminal_offer is not None
        ):
            self._terminal_offer()

    async def arm(
        self, binding: NativeWriteReceiptBinding, scope: TelegramAuthScope, version: str, intake: int, deadline: float
    ) -> None:
        import time

        from ..native_write_fence import FolderWriteRejectedError
        from .intake import marker

        if (
            binding in self._permits
            or self._client.retained_auth_scope() != scope
            or not self._scope_matches(binding)
            or marker() != intake
            or time.monotonic() >= deadline
        ):
            raise FolderWriteRejectedError("folder_permit_stale")
        issued = await self._write(lambda conn: _issue_folder_permit(conn, binding, version))
        if (
            not issued
            or self._client.retained_auth_scope() != scope
            or marker() != intake
            or time.monotonic() >= deadline
        ):
            raise FolderWriteRejectedError("folder_permit_stale")
        self._permits[binding] = (scope, version, intake, deadline)

    def disarm(self, binding: NativeWriteReceiptBinding) -> None:
        self._permits.pop(binding, None)

    async def preclaim(self, binding: NativeWriteReceiptBinding) -> Callable[[], bool]:
        import time

        from ..native_write_fence import FolderWriteRejectedError
        from .intake import marker

        permit = self._permits.get(binding)
        if permit is None:
            raise FolderWriteRejectedError("folder_permit_missing")
        scope, version, intake, deadline = permit
        accepted = await self._write(
            lambda conn: _preclaim_matches(conn, binding, version, self._scope_matches(binding))
        )
        if not accepted:
            raise FolderWriteRejectedError("folder_version_changed")
        return lambda: (
            accepted
            and self._permits.get(binding) is permit
            and self._client.retained_auth_scope() == scope
            and marker() == intake
            and time.monotonic() < deadline
        )

    async def _write[T](self, operation: Callable[[sqlite3.Connection], T]) -> T:
        import asyncio

        task = asyncio.create_task(self._writer.run(operation))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
            try:
                task.result()
            except Exception:  # noqa: BLE001 - cancellation remains authoritative after drain.
                pass
            raise

    def _scope_matches(self, binding: object) -> bool:
        from ..native_write_fence import NativeWriteReceiptBinding

        if not isinstance(binding, NativeWriteReceiptBinding):
            return False
        scope = self._client.retained_auth_scope()
        return scope is not None and (scope.account_id, scope.dc_id, scope.auth_key_id) == (
            binding.account_id,
            binding.dc_id,
            binding.key_id,
        )

    async def verify_request(self, binding: object, request: object) -> bool:
        from .command_receipts import read_intent

        if not self._scope_matches(binding):
            return False
        intent = await self._write(lambda conn: read_intent(conn, binding))
        if intent is None or not self._scope_matches(binding):
            return False
        before, intended = intent
        return validate_folder_wire_request(binding, request, before, intended, self._client.retained_draft_peer)

    async def bind_attempt(self, binding: object, attempt_id: str) -> None:
        from .command_receipts import bind_attempt

        await self._write(lambda conn: bind_attempt(conn, binding, attempt_id))

    async def record_wire_outcome(self, binding: object, attempt_id: str, outcome: str) -> None:
        from .command_receipts import record_wire_outcome

        await self._write(lambda conn: record_wire_outcome(conn, binding, attempt_id, outcome))

    async def validate_binding(self, binding: object, attempt_id: str) -> bool:
        from .command_receipts import validate_binding

        return bool(await self._write(lambda conn: validate_binding(conn, binding, attempt_id)))

    async def recover_binding(self, binding: object, attempt_id: str) -> bool:
        from .command_receipts import recover_binding

        return await self._write(lambda conn: recover_binding(conn, binding, attempt_id))


def folder_catalog_version(conn: sqlite3.Connection, account_id: int) -> str | None:
    """Return the account-bound durable semantic revision for a complete catalog."""
    from .sqlite_repository import SQLiteFolderSnapshotRepository

    owner, revision, token, complete = SQLiteFolderSnapshotRepository(conn).read_catalog_version()
    if owner != account_id or token is None or not complete:
        return None
    return f"folder-v1:{account_id}:{revision}:{token}"


def folder_catalog_owner(conn: sqlite3.Connection) -> int | None:
    from .sqlite_repository import SQLiteFolderSnapshotRepository

    return SQLiteFolderSnapshotRepository(conn).read_catalog_version()[0]


def read_folder(conn: sqlite3.Connection, account_id: int, folder_id: int) -> dict[str, object]:
    """Read retained folder facts without initiating acquisition."""
    from ..dialog_directory_coverage import read_dialog_directory_coverage
    from .read_model import folder_snapshot
    from .sqlite_repository import SQLiteFolderSnapshotRepository, read_folder_rules

    owner, _, _, _ = SQLiteFolderSnapshotRepository(conn).read_catalog_version()
    if owner != account_id:
        return {"account_id": account_id, "presence": "unknown", "catalog_version": None}
    catalog_version = folder_catalog_version(conn, account_id)
    rule = next((item for item in read_folder_rules(conn) if item.folder_id == folder_id), None)
    snapshot = folder_snapshot(conn, stale_after_seconds=900)
    acquisition = _folder_acquisition(conn)
    if rule is None:
        return {
            "account_id": account_id,
            "presence": "absent" if catalog_version else "unknown",
            "catalog_version": catalog_version,
            "folder_snapshot": snapshot,
            "acquisition": acquisition,
            "directory_coverage": read_dialog_directory_coverage(conn).to_wire(),
        }
    definition = _folder_definition(rule)
    reason = _folder_write_reason(rule)
    membership_rows = cast(
        list[tuple[str, int]],
        conn.execute(
            "SELECT state,COUNT(*) FROM telegram_folder_local_members WHERE namespace=? AND folder_id=? GROUP BY state",
            (rule.namespace, rule.folder_id),
        ).fetchall(),
    )
    counts = {str(state): int(count) for state, count in membership_rows}
    return {
        "account_id": account_id,
        "presence": "present",
        "catalog_version": catalog_version,
        "expected_version": catalog_version if rule.complete else None,
        "definition": definition,
        "definition_complete": rule.complete,
        "supported_actions": ["update", "delete"] if reason is None else [],
        "unsupported_reason": reason,
        "membership": {"present": counts.get("present", 0), "unknown": counts.get("unknown", 0)},
        "directory_coverage": read_dialog_directory_coverage(conn).to_wire(),
        "folder_snapshot": snapshot,
        "acquisition": acquisition,
    }


def _folder_definition(rule: FolderRule) -> dict[str, object]:
    from dataclasses import asdict

    definition = asdict(rule)
    definition["categories"] = sorted(category.value for category in rule.categories)
    definition["kind"] = rule.kind.value
    for field in ("included_ids", "pinned_ids", "excluded_ids", "title_entities"):
        definition[field] = list(cast(tuple[object, ...], definition[field]))
    return definition


def _folder_acquisition(conn: sqlite3.Connection) -> dict[str, object]:
    row = cast(
        tuple[object, ...] | None,
        conn.execute(
            "SELECT last_attempt_at,last_outcome,next_retry_at,consecutive_failures,coverage_status "
            "FROM telegram_folder_projection_state WHERE singleton=1"
        ).fetchone(),
    )
    if row is None:
        return {"coverage": "unavailable"}
    return {
        "last_attempt_at": row[0],
        "last_outcome": row[1],
        "next_retry_at": row[2],
        "consecutive_failures": row[3],
        "coverage": row[4],
    }


def _folder_write_reason(rule: FolderRule) -> str | None:
    from telethon.tl.types import InputPeerUser

    from .contracts import FolderRuleKind

    if not rule.complete:
        return "incomplete_definition"
    if rule.kind != FolderRuleKind.FILTER:
        return "unsupported_folder_kind"
    try:
        for entity in _title_entities(rule, lambda user_id: InputPeerUser(user_id, 0)):
            bytes(cast(SupportsBytes, entity))
    except ValueError, TypeError, UnicodeError, OverflowError:
        return "unsupported_title_entities"
    return None


def plan_folder_change(  # noqa: PLR0913 - native mutation fields are explicit
    action: str,
    folder_id: int,
    current: FolderRule | None,
    *,
    title: str | None = None,
    dialog_ids: tuple[int, ...] = (),
    add_dialog_ids: tuple[int, ...] = (),
    remove_dialog_ids: tuple[int, ...] = (),
    clear_excluded_dialog_ids: tuple[int, ...] = (),
) -> FolderRule | None:
    """Calculate one native folder definition without changing retained facts."""
    from .contracts import FolderRuleKind

    if action == "create":
        return _create_folder_rule(folder_id, current, title, dialog_ids)
    if current is None or not current.complete or current.kind != FolderRuleKind.FILTER:
        raise ValueError("folder definition unavailable or unsupported")
    if action == "delete":
        return None
    if action != "update":
        raise ValueError("invalid action")
    return _update_folder_rule(
        current,
        title=title,
        add_dialog_ids=add_dialog_ids,
        remove_dialog_ids=remove_dialog_ids,
        clear_excluded_dialog_ids=clear_excluded_dialog_ids,
    )


def _create_folder_rule(
    folder_id: int, current: FolderRule | None, title: str | None, dialog_ids: tuple[int, ...]
) -> FolderRule:
    if current is not None or title is None or not title.strip() or not dialog_ids or 0 in dialog_ids:
        raise ValueError("invalid create intent")
    return FolderRule(folder_id, title, included_ids=tuple(dict.fromkeys(dialog_ids)))


def _update_folder_rule(
    current: FolderRule,
    *,
    title: str | None,
    add_dialog_ids: tuple[int, ...],
    remove_dialog_ids: tuple[int, ...],
    clear_excluded_dialog_ids: tuple[int, ...],
) -> FolderRule:
    changed = apply_membership_changes(
        current,
        add_dialog_ids=add_dialog_ids,
        remove_dialog_ids=remove_dialog_ids,
        clear_excluded_dialog_ids=clear_excluded_dialog_ids,
    )
    if title is not None and title != current.title:
        if not title.strip():
            raise ValueError("title must not be blank")
        changed = replace(changed, title=title, title_entities=())
    if not (changed.included_ids or changed.pinned_ids or changed.categories):
        raise ValueError("manual folder must contain at least one dialog")
    return changed


def _operation_fingerprint(request: dict[str, object]) -> str:
    import hashlib
    import json

    return hashlib.sha256(
        json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


def _snapshot(conn: sqlite3.Connection, account_id: int) -> tuple[str | None, tuple[FolderRule, ...]]:
    from .sqlite_repository import read_folder_rules

    return folder_catalog_version(conn, account_id), read_folder_rules(conn)


def _rule_for_id(rules: tuple[FolderRule, ...], folder_id: int) -> FolderRule | None:
    return next((rule for rule in rules if rule.folder_id == folder_id), None)


def _new_folder_id(rules: tuple[FolderRule, ...]) -> int:
    import secrets

    used = {rule.folder_id for rule in rules}
    available = [value for value in range(2, 256) if value not in used]
    if not available:
        raise ValueError("no unused native folder ID available")
    return secrets.choice(available)


class FolderCommandOwner:
    """Serialize one native folder mutation under a bounded gateway demand."""

    def __init__(  # noqa: PLR0913 - command dependencies are explicit
        self,
        client: FolderCommandClient,
        writer: SQLiteWriter,
        worker: FolderCommandWorker,
        reader_conn: sqlite3.Connection,
        receipts: FolderReceiptOwner,
        *,
        offer_verification: Callable[[], None],
    ) -> None:
        import asyncio

        self._client = client
        self._writer = writer
        self._worker = worker
        self._reader_conn = reader_conn
        self._receipts = receipts
        self._offer_verification = offer_verification
        self._lock = asyncio.Lock()

    async def execute(self, request: Mapping[str, object]) -> dict[str, object]:
        import asyncio

        from ..telegram_demand import RpcAttemptBudget, create_demand_token, demand_context
        from ..telegram_rpc_consumers import DemandKind
        from ..telegram_rpc_scheduler import rpc_attempt_budget

        deadline = create_demand_token(DemandKind.FOLDER_COMMAND).admission_deadline
        try:
            async with asyncio.timeout_at(deadline), self._lock:
                with (
                    demand_context(DemandKind.FOLDER_COMMAND, deadline=deadline),
                    rpc_attempt_budget(RpcAttemptBudget(3)),
                ):
                    return await self._execute(dict(request), deadline)
        except TimeoutError:
            return _folder_error("completion_unconfirmed", "Repeat the exact operation with the same key.")

    async def _execute(self, request: dict[str, object], deadline: float) -> dict[str, object]:  # noqa: PLR0911 - each refusal has a distinct reason.
        from .command_receipts import find_operation

        scope = self._client.retained_auth_scope()
        if scope is None:
            return _folder_error("account_unverified", "Verify the connected account.")
        version = request.get("expected_catalog_version", request.get("expected_version"))
        if not isinstance(version, str) or not version.startswith(f"folder-v1:{scope.account_id}:"):
            return _folder_error("account_mismatch", "Read the current folder before retrying.")
        operation_key = str(request["operation_key"])
        fingerprint = _operation_fingerprint(request)
        previous = await self._writer.run(lambda conn: find_operation(conn, scope.account_id, operation_key))
        if self._client.retained_auth_scope() != scope:
            return _folder_error("account_mismatch", "Verify the connected account before retrying.")
        if previous is not None:
            if previous[0] != fingerprint:
                return _folder_error("operation_key_conflict", "Use the original payload or a new operation key.")
            return _operation_result(scope.account_id, operation_key, previous, request)
        fresh = await self._fresh_rules(scope, version)
        if isinstance(fresh, dict):
            return fresh
        rules, intake = fresh
        return await self._mutate(
            request, scope, rules, fingerprint=fingerprint, version=version, deadline=deadline, intake=intake
        )

    async def _fresh_rules(
        self, scope: TelegramAuthScope, version: str
    ) -> tuple[tuple[FolderRule, ...], int] | dict[str, object]:
        from .intake import marker

        current_version, _ = await self._writer.run(lambda conn: _snapshot(conn, scope.account_id))
        if version != current_version:
            return _folder_error("version_conflict", "Read the folder again before retrying.")
        from .refresh import FolderRefreshResult

        intake = marker()
        observed = await self._worker.force_refresh()
        if not isinstance(observed, FolderRefreshResult):
            return _folder_error("preflight_unavailable", "Retry after the folder catalog is available.")
        if observed is None or not observed.accepted or observed.observation_order is None:
            return _folder_error("preflight_unavailable", "Retry after the folder catalog is available.")
        current_version, rules = await self._writer.run(lambda conn: _snapshot(conn, scope.account_id))
        if version != current_version or self._client.retained_auth_scope() != scope or marker() != intake:
            return _folder_error("version_conflict", "Read the folder again before retrying.")
        return rules, intake

    async def _mutate(  # noqa: PLR0913 - command scope and fence are explicit
        self,
        request: dict[str, object],
        scope: TelegramAuthScope,
        rules: tuple[FolderRule, ...],
        *,
        fingerprint: str,
        version: str,
        deadline: float,
        intake: int,
    ) -> dict[str, object]:
        import asyncio
        import time

        from .command_receipts import insert_operation
        from .intake import marker

        try:
            binding, native, body, before, intended = _prepare_native_intent(
                request, scope, rules, self._client.retained_draft_peer
            )
        except ValueError, TypeError, UnicodeError:
            return _folder_error("unsupported_or_unavailable", "Check the folder and retained peer access.")
        try:
            await self._receipts._write(
                lambda conn: insert_operation(
                    conn,
                    binding,
                    operation_fingerprint=fingerprint,
                    expected_version=version,
                    before=before,
                    intended=intended,
                    now=int(time.time()),
                )
            )
        except asyncio.CancelledError:
            await self._receipts._write(lambda conn: _mark_not_sent(conn, binding))
            raise
        except ValueError:
            return _folder_error("operation_unresolved", "Resolve the previous folder operation first.")
        if before == intended:
            token = self._client.capture_image_scope()
            await self._receipts._write(
                lambda conn: _mark_unchanged(
                    conn,
                    binding,
                    version,
                    guard=lambda: token is not None and self._client.claim_image_scope(token) and marker() == intake,
                )
            )
        else:
            await self._dispatch_native(
                binding, native, body, scope=scope, version=version, deadline=deadline, intake=intake
            )
        result = await self._command_result(scope.account_id, binding.operation_key, request)
        if self._client.retained_auth_scope() != scope or marker() != intake:
            result["current_applicability"] = "unknown"
        return result

    async def _dispatch_native(  # noqa: PLR0913 - the physical fence is explicit.
        self,
        binding: NativeWriteReceiptBinding,
        native: object,
        body: bytes,
        *,
        scope: TelegramAuthScope,
        version: str,
        deadline: float,
        intake: int,
    ) -> None:
        import asyncio
        import time

        from ..native_write_fence import FolderWriteRejectedError, NativeWriteCheck, native_write_context
        from ..telegram_rpc_scheduler import TelegramRpcSource, rpc_scope
        from .intake import marker

        async def revalidate() -> Callable[[], bool]:
            accepted = await self._writer.run(
                lambda conn: _preclaim_matches(conn, binding, version, self._client.retained_auth_scope() == scope)
            )
            return lambda: (
                accepted
                and marker() == intake
                and time.monotonic() < deadline
                and self._client.retained_auth_scope() == scope
            )

        check = NativeWriteCheck(binding, body, revalidate)
        submitted = False
        try:
            await self._receipts.arm(binding, scope, version, intake, deadline)
            submitted = True
            with native_write_context(check), rpc_scope(TelegramRpcSource.FOLDER_COMMAND):
                await self._client(native)
        except asyncio.CancelledError:
            if not submitted:
                await self._receipts._write(lambda conn: _mark_not_sent(conn, binding))
            else:
                await self._receipts._write(lambda conn: _mark_unbound_not_sent(conn, binding))
            raise
        except FolderWriteRejectedError:
            await self._writer.run(lambda conn: _mark_not_sent(conn, binding))
        except Exception:  # noqa: BLE001 - the durable receipt determines the outcome.
            pass
        finally:
            self._receipts.disarm(binding)
            try:
                self._offer_verification()
            except Exception:  # noqa: BLE001 - composition reports failure without changing the receipt.
                pass

    async def _command_result(
        self, account_id: int, operation_key: str, request: dict[str, object]
    ) -> dict[str, object]:
        from .command_receipts import find_operation

        row = await self._writer.run(lambda conn: find_operation(conn, account_id, operation_key))
        assert row is not None
        if row[2] in {"acknowledged", "rejected", "unknown"}:
            from ..flood import TelegramRpcThrottled
            from ..telegram_rpc_scheduler import TelegramRpcAdmissionDeferred, TelegramRpcMethodRecoveryPending

            try:
                await self._verify_receipts(account_id, operation_key, force=True)
            except (
                TelegramRpcThrottled,
                TelegramRpcAdmissionDeferred,
                TelegramRpcMethodRecoveryPending,
                TimeoutError,
                OSError,
            ):
                pass
            row = await self._writer.run(lambda conn: find_operation(conn, account_id, operation_key))
            assert row is not None
        return _operation_result(account_id, operation_key, row, request)

    async def _verify_receipts(
        self, account_id: int, operation_key: str | None, *, force: bool, budget: RpcAttemptBudget | None = None
    ) -> bool:
        import time

        from ..telegram_rpc_scheduler import current_rpc_scope

        scope = self._client.retained_auth_scope()
        if scope is None or scope.account_id != account_id:
            return False
        scope_token = self._client.capture_image_scope()
        if scope_token is None:
            return False
        from .intake import marker

        intake = marker()
        now = int(time.time())
        reserved = await self._writer.run(
            lambda conn: _reserve_folder_verification(conn, account_id, operation_key, now, force=force)
        )
        if not reserved:
            return False
        active_budget = current_rpc_scope().attempt_budget
        before_attempts = None if active_budget is None else active_budget.attempts
        try:
            observation = await self._worker.force_refresh(budget)
        finally:
            if active_budget is not None and active_budget.attempts == before_attempts:
                await self._writer.run(lambda conn: _release_folder_verification(conn, account_id, reserved, now))
        observation_order = _accepted_folder_order(observation, scope == self._client.retained_auth_scope())
        if observation_order is None:
            return False
        return await self._writer.run(
            lambda conn: _finish_folder_verification(
                conn,
                account_id,
                reserved,
                observation_order,
                guard=lambda: self._client.claim_image_scope(scope_token) and marker() == intake,
            )
        )

    def verification_status(self, now: float, excluded: frozenset[WorkRevision]) -> DemandStatus | None:
        from ..telegram_demand_progress import WorkKey, WorkRevision
        from ..telegram_rpc_consumers import DemandKind

        scope = self._client.retained_auth_scope()
        if scope is None:
            return None
        rows = cast(
            list[tuple[str, str, int, float | None, float | None]],
            self._reader_conn.execute(
                "SELECT operation_key,operation_fingerprint,verification_attempts,next_verification_at,verification_deadline "
                "FROM folder_command_receipts WHERE account_id=? AND verification='pending' "
                "AND outcome IN ('acknowledged','rejected','unknown') ORDER BY next_verification_at,operation_key",
                (scope.account_id,),
            ).fetchall(),
        )
        for key, fingerprint, attempts, next_at, deadline in rows:
            revision = WorkRevision(
                WorkKey(DemandKind.FOLDER_SNAPSHOT, scope.account_id, f"receipt:{key}"), f"{fingerprint}:{attempts}"
            )
            if revision not in excluded:
                return DemandStatus(release_at=min(float(next_at or now), float(deadline or now)), selected=revision)
        return None

    async def verification_slice(self, budget: RpcAttemptBudget) -> SliceOutcome:
        from ..telegram_demand_progress import SliceOutcome, SliceOutcomeKind

        if budget.selected is None:
            raise ValueError("receipt verification requires a selected budget")
        selected = budget.selected
        progressed = await self._verify_receipts(selected.key.account_id, None, force=False, budget=budget)
        return (
            SliceOutcome(selected, SliceOutcomeKind.PROGRESSED, f"{selected.revision}:observed")
            if progressed
            else SliceOutcome(selected, SliceOutcomeKind.DEFERRED)
        )


def _folder_error(code: str, action: str) -> dict[str, object]:
    return {"ok": False, "error": code, "message": code.replace("_", " "), "required_action": action}


def _operation_result(
    account_id: int, operation_key: str, row: tuple[object, ...], request: dict[str, object]
) -> dict[str, object]:
    outcome = "not_sent" if row[5] else str(row[2])
    dispatched = row[4] is not None and outcome != "not_sent"
    dispatch, acknowledgement = _receipt_wire_state(outcome, dispatched)
    result: dict[str, object] = {
        "ok": True,
        "account_id": account_id,
        "operation_key": operation_key,
        "folder_id": row[1],
        "phase": "preparing" if outcome == "pending" else "completed",
        "outcome": "unknown" if outcome == "pending" else outcome,
        "dispatch": dispatch,
        "acknowledgement": acknowledgement,
        "verification": row[3],
        "next_action": "Inspect this receipt using the same operation key."
        if outcome in {"pending", "unknown"}
        else "Read the folder to inspect the current definition.",
    }
    overrides = _preserved_overrides(request, row[6])
    if overrides is not None:
        result["preserved_overrides"] = overrides
    return result


def _receipt_wire_state(outcome: str, dispatched: bool) -> tuple[str, str]:
    acknowledgement = "yes" if outcome == "acknowledged" else "no" if outcome in {"rejected", "not_sent"} else "unknown"
    if not dispatched:
        return "not_sent", acknowledgement
    return ("unknown" if outcome in {"pending", "unknown"} else "sent"), acknowledgement


def _preserved_overrides(request: dict[str, object], before: object) -> list[int] | None:
    from .sqlite_repository import decode_folder_definition

    clears = cast(list[int], request.get("clear_excluded_dialog_ids") or [])
    if not clears or not isinstance(before, str):
        return None
    rule = decode_folder_definition(before)
    return list(dict.fromkeys(value for value in clears if value in rule.explicit_ids))


def _plan_request(request: dict[str, object], target: int, before: FolderRule | None) -> FolderRule | None:
    return plan_folder_change(
        cast(str, request["action"]),
        target,
        before,
        title=cast(str | None, request.get("title")),
        dialog_ids=tuple(cast(list[int], request.get("dialog_ids") or [])),
        add_dialog_ids=tuple(cast(list[int], request.get("add_dialog_ids") or [])),
        remove_dialog_ids=tuple(cast(list[int], request.get("remove_dialog_ids") or [])),
        clear_excluded_dialog_ids=tuple(cast(list[int], request.get("clear_excluded_dialog_ids") or [])),
    )


def _mark_unchanged(
    conn: sqlite3.Connection, binding: NativeWriteReceiptBinding, version: str, *, guard: Callable[[], bool]
) -> None:
    from ..sync_transactions import write_transaction
    from .command_receipts import _BINDING_WHERE, _binding_values

    with write_transaction(conn):
        unchanged = guard() and folder_catalog_version(conn, binding.account_id) == version
        result = conn.execute(
            f"UPDATE folder_command_receipts SET outcome=?,verification='not_required' WHERE {_BINDING_WHERE} AND outcome='pending' AND attempt_id IS NULL",
            ("unchanged" if unchanged else "not_sent", *_binding_values(binding)),
        )
        if result.rowcount != 1:
            raise ValueError("folder receipt changed")


def _preclaim_matches(conn: sqlite3.Connection, binding: object, version: str, scope_matches: bool) -> bool:
    from ..native_write_fence import NativeWriteReceiptBinding
    from .command_receipts import _BINDING_WHERE, _binding_values

    if not isinstance(binding, NativeWriteReceiptBinding) or not scope_matches:
        return False
    if folder_catalog_version(conn, binding.account_id) != version:
        return False
    row = cast(
        tuple[object, ...] | None,
        conn.execute(
            f"SELECT 1 FROM folder_command_receipts WHERE {_BINDING_WHERE} AND outcome='pending' AND expected_version=? AND permit_issued=1",
            (*_binding_values(binding), version),
        ).fetchone(),
    )
    return row is not None


def _mark_not_sent(conn: sqlite3.Connection, binding: object) -> None:
    from ..sync_transactions import write_transaction
    from .command_receipts import _BINDING_WHERE, _binding_values

    with write_transaction(conn):
        result = conn.execute(
            f"UPDATE folder_command_receipts SET outcome=CASE WHEN outcome='pending' THEN 'not_sent' ELSE outcome END, verification='not_required',preclaim_rejected=1 WHERE {_BINDING_WHERE} AND outcome IN ('pending','rejected')",
            _binding_values(binding),
        )
        if result.rowcount != 1:
            raise ValueError("folder receipt changed")


def _reserve_folder_verification(
    conn: sqlite3.Connection, account_id: int, operation_key: str | None, now: int, *, force: bool
) -> list[tuple[str, int, int, str, str | None, int]]:
    from ..sync_transactions import write_transaction

    with write_transaction(conn):
        conn.execute(
            "UPDATE folder_command_receipts SET verification='exhausted' WHERE account_id=? AND verification='pending' AND (verification_attempts>=3 OR verification_deadline<=?)",
            (account_id, now),
        )
        rows = cast(
            list[tuple[str, int, int, str, str | None, int]],
            conn.execute(
                "SELECT operation_key,target,dispatch_order,action,intended_definition,verification_attempts FROM folder_command_receipts WHERE account_id=? AND verification='pending' AND outcome IN ('acknowledged','rejected','unknown') AND verification_attempts<3 AND verification_deadline>? AND (? IS NULL OR operation_key=?) AND (? OR next_verification_at<=?) ORDER BY next_verification_at,operation_key",
                (account_id, now, operation_key, operation_key, force, now),
            ).fetchall(),
        )
        for key, *_rest in rows:
            conn.execute(
                "UPDATE folder_command_receipts SET verification_attempts=verification_attempts+1,next_verification_at=?+CASE WHEN verification_attempts=0 THEN 5 ELSE 30 END WHERE account_id=? AND operation_key=?",
                (now, account_id, key),
            )
        return rows


def _finish_folder_verification(
    conn: sqlite3.Connection,
    account_id: int,
    receipts: list[tuple[str, int, int, str, str | None, int]],
    observation_order: int,
    *,
    guard: Callable[[], bool],
) -> bool:
    from ..sync_transactions import write_transaction
    from .sqlite_repository import encode_folder_definition, read_folder_rules

    with write_transaction(conn):
        if not guard() or folder_catalog_version(conn, account_id) is None:
            return False
        rules = read_folder_rules(conn)
        changed = False
        for operation_key, target, dispatch_order, action, intended, _ in receipts:
            if observation_order <= dispatch_order:
                continue
            rule = _rule_for_id(rules, target)
            current = None if rule is None else encode_folder_definition(rule)
            matched = current == intended if action != "delete" else rule is None
            result = conn.execute(
                "UPDATE folder_command_receipts SET verification=? WHERE account_id=? AND operation_key=? AND verification='pending'",
                ("matched" if matched else "mismatched", account_id, operation_key),
            )
            changed = changed or result.rowcount == 1
        return changed


def _prepare_native_intent(
    request: dict[str, object],
    scope: TelegramAuthScope,
    rules: tuple[FolderRule, ...],
    resolve: Callable[[int], object | None],
) -> tuple[NativeWriteReceiptBinding, object, bytes, str | None, str | None]:
    from ..native_write_fence import NativeWriteReceiptBinding
    from .sqlite_repository import encode_folder_definition

    action = cast(str, request["action"])
    target = _new_folder_id(rules) if action == "create" else cast(int, request["folder_id"])
    before_rule = _rule_for_id(rules, target)
    intended_rule = _plan_request(request, target, before_rule)
    native = native_folder_request(action, target, intended_rule, resolve)
    before = None if before_rule is None else encode_folder_definition(before_rule)
    intended = None if intended_rule is None else encode_folder_definition(intended_rule)
    binding = NativeWriteReceiptBinding(
        "folder",
        scope.account_id,
        scope.dc_id,
        scope.auth_key_id,
        cast(str, request["operation_key"]),
        action,
        target,
        folder_intent_fingerprint(action, target, before, intended),
        1,
    )
    return binding, native, bytes(cast(SupportsBytes, native)), before, intended


def _release_folder_verification(
    conn: sqlite3.Connection,
    account_id: int,
    receipts: list[tuple[str, int, int, str, str | None, int]],
    now: int,
) -> None:
    from ..sync_transactions import write_transaction

    with write_transaction(conn):
        for key, _, _, _, _, previous_attempts in receipts:
            conn.execute(
                "UPDATE folder_command_receipts SET verification_attempts=verification_attempts-1,next_verification_at=? WHERE account_id=? AND operation_key=? AND verification='pending' AND verification_attempts=?",
                (now + 5, account_id, key, previous_attempts + 1),
            )


def _accepted_folder_order(observation: object, same_scope: bool) -> int | None:
    from .refresh import FolderRefreshResult

    if not same_scope or not isinstance(observation, FolderRefreshResult) or not observation.accepted:
        return None
    return observation.observation_order


def _issue_folder_permit(conn: sqlite3.Connection, binding: NativeWriteReceiptBinding, version: str) -> bool:
    from ..sync_transactions import write_transaction
    from .command_receipts import _BINDING_WHERE, _binding_values

    with write_transaction(conn):
        if folder_catalog_version(conn, binding.account_id) != version:
            return False
        result = conn.execute(
            f"UPDATE folder_command_receipts SET permit_issued=1 WHERE {_BINDING_WHERE} AND expected_version=? AND outcome='pending' AND permit_issued=0",
            (*_binding_values(binding), version),
        )
        return result.rowcount == 1


def _mark_unbound_not_sent(conn: sqlite3.Connection, binding: NativeWriteReceiptBinding) -> None:
    from ..sync_transactions import write_transaction
    from .command_receipts import _BINDING_WHERE, _binding_values

    with write_transaction(conn):
        conn.execute(
            f"UPDATE folder_command_receipts SET outcome='not_sent',verification='not_required',preclaim_rejected=1 WHERE {_BINDING_WHERE} AND outcome='pending' AND attempt_id IS NULL",
            _binding_values(binding),
        )
