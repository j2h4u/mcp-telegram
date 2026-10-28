"""Bounded, private TL snapshots and pure native draft request construction."""

from __future__ import annotations

from copy import copy
from hashlib import sha256
from typing import cast, get_args

from telethon.extensions import BinaryReader  # type: ignore[import-untyped]
from telethon.tl import types  # type: ignore[import-untyped]
from telethon.tl.functions.messages import SaveDraftRequest  # type: ignore[import-untyped]
from telethon.tl.tlobject import TLObject  # type: ignore[import-untyped]
from telethon.utils import get_peer_id  # type: ignore[import-untyped]

from mcp_telegram.drafts.contracts import (
    MAX_DRAFT_ENTITY_COUNT,
    MAX_DRAFT_TEXT_LENGTH,
    DraftScope,
    RestoreSnapshot,
)

MAX_RESTORE_SNAPSHOT_BYTES = 1_048_576
_SNAPSHOT_PREFIX = b"TGDR\x01"
_MAX_RICH_NODES = 4096
_REPLAY_FIELDS = frozenset(
    {
        "message",
        "date",
        "no_webpage",
        "invert_media",
        "reply_to",
        "entities",
        "media",
        "effect",
        "suggested_post",
        "rich_message",
    }
)
# shortcut: rich file/peer dependencies are not archived, add their acquisition owner before permitting restoration.
_RICH_VALUE_TYPES = (
    types.PageBlockParagraph,
    types.PageBlockTitle,
    types.PageBlockSubtitle,
    types.PageBlockHeader,
    types.PageBlockSubheader,
    types.PageBlockFooter,
    types.PageBlockKicker,
    types.PageBlockBlockquote,
    types.PageBlockPullquote,
    types.PageBlockPreformatted,
    types.PageBlockDivider,
    types.PageBlockAnchor,
    types.PageBlockList,
    types.PageBlockOrderedList,
    types.PageBlockTable,
    types.PageBlockDetails,
    types.PageBlockCover,
    types.PageBlockBlockquoteBlocks,
    types.PageBlockHeading1,
    types.PageBlockHeading2,
    types.PageBlockHeading3,
    types.PageBlockHeading4,
    types.PageBlockHeading5,
    types.PageBlockHeading6,
    types.PageBlockMath,
    types.PageBlockThinking,
    types.PageListItemText,
    types.PageListItemBlocks,
    types.PageListOrderedItemText,
    types.PageListOrderedItemBlocks,
    types.PageTableCell,
    types.PageTableRow,
    types.PageCaption,
    types.TextEmpty,
    types.TextPlain,
    types.TextBold,
    types.TextItalic,
    types.TextUnderline,
    types.TextStrike,
    types.TextFixed,
    types.TextEmail,
    types.TextUrl,
    types.TextPhone,
    types.TextConcat,
    types.TextSubscript,
    types.TextSuperscript,
    types.TextMarked,
    types.TextSpoiler,
    types.TextAnchor,
    types.TextAutoEmail,
    types.TextAutoPhone,
    types.TextAutoUrl,
    types.TextBankCard,
    types.TextBotCommand,
    types.TextCashtag,
    types.TextHashtag,
    types.TextMath,
    types.TextDate,
    types.TextMention,
)


class DraftSnapshotError(ValueError):
    """Content-free reason why a retained composition cannot be used."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _fingerprint(draft: types.DraftMessage | types.DraftMessageEmpty) -> str:
    composition = copy(draft)
    composition.date = None
    return sha256(bytes(composition)).hexdigest()


def capture_restore_snapshot(draft: object) -> RestoreSnapshot | None:
    """Capture complete provider data without changing the ordinary read projection."""
    if not isinstance(draft, (types.DraftMessage, types.DraftMessageEmpty)):
        return None
    try:
        payload = _SNAPSHOT_PREFIX + bytes(draft)
        if len(payload) > MAX_RESTORE_SNAPSHOT_BYTES:
            return None
        return RestoreSnapshot(payload, _fingerprint(draft), _restore_reason(draft))
    except Exception:  # noqa: BLE001 - vendor serialization may embed private payloads in exceptions.
        return None


def decode_restore_snapshot(snapshot: RestoreSnapshot) -> types.DraftMessage | types.DraftMessageEmpty:
    """Decode the one supported envelope and validate its exact content fingerprint."""
    try:
        payload = snapshot.payload
        if len(payload) > MAX_RESTORE_SNAPSHOT_BYTES or not payload.startswith(_SNAPSHOT_PREFIX):
            raise ValueError("invalid envelope")
        raw = payload[len(_SNAPSHOT_PREFIX) :]
        with BinaryReader(raw) as reader:
            draft = reader.tgread_object()
            if not isinstance(draft, (types.DraftMessage, types.DraftMessageEmpty)) or reader.tell_position() != len(
                raw
            ):
                raise ValueError("invalid draft")
        if _fingerprint(draft) != snapshot.semantic_fingerprint:
            raise ValueError("invalid fingerprint")
        return draft
    except Exception:  # noqa: BLE001 - do not leak retained text or capabilities through parser errors.
        raise DraftSnapshotError("invalid_restore_snapshot") from None


def _rich_restore_reason(rich: object) -> str | None:
    if not isinstance(rich, types.RichMessage) or rich.part:
        return "partial_rich_message"
    if rich.photos or rich.documents:
        return "media_dependencies_not_retained"
    pending: list[object] = list(rich.blocks)
    visited = 0
    while pending:
        value = pending.pop()
        visited += 1
        if visited > _MAX_RICH_NODES:
            return "rich_message_too_complex"
        if isinstance(value, list):
            pending.extend(value)
        elif isinstance(value, TLObject):
            if not isinstance(value, _RICH_VALUE_TYPES):
                return "unsupported_rich_dependency"
            pending.extend(vars(value).values())
    return None


def _restore_reason(draft: types.DraftMessage | types.DraftMessageEmpty) -> str | None:
    if isinstance(draft, types.DraftMessageEmpty):
        return None
    return _composition_restore_reason(draft)


def _text_composition_restore_reason(draft: types.DraftMessage) -> str | None:
    if len(draft.message) > MAX_DRAFT_TEXT_LENGTH or len(draft.entities or ()) > MAX_DRAFT_ENTITY_COUNT:
        return "draft_composition_too_large"
    if any(not isinstance(entity, get_args(types.TypeMessageEntity)) for entity in draft.entities or ()):
        return "unsupported_draft_entity"
    return None


def _composition_restore_reason(draft: types.DraftMessage) -> str | None:
    if set(vars(draft)) - _REPLAY_FIELDS:
        return "unsupported_draft_field"
    text_reason = _text_composition_restore_reason(draft)
    if text_reason is not None:
        return text_reason
    if draft.reply_to is not None and (
        not isinstance(draft.reply_to, types.InputReplyToMessage) or draft.reply_to.monoforum_peer_id is not None
    ):
        return "unsupported_reply_context"
    if draft.media is not None and not isinstance(draft.media, (types.InputMediaEmpty, types.InputMediaWebPage)):
        return "media_dependencies_not_retained"
    return _rich_restore_reason(draft.rich_message) if draft.rich_message is not None else None


def _validate_target(peer: object, scope: DraftScope) -> None:
    if scope.subdialog_peer_id is not None:
        raise DraftSnapshotError("unsupported_draft_scope")
    if scope.top_message_id is not None and scope.top_message_id <= 0:
        raise DraftSnapshotError("invalid_draft_topic")
    if isinstance(peer, types.InputPeerSelf):
        peer_id = scope.account_id
    elif isinstance(peer, (types.InputPeerUser, types.InputPeerChat, types.InputPeerChannel)):
        peer_id = get_peer_id(peer)
    else:
        raise DraftSnapshotError("unresolved_draft_peer")
    if peer_id != scope.dialog_id:
        raise DraftSnapshotError("draft_peer_mismatch")


def _scoped_reply(scope: DraftScope, reply: types.InputReplyToMessage | None) -> types.InputReplyToMessage | None:
    if reply is not None:
        if reply.top_msg_id is not None and reply.top_msg_id != scope.top_message_id:
            raise DraftSnapshotError("draft_topic_mismatch")
        reply = copy(reply)
        reply.top_msg_id = scope.top_message_id
        return reply
    if scope.top_message_id is not None:
        return types.InputReplyToMessage(reply_to_msg_id=0, top_msg_id=scope.top_message_id)
    return None


def build_clear_draft_request(peer: object, scope: DraftScope) -> SaveDraftRequest:
    """Clear only the addressed composer, retaining the protocol topic selector."""
    _validate_target(peer, scope)
    return SaveDraftRequest(peer=cast(types.TypeInputPeer, peer), message="", reply_to=_scoped_reply(scope, None))


def _save_request(peer: object, scope: DraftScope, draft: types.DraftMessage) -> SaveDraftRequest:
    reason = _restore_reason(draft)
    if reason is not None:
        raise DraftSnapshotError(reason)
    rich = draft.rich_message
    return SaveDraftRequest(
        peer=cast(types.TypeInputPeer, peer),
        message=draft.message,
        no_webpage=draft.no_webpage,
        invert_media=draft.invert_media,
        reply_to=_scoped_reply(scope, cast(types.InputReplyToMessage | None, draft.reply_to)),
        entities=draft.entities,
        media=draft.media,
        effect=draft.effect,
        suggested_post=draft.suggested_post,
        rich_message=None if rich is None else types.InputRichMessage(blocks=rich.blocks, rtl=rich.rtl),
    )


def build_save_draft_request(peer: object, scope: DraftScope, snapshot: RestoreSnapshot) -> SaveDraftRequest:
    """Replay a complete supported composition, never a lossy projected draft."""
    _validate_target(peer, scope)
    draft = decode_restore_snapshot(snapshot)
    if snapshot.restore_reason is not None:
        raise DraftSnapshotError(snapshot.restore_reason)
    if isinstance(draft, types.DraftMessageEmpty):
        return build_clear_draft_request(peer, scope)
    return _save_request(peer, scope, draft)


def _replace_draft_text(draft: types.DraftMessage, text: str, *, drop_formatting: bool) -> None:
    if text != draft.message:
        if draft.rich_message is not None:
            raise DraftSnapshotError("rich_text_update_unsupported")
        if draft.entities and not drop_formatting:
            raise DraftSnapshotError("draft_formatting_conflict")
        draft.entities = None
    draft.message = text


def build_text_draft_request(
    peer: object,
    scope: DraftScope,
    text: str,
    *,
    existing: RestoreSnapshot | None = None,
    drop_formatting: bool = False,
) -> SaveDraftRequest:
    """Create or replace plain text while preserving the remaining composition."""
    _validate_target(peer, scope)
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_DRAFT_TEXT_LENGTH:
        raise DraftSnapshotError("invalid_draft_text")
    draft = decode_restore_snapshot(existing) if existing is not None else types.DraftMessage(text, date=None)
    if existing is not None and existing.restore_reason is not None:
        raise DraftSnapshotError(existing.restore_reason)
    if isinstance(draft, types.DraftMessageEmpty):
        draft = types.DraftMessage(text, date=None)
    _replace_draft_text(draft, text, drop_formatting=drop_formatting)
    return _save_request(peer, scope, draft)
