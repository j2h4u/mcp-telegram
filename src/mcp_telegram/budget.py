from __future__ import annotations

# Priority tiers for unread chat sorting (lower = higher priority).
# Gaps between values allow inserting new tiers without renumbering.
UNREAD_TIER_MENTION_DM = 10       # DM with unread @mention
UNREAD_TIER_MENTION_GROUP = 20    # Group with unread @mention
UNREAD_TIER_HUMAN_DM = 30        # 1-on-1 with a real person
UNREAD_TIER_BOT_DM = 40          # 1-on-1 with a bot
UNREAD_TIER_SMALL_GROUP = 50     # Group within size threshold
UNREAD_TIER_CHANNEL = 70         # Channel / broadcast


def unread_chat_tier(chat: dict) -> int:
    """Classify an unread chat into a priority tier.

    Uses our internal ``category`` field (user/bot/group/channel),
    not raw Telegram flags. Unknown categories fall back to SMALL_GROUP.
    """
    has_mentions = chat["unread_mentions_count"] > 0
    category = chat["category"]

    if has_mentions:
        return UNREAD_TIER_MENTION_DM if category in ("user", "bot") else UNREAD_TIER_MENTION_GROUP
    if category == "user":
        return UNREAD_TIER_HUMAN_DM
    if category == "bot":
        return UNREAD_TIER_BOT_DM
    if category == "channel":
        return UNREAD_TIER_CHANNEL
    return UNREAD_TIER_SMALL_GROUP


def allocate_message_budget_proportional(
    unread_counts: dict[int, int],
    limit: int,
    min_per_chat: int = 3,
) -> dict[int, int]:
    """Distribute a message budget across chats with proportional allocation.

    If total unread messages fit within limit, returns unread_counts unchanged.
    If over limit, allocates at least min_per_chat per chat, then distributes
    remaining budget proportionally by unread count.

    Args:
        unread_counts: {chat_id: unread_count} mapping
        limit: Total message budget across all chats
        min_per_chat: Minimum messages per chat (default 3)

    When ``min_per_chat * len(unread_counts) >= limit``, falls back to even
    distribution: ``limit // num_chats`` per chat, remainder to first chats.

    Returns:
        {chat_id: budget_for_chat} allocation
    """
    if not unread_counts:
        return {}

    total_unread = sum(unread_counts.values())
    if total_unread <= limit:
        return unread_counts.copy()

    allocation = {}
    num_chats = len(unread_counts)
    reserved = min_per_chat * num_chats

    if reserved >= limit:
        per_chat = limit // num_chats
        remainder = limit % num_chats
        for i, chat_id in enumerate(sorted(unread_counts.keys())):
            allocation[chat_id] = per_chat + (1 if i < remainder else 0)
        return allocation

    remaining_budget = limit - reserved
    for chat_id, unread_count in unread_counts.items():
        proportion = unread_count / total_unread if total_unread > 0 else 0
        extra = int(proportion * remaining_budget)
        allocation[chat_id] = min_per_chat + min(extra, unread_count - min_per_chat)

    total_allocated = sum(allocation.values())
    if total_allocated > limit:
        overage = total_allocated - limit
        for chat_id in sorted(allocation.keys(), key=lambda cid: allocation[cid], reverse=True):
            if overage <= 0:
                break
            reduction = min(overage, allocation[chat_id] - min_per_chat)
            allocation[chat_id] -= reduction
            overage -= reduction

    return allocation
