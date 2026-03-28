from __future__ import annotations

from mcp_telegram.budget import (
    UNREAD_TIER_BOT_DM,
    UNREAD_TIER_CHANNEL,
    UNREAD_TIER_HUMAN_DM,
    UNREAD_TIER_MENTION_DM,
    UNREAD_TIER_MENTION_GROUP,
    UNREAD_TIER_SMALL_GROUP,
    allocate_message_budget_proportional,
    unread_chat_tier,
)


class TestUnreadChatTier:
    def test_dm_with_mention(self):
        chat = {"unread_mentions_count": 1, "category": "user"}
        assert unread_chat_tier(chat) == UNREAD_TIER_MENTION_DM

    def test_bot_with_mention(self):
        chat = {"unread_mentions_count": 1, "category": "bot"}
        assert unread_chat_tier(chat) == UNREAD_TIER_MENTION_DM

    def test_group_with_mention(self):
        chat = {"unread_mentions_count": 2, "category": "group"}
        assert unread_chat_tier(chat) == UNREAD_TIER_MENTION_GROUP

    def test_channel_with_mention(self):
        chat = {"unread_mentions_count": 1, "category": "channel"}
        assert unread_chat_tier(chat) == UNREAD_TIER_MENTION_GROUP

    def test_human_dm_no_mention(self):
        chat = {"unread_mentions_count": 0, "category": "user"}
        assert unread_chat_tier(chat) == UNREAD_TIER_HUMAN_DM

    def test_bot_dm_no_mention(self):
        chat = {"unread_mentions_count": 0, "category": "bot"}
        assert unread_chat_tier(chat) == UNREAD_TIER_BOT_DM

    def test_channel_no_mention(self):
        chat = {"unread_mentions_count": 0, "category": "channel"}
        assert unread_chat_tier(chat) == UNREAD_TIER_CHANNEL

    def test_group_no_mention(self):
        chat = {"unread_mentions_count": 0, "category": "group"}
        assert unread_chat_tier(chat) == UNREAD_TIER_SMALL_GROUP

    def test_unknown_category_falls_back_to_small_group(self):
        chat = {"unread_mentions_count": 0, "category": "unknown"}
        assert unread_chat_tier(chat) == UNREAD_TIER_SMALL_GROUP

    def test_tier_ordering(self):
        assert UNREAD_TIER_MENTION_DM < UNREAD_TIER_MENTION_GROUP
        assert UNREAD_TIER_MENTION_GROUP < UNREAD_TIER_HUMAN_DM
        assert UNREAD_TIER_HUMAN_DM < UNREAD_TIER_BOT_DM
        assert UNREAD_TIER_BOT_DM < UNREAD_TIER_SMALL_GROUP
        assert UNREAD_TIER_SMALL_GROUP < UNREAD_TIER_CHANNEL


class TestAllocateMessageBudget:
    def test_empty_input(self):
        assert allocate_message_budget_proportional({}, limit=100) == {}

    def test_total_within_limit_returns_copy(self):
        counts = {1: 5, 2: 10}
        result = allocate_message_budget_proportional(counts, limit=100)
        assert result == {1: 5, 2: 10}
        assert result is not counts  # returns a copy

    def test_total_exactly_at_limit(self):
        counts = {1: 50, 2: 50}
        result = allocate_message_budget_proportional(counts, limit=100)
        assert result == {1: 50, 2: 50}

    def test_proportional_allocation_over_limit(self):
        counts = {1: 100, 2: 300}
        result = allocate_message_budget_proportional(counts, limit=50, min_per_chat=3)
        assert sum(result.values()) <= 50
        assert all(v >= 3 for v in result.values())
        # Chat 2 has 3x the unread, should get more budget
        assert result[2] > result[1]

    def test_min_per_chat_respected(self):
        counts = {1: 1000, 2: 1000, 3: 1000}
        result = allocate_message_budget_proportional(counts, limit=30, min_per_chat=5)
        assert all(v >= 5 for v in result.values())

    def test_reserved_exceeds_limit_even_distribution(self):
        # min_per_chat * num_chats >= limit: fallback to even split
        counts = {1: 100, 2: 100, 3: 100}
        result = allocate_message_budget_proportional(counts, limit=6, min_per_chat=5)
        assert sum(result.values()) == 6
        assert result[1] == 2
        assert result[2] == 2
        assert result[3] == 2

    def test_reserved_exceeds_with_remainder(self):
        counts = {1: 100, 2: 100, 3: 100}
        result = allocate_message_budget_proportional(counts, limit=7, min_per_chat=5)
        assert sum(result.values()) == 7
        # Remainder of 1 goes to first chat (sorted by ID)
        values = [result[k] for k in sorted(result.keys())]
        assert values == [3, 2, 2]

    def test_single_chat(self):
        counts = {1: 100}
        result = allocate_message_budget_proportional(counts, limit=20, min_per_chat=3)
        assert result[1] <= 20

    def test_overage_correction(self):
        # Proportional allocation can overshoot; verify correction brings total <= limit
        counts = {1: 90, 2: 10}
        result = allocate_message_budget_proportional(counts, limit=20, min_per_chat=3)
        assert sum(result.values()) <= 20

    def test_allocation_does_not_exceed_unread_count(self):
        counts = {1: 5, 2: 100}
        result = allocate_message_budget_proportional(counts, limit=50, min_per_chat=3)
        assert result[1] <= 5

    def test_default_min_per_chat_is_three(self):
        counts = {1: 1000, 2: 1000}
        result = allocate_message_budget_proportional(counts, limit=10)
        assert all(v >= 3 for v in result.values())
