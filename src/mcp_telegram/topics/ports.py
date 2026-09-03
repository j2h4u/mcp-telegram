"""Variable I/O boundaries used by topic refresh."""

from __future__ import annotations

from typing import Protocol

from .contracts import TopicFact


class TelegramTopicGateway(Protocol):
    async def fetch_topics(self, entity: object) -> tuple[TopicFact, ...]: ...


class TopicSnapshotRepository(Protocol):
    def upsert_topics(self, dialog_id: int, topics: tuple[TopicFact, ...]) -> None: ...


class TopicMetadataRepository(Protocol):
    """Canonical application seam for realtime topic metadata facts."""

    def apply_topic_create(  # noqa: PLR0913
        self,
        dialog_id: int,
        topic_id: int,
        *,
        title: str,
        icon_emoji_id: int | None,
        date: int | None,
        observed_at: int,
    ) -> None: ...

    def apply_topic_edit(  # noqa: PLR0913
        self,
        dialog_id: int,
        topic_id: int,
        *,
        title: str | None,
        icon_emoji_id: int | None,
        hidden: bool | None,
        observed_at: int,
    ) -> None: ...

    def apply_topic_pin(self, dialog_id: int, topic_id: int, *, pinned: bool, observed_at: int) -> None: ...

    def apply_topic_pins(self, dialog_id: int, order: tuple[int, ...], *, observed_at: int) -> None: ...
