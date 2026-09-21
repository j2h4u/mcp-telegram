"""Telethon boundary for normalized account draft observations.

The adapter never resolves peers or performs enrichment.  Raw TL objects are
discarded at this boundary after their bounded, supported primitives have been
copied into :mod:`mcp_telegram.drafts.contracts`.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Protocol, cast

from telethon.tl import types  # type: ignore[import-untyped]
from telethon.tl.functions.messages import GetAllDraftsRequest  # type: ignore[import-untyped]
from telethon.utils import get_peer_id  # type: ignore[import-untyped]

from mcp_telegram.drafts.contracts import (
    MAX_DRAFT_ENTITY_COUNT,
    MAX_REFERENCE_KIND_LENGTH,
    CompositionCompleteness,
    DraftComposition,
    DraftDisposition,
    DraftEntity,
    DraftObservation,
    DraftObservationSource,
    DraftReference,
    DraftScope,
    SnapshotCoverage,
)
from mcp_telegram.drafts.ports import DraftSnapshotGateway
from mcp_telegram.telegram_demand import AcquisitionKind, acquisition_context
from mcp_telegram.telegram_rpc_consumers import TelegramRpcSource
from mcp_telegram.telegram_rpc_scheduler import rpc_scope


class _DraftClient(Protocol):
    async def __call__(self, request: object) -> object: ...


def _peer_id(peer: object | None) -> int | None:
    """Return a canonical peer id only when it is already present in the update."""
    if peer is None:
        return None
    try:
        value = get_peer_id(cast(types.TypePeer, peer))
    except TypeError, ValueError:
        return None
    return int(value) if isinstance(value, int) and value != 0 else None


def _positive_int(value: object | None) -> int | None:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) and value != 0 else None


def _bounded_kind(value: object) -> str:
    return type(value).__name__[:MAX_REFERENCE_KIND_LENGTH]


def _entity(raw: object) -> tuple[DraftEntity | None, bool]:
    """Normalize one entity without retaining URL or arbitrary raw fields."""
    offset = getattr(raw, "offset", None)
    length = getattr(raw, "length", None)
    if not isinstance(offset, int) or not isinstance(length, int) or offset < 0 or length < 0:
        return None, True
    identifier = _positive_int(getattr(raw, "user_id", None))
    if identifier is None:
        identifier = _positive_int(getattr(raw, "document_id", None))
    language = getattr(raw, "language", None)
    if not isinstance(language, str) or len(language) > MAX_REFERENCE_KIND_LENGTH:
        language = None
    # Text-url and similar entities contain opaque/string targets.  Their
    # visible range survives, but its destination is intentionally not copied.
    partial = hasattr(raw, "url") or (hasattr(raw, "document_id") and identifier is None)
    return DraftEntity(_bounded_kind(raw), offset, length, identifier, language), partial


def _reference(raw: object | None, *, fallback_kind: str) -> tuple[DraftReference | None, bool]:
    if raw is None:
        return None, False
    identifier = _positive_int(getattr(raw, "id", None))
    nested_id = getattr(raw, "id", None)
    if identifier is None:
        identifier = _positive_int(getattr(nested_id, "id", None))
    if identifier is None:
        identifier = _positive_int(getattr(raw, "story_id", None))
    if identifier is None:
        identifier = _positive_int(getattr(raw, "document_id", None))
    if identifier is None:
        document = getattr(raw, "document", None)
        identifier = _positive_int(getattr(document, "id", None))
    message_id = _positive_int(getattr(raw, "reply_to_msg_id", None))
    if message_id is None:
        message_id = _positive_int(getattr(raw, "top_msg_id", None))
    peer_id = _peer_id(getattr(raw, "reply_to_peer_id", None))
    if peer_id is None:
        peer_id = _peer_id(getattr(raw, "peer", None))
    reference = DraftReference(
        _bounded_kind(raw) if raw is not None else fallback_kind, identifier, peer_id, message_id
    )
    # Quote text, URLs and other nested data are not bounded primitives.  Keep
    # the structural context and flag that the whole construct was not copied.
    partial = any(hasattr(raw, field) for field in ("quote_text", "url", "entities"))
    return reference, partial


def _reply_context(
    raw: object | None,
) -> tuple[DraftReference | None, DraftReference | None, DraftReference | None, DraftReference | None, bool]:
    """Project reply, story, mono-forum, and quote contexts from one TL union."""
    if raw is None:
        return None, None, None, None, False
    peer_id = _peer_id(getattr(raw, "reply_to_peer_id", None)) or _peer_id(getattr(raw, "peer", None))
    reply_id = _positive_int(getattr(raw, "reply_to_msg_id", None))
    story_id = _positive_int(getattr(raw, "story_id", None))
    monoforum_peer_id = _peer_id(getattr(raw, "monoforum_peer_id", None))
    quote_offset = _positive_int(getattr(raw, "quote_offset", None))
    reply = DraftReference("reply", peer_id=peer_id, message_id=reply_id) if reply_id is not None else None
    story = DraftReference("story", peer_id=peer_id, identifier=story_id) if story_id is not None else None
    monoforum = DraftReference("monoforum", peer_id=monoforum_peer_id) if monoforum_peer_id is not None else None
    has_quote = any(getattr(raw, field, None) is not None for field in ("quote_text", "quote_entities", "quote_offset"))
    quote = (
        DraftReference("quote", peer_id=peer_id, message_id=reply_id, identifier=quote_offset) if has_quote else None
    )
    partial = any(
        getattr(raw, field, None) is not None
        for field in ("quote_text", "quote_entities", "todo_item_id", "poll_option")
    )
    return reply, story, quote, monoforum, partial


def _entities(draft: types.DraftMessage) -> tuple[tuple[DraftEntity, ...], bool]:
    """Normalize bounded entity ranges and record omitted detail."""
    entities: list[DraftEntity] = []
    partial = False
    raw_entities = getattr(draft, "entities", None)
    if raw_entities is not None:
        if not isinstance(raw_entities, Sequence) or isinstance(raw_entities, (str, bytes)):
            partial = True
        else:
            for raw_entity in raw_entities[:MAX_DRAFT_ENTITY_COUNT]:
                entity, entity_partial = _entity(raw_entity)
                partial = partial or entity_partial
                if entity is not None:
                    entities.append(entity)
                else:
                    partial = True
            if len(raw_entities) > MAX_DRAFT_ENTITY_COUNT:
                partial = True
    return tuple(entities), partial


def _composition_references(
    draft: types.DraftMessage,
) -> tuple[
    DraftReference | None,
    DraftReference | None,
    DraftReference | None,
    DraftReference | None,
    DraftReference | None,
    DraftReference | None,
    DraftReference | None,
    bool,
]:
    """Normalize all supported non-text composition references."""

    reply, story, quote, monoforum, reply_partial = _reply_context(getattr(draft, "reply_to", None))
    media, media_partial = _reference(getattr(draft, "media", None), fallback_kind="media")
    rich, rich_partial = _reference(getattr(draft, "rich_message", None), fallback_kind="rich")
    suggested, suggested_partial = _reference(getattr(draft, "suggested_post", None), fallback_kind="suggested_post")
    return (
        reply,
        story,
        quote,
        monoforum,
        media,
        rich,
        suggested,
        any((reply_partial, media_partial, rich_partial, suggested_partial)),
    )


def _draft_date(draft: types.DraftMessage) -> datetime | None:
    date = getattr(draft, "date", None)
    if not isinstance(date, datetime):
        return None
    return date.replace(tzinfo=UTC) if date.tzinfo is None else date


def _composition(draft: object) -> DraftComposition | None:
    if not isinstance(draft, types.DraftMessage):
        return None
    text = getattr(draft, "message", None)
    if not isinstance(text, str):
        return None
    entities, entity_partial = _entities(draft)
    reply, story, quote, monoforum, media, rich, suggested, reference_partial = _composition_references(draft)
    return DraftComposition(
        text=text,
        date=_draft_date(draft),
        entities=entities,
        reply=reply,
        story=story,
        quote=quote,
        monoforum=monoforum,
        media=media,
        rich=rich,
        no_webpage=getattr(draft, "no_webpage", None),
        invert_media=getattr(draft, "invert_media", None),
        effect_id=_positive_int(getattr(draft, "effect", None)),
        suggested_post=suggested,
        completeness=CompositionCompleteness.PARTIAL
        if entity_partial or reference_partial
        else CompositionCompleteness.COMPLETE,
    )


def normalize_update_draft(
    update: object,
    *,
    account_id: int,
    source: DraftObservationSource,
    observed_at: datetime,
) -> DraftObservation | None:
    """Normalize exactly one ``UpdateDraftMessage`` without network access."""
    if not isinstance(update, types.UpdateDraftMessage):
        return None
    dialog_id = _peer_id(update.peer)
    if dialog_id is None:
        return None
    scope = DraftScope(
        account_id=account_id,
        dialog_id=dialog_id,
        top_message_id=_positive_int(update.top_msg_id),
        subdialog_peer_id=_peer_id(update.saved_peer_id),
    )
    composition = _composition(update.draft)
    if composition is not None:
        return DraftObservation(
            scope,
            DraftDisposition.PRESENT,
            source,
            observed_at,
            composition,
            ambiguity=composition.date is None,
        )
    if isinstance(update.draft, types.DraftMessageEmpty):
        return DraftObservation(scope, DraftDisposition.TOMBSTONE, source, observed_at, ambiguity=True)
    return None


class TelethonDraftSnapshotGateway(DraftSnapshotGateway):
    """Issue the one unpaged draft snapshot request under the draft root scope."""

    def __init__(self, client: object, account_id: int) -> None:
        self._client = cast(_DraftClient, client)
        self._account_id = account_id

    async def fetch_all_drafts(self) -> tuple[SnapshotCoverage, tuple[DraftObservation, ...]]:
        observed_at = datetime.now(UTC)
        with rpc_scope(TelegramRpcSource.DRAFT_SNAPSHOT, acquisition_kind=AcquisitionKind.DRAFT_SNAPSHOT):
            with acquisition_context(AcquisitionKind.DRAFT_SNAPSHOT):
                result = await self._client(GetAllDraftsRequest())
        raw_updates = getattr(result, "updates", None)
        too_long = isinstance(result, types.UpdatesTooLong)
        if not isinstance(raw_updates, Sequence) or isinstance(raw_updates, (str, bytes)):
            return SnapshotCoverage(self._account_id, False, 0, too_long), ()
        observations: list[DraftObservation] = []
        complete = not too_long
        for update in raw_updates:
            observation = normalize_update_draft(
                update,
                account_id=self._account_id,
                source=DraftObservationSource.SNAPSHOT,
                observed_at=observed_at,
            )
            if isinstance(update, types.UpdateDraftMessage) and observation is None:
                complete = False
            if observation is not None:
                observations.append(observation)
        return SnapshotCoverage(self._account_id, complete, len(raw_updates), too_long), tuple(observations)


__all__ = ["TelethonDraftSnapshotGateway", "normalize_update_draft"]
