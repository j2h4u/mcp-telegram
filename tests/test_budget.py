from __future__ import annotations

from mcp_telegram.budget import (
    UNREAD_TIER_BOT_DM,
    UNREAD_TIER_CHANNEL,
    UNREAD_TIER_HUMAN_DM,
    UNREAD_TIER_MENTION_GROUP,
    UNREAD_TIER_SMALL_GROUP,
    allocate_message_budget_round_robin,
    unread_chat_tier,
)


class TestUnreadChatTier:
    def test_dm_with_mention(self):
        chat = {"unread_mentions_count": 1, "category": "user"}
        assert unread_chat_tier(chat) == UNREAD_TIER_HUMAN_DM

    def test_bot_with_mention(self):
        chat = {"unread_mentions_count": 1, "category": "bot"}
        assert unread_chat_tier(chat) == UNREAD_TIER_BOT_DM

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
        assert UNREAD_TIER_HUMAN_DM < UNREAD_TIER_MENTION_GROUP
        assert UNREAD_TIER_MENTION_GROUP < UNREAD_TIER_BOT_DM
        assert UNREAD_TIER_HUMAN_DM < UNREAD_TIER_BOT_DM
        assert UNREAD_TIER_BOT_DM < UNREAD_TIER_SMALL_GROUP
        assert UNREAD_TIER_SMALL_GROUP < UNREAD_TIER_CHANNEL


class TestAllocateMessageBudget:
    def test_round_robin_caps_each_dialog_and_preserves_rank_order(self):
        result = allocate_message_budget_round_robin(
            {10: 300, 20: 300, 30: 300}, limit=8, tiers={10: 10, 20: 10, 30: 10}
        )
        assert result == {10: 3, 20: 3, 30: 2}

    def test_round_robin_never_allocates_more_than_five_per_dialog(self):
        result = allocate_message_budget_round_robin({10: 300}, limit=100, tiers={10: 10})
        assert result == {10: 5}

    def test_higher_tier_exhausts_budget_before_lower_tier(self):
        result = allocate_message_budget_round_robin(
            {10: 300, 20: 300, 30: 300}, limit=8, tiers={10: 10, 20: 10, 30: 40}
        )
        assert result == {10: 4, 20: 4, 30: 0}

    def test_lower_tier_receives_remaining_budget_after_higher_tier_cap(self):
        result = allocate_message_budget_round_robin({10: 300, 20: 1, 30: 300}, limit=8, tiers={10: 10, 20: 10, 30: 40})
        assert result == {10: 5, 20: 1, 30: 2}

    def test_custom_cap_and_unsorted_tiers(self):
        result = allocate_message_budget_round_robin(
            {30: 100, 10: 100, 20: 100}, limit=30, tiers={30: 40, 10: 10, 20: 10}, max_per_chat=20
        )
        assert result == {30: 0, 10: 15, 20: 15}

    def test_empty_counts_and_exhausted_budget(self):
        assert allocate_message_budget_round_robin({}, limit=40, tiers={}) == {}
        assert allocate_message_budget_round_robin({10: 5}, limit=0, tiers={10: 10}) == {10: 0}
        assert allocate_message_budget_round_robin({10: 0}, limit=40, tiers={10: 10}) == {10: 0}
