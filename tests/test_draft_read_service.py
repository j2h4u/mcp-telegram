import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from telethon.tl import types

from mcp_telegram.config import DraftRecoveryConfig
from mcp_telegram.drafts.contracts import DraftObservationSource
from mcp_telegram.drafts.read_service import read_draft_request
from mcp_telegram.drafts.snapshot import capture_restore_snapshot
from mcp_telegram.drafts.sqlite_projection import SQLiteDraftProjection
from mcp_telegram.drafts.telethon_adapter import normalize_update_draft
from mcp_telegram.sync_db import ensure_sync_schema


@pytest.fixture
def database(tmp_path: Path) -> Iterator[tuple[sqlite3.Connection, SQLiteDraftProjection]]:
    path = tmp_path / "drafts.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    projection = SQLiteDraftProjection(conn, DraftRecoveryConfig())
    projection.bind_account(42)
    try:
        yield conn, projection
    finally:
        conn.close()


def test_unknown_is_not_absent_and_read_does_not_schedule_acquisition(
    database: tuple[sqlite3.Connection, SQLiteDraftProjection],
) -> None:
    conn, _projection = database
    before = conn.total_changes
    result = read_draft_request(conn, {"dialog_id": 100}, account_id=42)
    assert result["ok"] is True
    assert cast(dict[str, object], result["current"])["presence"] == "unknown"
    assert result["history"] == []
    assert result["acquisition_status"] == "unknown"
    assert result["coverage_status"] == "unknown"
    assert conn.total_changes == before


def test_retained_draft_read_returns_no_private_snapshot(
    database: tuple[sqlite3.Connection, SQLiteDraftProjection],
) -> None:
    conn, projection = database
    observed_at = datetime.now(UTC).replace(microsecond=0)
    observation = normalize_update_draft(
        types.UpdateDraftMessage(types.PeerUser(100), types.DraftMessage("retained text", observed_at)),
        account_id=42,
        source=DraftObservationSource.REALTIME,
        observed_at=observed_at,
    )
    assert observation is not None
    assert projection.apply_realtime(observation).accepted
    result = read_draft_request(conn, {"dialog_id": 100}, account_id=42)
    current = cast(dict[str, object], result["current"])
    assert current["presence"] == "present"
    assert current["text"] == "retained text"
    assert current["expected_version"]
    assert "restore_payload" not in str(result)


def test_rich_draft_current_and_history_preserve_complete_tree(
    database: tuple[sqlite3.Connection, SQLiteDraftProjection],
) -> None:
    conn, projection = database
    observed = datetime.now(UTC).replace(microsecond=0)
    long_text = "rich " * 1200
    rich = types.RichMessage([types.PageBlockParagraph(types.TextPlain(long_text))], [], [], rtl=True)
    for offset, draft in enumerate(
        (
            types.DraftMessage("original", observed, rich_message=rich),
            types.DraftMessage("next", observed + timedelta(seconds=1)),
        )
    ):
        observation = normalize_update_draft(
            types.UpdateDraftMessage(types.PeerUser(100), draft),
            account_id=42,
            source=DraftObservationSource.REALTIME,
            observed_at=observed + timedelta(seconds=offset),
        )
        assert observation is not None and projection.apply_realtime(observation).accepted
        if offset == 0:
            current = cast(dict[str, object], read_draft_request(conn, {"dialog_id": 100}, account_id=42)["current"])
            tree = cast(dict[str, object], current["rich_message"])
            assert tree["rtl"] is True
            blocks = cast(list[dict[str, object]], tree["blocks"])
            assert blocks[0]["_"] == "PageBlockParagraph"
            assert cast(dict[str, object], blocks[0]["text"]) == {"_": "TextPlain", "text": long_text}
            assert current["rich_content_status"] == "complete"
    result = read_draft_request(conn, {"dialog_id": 100}, account_id=42)
    history = cast(list[dict[str, object]], result["history"])
    assert long_text in str(history[0]["rich_message"])
    assert history[0]["rich_content_status"] == "complete"
    assert history[0]["source_order_at"] == int(observed.timestamp())
    selected = read_draft_request(conn, {"dialog_id": 100, "history_id": history[0]["history_id"]}, account_id=42)
    assert cast(dict[str, object], selected["selected_history"])["rich_message"] == history[0]["rich_message"]
    assert cast(list[dict[str, object]], selected["history"])[0]["content_omission_reason"] == "selected_history"
    assert "restore_payload" not in str(result)


def test_partial_rich_draft_reports_source_completeness(
    database: tuple[sqlite3.Connection, SQLiteDraftProjection],
) -> None:
    conn, projection = database
    observed = datetime.now(UTC).replace(microsecond=0)
    draft = types.DraftMessage("", observed, rich_message=types.RichMessage([], [], [], part=True))
    observation = normalize_update_draft(
        types.UpdateDraftMessage(types.PeerUser(100), draft),
        account_id=42,
        source=DraftObservationSource.REALTIME,
        observed_at=observed,
    )
    assert observation is not None and projection.apply_realtime(observation).accepted
    current = cast(dict[str, object], read_draft_request(conn, {"dialog_id": 100}, account_id=42)["current"])
    assert current["rich_content_status"] == "partial"
    assert current["rich_text_status"] == "complete"
    assert cast(dict[str, object], current["rich_message"])["part"] is True


def test_rich_draft_reports_missing_media_references(
    database: tuple[sqlite3.Connection, SQLiteDraftProjection],
) -> None:
    conn, projection = database
    observed = datetime.now(UTC).replace(microsecond=0)
    caption = types.PageCaption(types.TextPlain("caption"), types.TextEmpty())
    rich = types.RichMessage([types.PageBlockPhoto(42, caption)], [], [])
    observation = normalize_update_draft(
        types.UpdateDraftMessage(types.PeerUser(100), types.DraftMessage("", observed, rich_message=rich)),
        account_id=42,
        source=DraftObservationSource.REALTIME,
        observed_at=observed,
    )
    assert observation is not None and projection.apply_realtime(observation).accepted
    current = cast(dict[str, object], read_draft_request(conn, {"dialog_id": 100}, account_id=42)["current"])
    assert current["rich_media_reference_gaps"] == {"missing_photo_ids": [42], "missing_document_ids": []}


def test_migration_recovers_current_and_history_rich_facts_without_revision_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mcp_telegram.sync_db as sync_db_module

    path = tmp_path / "old.db"
    with monkeypatch.context() as old:
        old.setattr(sync_db_module, "_CURRENT_SCHEMA_VERSION", 101)
        ensure_sync_schema(path)
    observed = datetime.now(UTC).replace(microsecond=0)
    rich = types.RichMessage([types.PageBlockParagraph(types.TextPlain("retained"))], [], [])
    snapshot = capture_restore_snapshot(types.DraftMessage("", observed, rich_message=rich))
    assert snapshot is not None
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "INSERT INTO draft_current(account_id,dialog_id,state,text,rich_message_json,composition_complete,"
            "source_kind,source_observed_at,observation_started_at,observation_completed_at,projection_revision,"
            "normalization_version,restore_payload,semantic_fingerprint) "
            "VALUES(42,100,'present','',?,1,'realtime_present',1,1,1,7,'1',?,?)",
            ('{"kind":"RichMessage"}', snapshot.payload, snapshot.semantic_fingerprint),
        )
        conn.execute(
            "INSERT INTO draft_versions(account_id,dialog_id,source_revision,state,text,restore_payload,"
            "semantic_fingerprint,source_observed_at,archived_at) VALUES(42,100,6,'present','',?,?,1,2)",
            (snapshot.payload, snapshot.semantic_fingerprint),
        )
        conn.execute(
            "INSERT INTO draft_current(account_id,dialog_id,state,rich_message_json,composition_complete,source_kind,"
            "source_observed_at,observation_started_at,observation_completed_at,projection_revision,normalization_version,"
            "restore_payload,semantic_fingerprint) VALUES(42,101,'present',?,1,'realtime_present',1,1,1,3,'1',?,?)",
            ('{"kind":"RichMessage"}', b"corrupt", "fingerprint"),
        )
    ensure_sync_schema(path)
    with closing(sqlite3.connect(path)) as conn:
        current = cast(
            tuple[str, int] | None,
            conn.execute(
                "SELECT rich_message_json,projection_revision FROM draft_current WHERE dialog_id=100"
            ).fetchone(),
        )
        history = cast(
            tuple[str, int] | None,
            conn.execute("SELECT rich_message_json,source_revision FROM draft_versions").fetchone(),
        )
        assert current is not None and history is not None
        assert current[0] == history[0] and '"text":"retained"' in current[0]
        assert (current[1], history[1]) == (7, 6)
        assert conn.execute("SELECT rich_message_json FROM draft_current WHERE dialog_id=101").fetchone() == (None,)


@pytest.mark.parametrize(
    "payload,error",
    [
        ({"dialog_id": True}, "invalid_params"),
        ({"dialog_id": 0}, "invalid_params"),
        ({"dialog_id": 100, "topic_id": -1}, "invalid_params"),
        ({"dialog_id": 100, "account_id": 43}, "account_mismatch"),
        ({"dialog_id": 100, "history_id": 1}, "history_unavailable"),
    ],
)
def test_invalid_or_other_account_requests_are_rejected(
    database: tuple[sqlite3.Connection, SQLiteDraftProjection], payload: dict[str, object], error: str
) -> None:
    conn, _projection = database
    assert read_draft_request(conn, payload, account_id=42)["error"] == error


@pytest.mark.parametrize("previous_kind", ["exact", "legacy", "unsupported_media"])
def test_history_reads_preserve_restore_availability_and_hide_private_snapshots(
    database: tuple[sqlite3.Connection, SQLiteDraftProjection], previous_kind: str
) -> None:
    conn, projection = database
    first_at = datetime.now(UTC).replace(microsecond=0)
    media = types.InputMediaContact("+0", "Test", "", "") if previous_kind == "unsupported_media" else None
    previous = normalize_update_draft(
        types.UpdateDraftMessage(types.PeerUser(100), types.DraftMessage("previous", first_at, media=media)),
        account_id=42,
        source=DraftObservationSource.REALTIME,
        observed_at=first_at,
    )
    assert previous is not None
    if previous_kind == "legacy":
        previous = replace(previous, restore_snapshot=None)
    assert projection.apply_realtime(previous).accepted
    second_at = first_at + timedelta(seconds=1)
    current = normalize_update_draft(
        types.UpdateDraftMessage(types.PeerUser(100), types.DraftMessage("current", second_at)),
        account_id=42,
        source=DraftObservationSource.REALTIME,
        observed_at=second_at,
    )
    assert current is not None and projection.apply_realtime(current).accepted
    before = conn.total_changes
    result = read_draft_request(conn, {"dialog_id": 100}, account_id=42)
    history = cast(list[dict[str, object]], result["history"])
    assert len(history) == 1
    assert history[0]["text"] == "previous"
    assert history[0]["restorable"] is (previous_kind == "exact")
    assert ("restore_reason" in history[0]) is (previous_kind != "exact")
    selected = read_draft_request(conn, {"dialog_id": 100, "history_id": history[0]["history_id"]}, account_id=42)
    assert cast(dict[str, object], selected["selected_history"])["text"] == history[0]["text"]
    assert conn.total_changes == before
    assert "restore_payload" not in str(result)


def test_rich_history_response_budget_preserves_ids_and_selected_detail(
    database: tuple[sqlite3.Connection, SQLiteDraftProjection],
) -> None:
    conn, projection = database
    observed = datetime.now(UTC).replace(microsecond=0)
    for index in range(11):
        date = observed + timedelta(seconds=index)
        text = f"draft-{index}-" + "x" * 40000
        rich = types.RichMessage([types.PageBlockParagraph(types.TextPlain(text))], [], [])
        draft = types.DraftMessage("", date, rich_message=rich)
        observation = normalize_update_draft(
            types.UpdateDraftMessage(types.PeerUser(100), draft),
            account_id=42,
            source=DraftObservationSource.REALTIME,
            observed_at=date,
        )
        assert observation is not None and projection.apply_realtime(observation).accepted
    result = read_draft_request(conn, {"dialog_id": 100}, account_id=42)
    history = cast(list[dict[str, object]], result["history"])
    assert len(history) == 10
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= 256 * 1024
    omitted = next(item for item in history if item.get("content_included") is False)
    assert omitted["content_omission_reason"] == "response_budget"
    assert "text" not in omitted and "rich_message" not in omitted and "rich_media_reference_gaps" not in omitted
    selected = read_draft_request(conn, {"dialog_id": 100, "history_id": omitted["history_id"]}, account_id=42)
    detail = cast(dict[str, object], selected["selected_history"])
    assert "x" * 40000 in str(detail["rich_message"])
    assert [item["history_id"] for item in cast(list[dict[str, object]], selected["history"])] == [
        item["history_id"] for item in history
    ]
    selected_text = cast(
        dict[str, object],
        cast(list[dict[str, object]], cast(dict[str, object], detail["rich_message"])["blocks"])[0]["text"],
    )["text"]
    assert isinstance(selected_text, str) and json.dumps(selected).count(selected_text) == 1
    assert len(json.dumps(selected, ensure_ascii=False).encode()) <= 256 * 1024


def test_selected_oversized_history_takes_priority_over_current(
    database: tuple[sqlite3.Connection, SQLiteDraftProjection],
) -> None:
    conn, projection = database
    observed = datetime.now(UTC).replace(microsecond=0)
    for index in range(2):
        date = observed + timedelta(seconds=index)
        rich = types.RichMessage([types.PageBlockParagraph(types.TextPlain(f"{index}" + "z" * 270000))], [], [])
        observation = normalize_update_draft(
            types.UpdateDraftMessage(types.PeerUser(100), types.DraftMessage("", date, rich_message=rich)),
            account_id=42,
            source=DraftObservationSource.REALTIME,
            observed_at=date,
        )
        assert observation is not None and projection.apply_realtime(observation).accepted
    first = read_draft_request(conn, {"dialog_id": 100}, account_id=42)
    history_id = cast(list[dict[str, object]], first["history"])[0]["history_id"]
    selected = read_draft_request(conn, {"dialog_id": 100, "history_id": history_id}, account_id=42)
    assert cast(dict[str, object], selected["current"])["content_omission_reason"] == "response_budget"
    assert "z" * 270000 in str(cast(dict[str, object], selected["selected_history"])["rich_message"])
    assert json.dumps(selected).count("z" * 270000) == 1
