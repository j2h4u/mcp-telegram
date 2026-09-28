from .models import DialogType

# Priority tiers for unread chat sorting (lower = higher priority).
# Gaps between values allow inserting new tiers without renumbering.
UNREAD_TIER_MENTION_DM = 10  # DM with unread @mention
UNREAD_TIER_MENTION_GROUP = 20  # Group with unread @mention
UNREAD_TIER_HUMAN_DM = 30  # 1-on-1 with a real person
UNREAD_TIER_BOT_DM = 40  # 1-on-1 with a bot
UNREAD_TIER_SMALL_GROUP = 50  # Group within size threshold
UNREAD_TIER_CHANNEL = 70  # Channel / broadcast


def unread_chat_tier(chat: dict) -> int:
    """Classify an unread chat into a priority tier.

    ``chat`` must have keys: ``unread_mentions_count`` (int), ``category`` (str).
    Uses our internal ``category`` field (user/bot/group/channel),
    not raw Telegram flags. Unknown categories fall back to SMALL_GROUP.
    """
    has_mentions = chat["unread_mentions_count"] > 0
    category = DialogType.parse(chat["category"])

    if has_mentions:
        return UNREAD_TIER_MENTION_DM if category in (DialogType.USER, DialogType.BOT) else UNREAD_TIER_MENTION_GROUP
    if category == DialogType.USER:
        return UNREAD_TIER_HUMAN_DM
    if category == DialogType.BOT:
        return UNREAD_TIER_BOT_DM
    if category == DialogType.CHANNEL:
        return UNREAD_TIER_CHANNEL
    return UNREAD_TIER_SMALL_GROUP


def allocate_message_budget_round_robin(
    unread_counts: dict[int, int], limit: int, *, max_per_chat: int = 5
) -> dict[int, int]:
    """Allocate a bounded inbox budget fairly in ranked dialog order."""
    if not unread_counts or limit <= 0 or max_per_chat <= 0:
        return dict.fromkeys(unread_counts, 0)

    allocation = dict.fromkeys(unread_counts, 0)
    remaining = limit
    while remaining:
        advanced = False
        for chat_id, unread_count in unread_counts.items():
            if remaining == 0:
                break
            if allocation[chat_id] >= min(unread_count, max_per_chat):
                continue
            allocation[chat_id] += 1
            remaining -= 1
            advanced = True
        if not advanced:
            break
    return allocation
