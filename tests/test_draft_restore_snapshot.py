from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from telethon.tl import types  # type: ignore[import-untyped]

from mcp_telegram.drafts.contracts import DraftObservationSource, DraftScope, RestoreSnapshot
from mcp_telegram.drafts.snapshot import (
    MAX_RESTORE_SNAPSHOT_BYTES,
    DraftSnapshotError,
    build_clear_draft_request,
    build_save_draft_request,
    build_text_draft_request,
    capture_restore_snapshot,
    decode_restore_snapshot,
)
from mcp_telegram.drafts.telethon_adapter import normalize_update_draft

_DATE = datetime(2026, 1, 1, tzinfo=UTC)
_PEER = types.InputPeerUser(91, 123)
_SCOPE = DraftScope(42, 91)


def _snapshot(draft: object) -> RestoreSnapshot:
    snapshot = capture_restore_snapshot(draft)
    assert snapshot is not None
    return snapshot


def test_snapshot_roundtrip_preserves_fields_hidden_by_read_projection() -> None:
    draft = types.DraftMessage(
        "link",
        _DATE,
        entities=[types.MessageEntityTextUrl(0, 4, "https://private.invalid/original")],
        reply_to=types.InputReplyToMessage(
            5, quote_text="quoted", quote_offset=0, quote_entities=[types.MessageEntityBold(0, 6)]
        ),
        media=types.InputMediaWebPage("https://private.invalid/preview", force_large_media=True),
        no_webpage=False,
        invert_media=True,
        effect=123,
        suggested_post=types.SuggestedPost(schedule_date=_DATE),
    )
    snapshot = _snapshot(draft)
    restored = decode_restore_snapshot(snapshot)
    request = build_save_draft_request(_PEER, _SCOPE, snapshot)

    assert bytes(restored) == bytes(draft)
    assert snapshot.restore_reason is None
    assert "private.invalid" not in repr(snapshot)
    assert request.entities is not None
    assert isinstance(request.entities[0], types.MessageEntityTextUrl)
    assert request.entities[0].url == "https://private.invalid/original"
    assert isinstance(request.reply_to, types.InputReplyToMessage)
    assert request.reply_to.quote_text == "quoted"
    assert request.reply_to.quote_offset == 0
    assert isinstance(request.media, types.InputMediaWebPage)
    assert request.media.force_large_media is True
    assert request.effect == 123
    assert request.invert_media is True
    assert request.suggested_post is not None
    assert request.suggested_post.schedule_date == _DATE


def test_fingerprint_ignores_root_date_but_not_hidden_composition_changes() -> None:
    draft = types.DraftMessage("link", _DATE, entities=[types.MessageEntityTextUrl(0, 4, "https://one.invalid")])
    first = _snapshot(draft)
    draft.date = _DATE + timedelta(seconds=1)
    later = _snapshot(draft)
    assert first.payload != later.payload
    assert first.semantic_fingerprint == later.semantic_fingerprint
    assert draft.entities is not None
    assert isinstance(draft.entities[0], types.MessageEntityTextUrl)
    draft.entities[0].url = "https://two.invalid"
    assert _snapshot(draft).semantic_fingerprint != first.semantic_fingerprint


@pytest.mark.parametrize("topic_id", [1, 730])
def test_topic_create_and_clear_keep_exact_topic_selector(topic_id: int) -> None:
    scope = DraftScope(42, -1000000000091, topic_id)
    peer = types.InputPeerChannel(91, 123)
    created = build_text_draft_request(peer, scope, "draft")
    cleared = build_clear_draft_request(peer, scope)
    for request in (created, cleared):
        assert request.peer is peer
        assert isinstance(request.reply_to, types.InputReplyToMessage)
        assert request.reply_to.reply_to_msg_id == 0
        assert request.reply_to.top_msg_id == topic_id
    assert cleared.message == ""
    assert cleared.media is None
    assert cleared.entities is None


def test_clear_and_restore_absence_are_not_reply_only_drafts() -> None:
    request = build_save_draft_request(_PEER, _SCOPE, _snapshot(types.DraftMessageEmpty(_DATE)))
    assert request.message == ""
    assert request.reply_to is None
    assert (
        _snapshot(types.DraftMessageEmpty()).semantic_fingerprint
        == _snapshot(types.DraftMessageEmpty(_DATE)).semantic_fingerprint
    )


def test_update_requires_explicit_formatting_drop_and_retains_reply_and_preview() -> None:
    draft = types.DraftMessage(
        "old",
        _DATE,
        entities=[types.MessageEntityBold(0, 3)],
        reply_to=types.InputReplyToMessage(8, quote_text="quote"),
        media=types.InputMediaWebPage("https://private.invalid"),
    )
    snapshot = _snapshot(draft)
    with pytest.raises(DraftSnapshotError, match="draft_formatting_conflict"):
        build_text_draft_request(_PEER, _SCOPE, "new text", existing=snapshot)
    request = build_text_draft_request(_PEER, _SCOPE, "new text", existing=snapshot, drop_formatting=True)
    assert request.entities is None
    assert isinstance(request.reply_to, types.InputReplyToMessage)
    assert request.reply_to.quote_text == "quote"
    assert isinstance(request.media, types.InputMediaWebPage)
    assert request.media.url == "https://private.invalid"
    retained = decode_restore_snapshot(snapshot)
    assert isinstance(retained, types.DraftMessage)
    assert retained.message == "old"


def test_plain_rich_snapshot_replays_without_downconverting_to_text() -> None:
    rich = types.RichMessage([types.PageBlockParagraph(types.TextBold(types.TextPlain("rich")))], [], [], rtl=True)
    snapshot = _snapshot(types.DraftMessage("", _DATE, rich_message=rich))
    request = build_save_draft_request(_PEER, _SCOPE, snapshot)
    assert isinstance(request.rich_message, types.InputRichMessage)
    assert request.rich_message.rtl is True
    assert bytes(request.rich_message.blocks[0]) == bytes(rich.blocks[0])
    with pytest.raises(DraftSnapshotError, match="rich_text_update_unsupported"):
        build_text_draft_request(_PEER, _SCOPE, "replacement", existing=snapshot, drop_formatting=True)


@pytest.mark.parametrize(
    ("draft", "reason"),
    [
        (
            types.DraftMessage("", _DATE, media=types.InputMediaDocument(types.InputDocument(1, 2, b"ref"))),
            "media_dependencies_not_retained",
        ),
        (types.DraftMessage("", _DATE, rich_message=types.RichMessage([], [], [], part=True)), "partial_rich_message"),
        (
            types.DraftMessage("", _DATE, rich_message=types.RichMessage([types.PageBlockUnsupported()], [], [])),
            "unsupported_rich_dependency",
        ),
        (
            types.DraftMessage("reply", _DATE, reply_to=types.InputReplyToEphemeralMessage(1)),
            "unsupported_reply_context",
        ),
    ],
)
def test_unrestorable_composition_is_retained_but_not_replayed(draft: types.DraftMessage, reason: str) -> None:
    snapshot = _snapshot(draft)
    assert snapshot.restore_reason == reason
    assert bytes(decode_restore_snapshot(snapshot)) == bytes(draft)
    with pytest.raises(DraftSnapshotError, match=reason):
        build_save_draft_request(_PEER, _SCOPE, snapshot)
    with pytest.raises(DraftSnapshotError, match=reason):
        build_text_draft_request(_PEER, _SCOPE, "new", existing=snapshot)


@pytest.mark.parametrize("mutation", ["version", "truncated", "trailing", "wrong_root", "fingerprint", "oversized"])
def test_invalid_snapshot_is_rejected_without_payload_in_error(mutation: str) -> None:
    snapshot = _snapshot(types.DraftMessage("private content", _DATE))
    if mutation == "version":
        snapshot = replace(snapshot, payload=b"TGDR\x02" + snapshot.payload[5:])
    elif mutation == "truncated":
        snapshot = replace(snapshot, payload=snapshot.payload[:-1])
    elif mutation == "trailing":
        snapshot = replace(snapshot, payload=snapshot.payload + b"private content")
    elif mutation == "wrong_root":
        snapshot = replace(snapshot, payload=snapshot.payload[:5] + bytes(types.PeerUser(91)))
    elif mutation == "fingerprint":
        snapshot = replace(snapshot, semantic_fingerprint="wrong")
    else:
        snapshot = replace(snapshot, payload=b"x" * (MAX_RESTORE_SNAPSHOT_BYTES + 1))
    with pytest.raises(DraftSnapshotError, match="^invalid_restore_snapshot$"):
        decode_restore_snapshot(snapshot)


def test_snapshot_limit_or_serialization_failure_does_not_invent_partial_backup() -> None:
    draft = types.DraftMessage("x", _DATE, media=types.InputMediaWebPage("x" * MAX_RESTORE_SNAPSHOT_BYTES))
    assert capture_restore_snapshot(draft) is None
    assert capture_restore_snapshot(object()) is None


def test_existing_update_ingestion_attaches_lossless_snapshot() -> None:
    update = types.UpdateDraftMessage(
        types.PeerUser(91),
        types.DraftMessage("link", _DATE, entities=[types.MessageEntityTextUrl(0, 4, "https://private.invalid")]),
    )
    observation = normalize_update_draft(
        update, account_id=42, source=DraftObservationSource.REALTIME, observed_at=_DATE
    )
    assert observation is not None
    assert observation.restore_snapshot is not None
    retained = decode_restore_snapshot(observation.restore_snapshot)
    assert isinstance(retained, types.DraftMessage)
    assert retained.entities is not None
    assert isinstance(retained.entities[0], types.MessageEntityTextUrl)
    assert retained.entities[0].url == "https://private.invalid"


def test_request_rejects_different_peer_or_topic_and_saved_subdialog() -> None:
    with pytest.raises(DraftSnapshotError, match="draft_peer_mismatch"):
        build_clear_draft_request(_PEER, DraftScope(42, 92))
    with pytest.raises(DraftSnapshotError, match="unsupported_draft_scope"):
        build_clear_draft_request(_PEER, DraftScope(42, 91, subdialog_peer_id=9))
    snapshot = _snapshot(types.DraftMessage("topic", _DATE, reply_to=types.InputReplyToMessage(0, top_msg_id=6)))
    with pytest.raises(DraftSnapshotError, match="draft_topic_mismatch"):
        build_save_draft_request(_PEER, _SCOPE, snapshot)
