from __future__ import annotations

from mcp_telegram.budget import (
    UNREAD_TIER_BOT_DM,
    UNREAD_TIER_CHANNEL,
    UNREAD_TIER_HUMAN_DM,
    UNREAD_TIER_MENTION_DM,
    UNREAD_TIER_MENTION_GROUP,
    UNREAD_TIER_SMALL_GROUP,
    allocate_message_budget_round_robin,
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
    def test_round_robin_caps_each_dialog_and_preserves_rank_order(self):
        result = allocate_message_budget_round_robin({10: 300, 20: 300, 30: 300}, limit=8)
        assert result == {10: 3, 20: 3, 30: 2}

    def test_round_robin_never_allocates_more_than_five_per_dialog(self):
        result = allocate_message_budget_round_robin({10: 300}, limit=100)
        assert result == {10: 5}
