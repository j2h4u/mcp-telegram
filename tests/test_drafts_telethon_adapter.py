from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from telethon.tl import types  # type: ignore[import-untyped]

from mcp_telegram.drafts.contracts import (
    CompositionCompleteness,
    DraftDisposition,
    DraftObservationSource,
)
from mcp_telegram.drafts.telethon_adapter import TelethonDraftSnapshotGateway, normalize_update_draft
from mcp_telegram.telegram_demand import AcquisitionKind, demand_context
from mcp_telegram.telegram_rpc_consumers import DemandKind


def test_normalize_present_empty_draft_is_not_an_absent_draft() -> None:
    update = types.UpdateDraftMessage(types.PeerUser(91), types.DraftMessage("", datetime(2026, 1, 1, tzinfo=UTC)))

    observation = normalize_update_draft(
        update,
        account_id=42,
        source=DraftObservationSource.REALTIME,
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    assert observation is not None
    assert observation.disposition is DraftDisposition.PRESENT
    assert observation.composition is not None
    assert observation.composition.text == ""


def test_normalize_copies_utf16_entity_metadata_but_not_url_target() -> None:
    update = types.UpdateDraftMessage(
        types.PeerUser(91),
        types.DraftMessage(
            "link",
            datetime(2026, 1, 1, tzinfo=UTC),
            entities=[types.MessageEntityTextUrl(0, 4, "https://secret.invalid/token")],
        ),
    )

    observation = normalize_update_draft(
        update,
        account_id=42,
        source=DraftObservationSource.REALTIME,
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    assert observation is not None and observation.composition is not None
    assert observation.composition.entities[0].offset_utf16 == 0
    assert observation.composition.entities[0].length_utf16 == 4
    assert observation.composition.completeness is CompositionCompleteness.PARTIAL
    assert "secret.invalid" not in repr(observation)


def test_normalize_retains_bounded_reply_story_quote_and_monoforum_context() -> None:
    reply = types.InputReplyToMessage(
        reply_to_msg_id=17,
        reply_to_peer_id=types.InputPeerUser(91, 0),
        quote_text="do not persist this quote text",
        monoforum_peer_id=types.InputPeerChannel(92, 0),
    )
    update = types.UpdateDraftMessage(
        types.PeerUser(91),
        types.DraftMessage("draft", datetime(2026, 1, 1, tzinfo=UTC), reply_to=reply),
    )

    observation = normalize_update_draft(
        update,
        account_id=42,
        source=DraftObservationSource.REALTIME,
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    assert observation is not None and observation.composition is not None
    assert observation.composition.reply is not None
    assert observation.composition.reply.message_id == 17
    assert observation.composition.quote is not None
    assert observation.composition.monoforum is not None
    assert observation.composition.completeness is CompositionCompleteness.PARTIAL
    assert "do not persist" not in repr(observation)


@pytest.mark.asyncio
async def test_snapshot_requires_updates_vector_before_absence_can_be_inferred() -> None:
    client = AsyncMock()
    client.return_value = object()
    gateway = TelethonDraftSnapshotGateway(client, 42)

    with demand_context(DemandKind.DRAFT_SNAPSHOT):
        coverage, observations = await gateway.fetch_all_drafts()

    assert coverage.authoritative is False
    assert observations == ()


@pytest.mark.asyncio
async def test_snapshot_is_one_classified_unpaged_rpc() -> None:
    client = AsyncMock()
    client.return_value = types.Updates([], [], [], datetime(2026, 1, 1, tzinfo=UTC), 1)
    gateway = TelethonDraftSnapshotGateway(client, 42)

    with demand_context(DemandKind.DRAFT_SNAPSHOT):
        coverage, observations = await gateway.fetch_all_drafts()

    assert coverage.authoritative is True
    assert observations == ()
    request = client.await_args.args[0]
    assert type(request).__name__ == "GetAllDraftsRequest"
    assert AcquisitionKind.DRAFT_SNAPSHOT.value == "draft_snapshot"
