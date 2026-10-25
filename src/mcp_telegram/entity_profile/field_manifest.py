"""Finite, reviewed User/UserFull output-field inventory for installed TL layer 229.

Generated from constructor signatures, then classified by semantic ownership.
No runtime schema discovery or unrestricted TL serialization is used. Input-only
constructors reached through broad aliases are ID-only rejection sentinels;
returned sticker/channel/call references retain semantic identifiers. Changed
fields or constructors need an explicit manifest update.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from .contracts import ProfileFieldClass

TL_PROFILE_LAYER = 229


@dataclass(frozen=True, slots=True)
class ProfileFieldRule:
    value_types: tuple[str, ...]
    classification: ProfileFieldClass = ProfileFieldClass.PROFILE
    optional: bool = False
    vector: bool = False
    flag: bool = False
    clear_on_none: bool = False


@dataclass(frozen=True, slots=True)
class ProfileConstructorRule:
    constructor_id: int
    fields: Mapping[str, ProfileFieldRule]
    protocol_only: bool = False


TYPES_CHAT_PHOTO = ("ChatPhotoEmpty", "ChatPhoto")
TYPES_DOCUMENT = ("DocumentEmpty", "Document")
TYPES_DOCUMENT_ATTRIBUTE = (
    "DocumentAttributeImageSize",
    "DocumentAttributeAnimated",
    "DocumentAttributeSticker",
    "DocumentAttributeVideo",
    "DocumentAttributeAudio",
    "DocumentAttributeFilename",
    "DocumentAttributeHasStickers",
    "DocumentAttributeCustomEmoji",
)
TYPES_EMOJI_STATUS = ("EmojiStatusEmpty", "EmojiStatus", "EmojiStatusCollectible", "InputEmojiStatusCollectible")
TYPES_GEO_POINT = ("GeoPointEmpty", "GeoPoint")
TYPES_INLINE_BUTTON_TYPE = (
    "InlineButtonTypeUrl",
    "InlineButtonTypeUrlAuth",
    "InputInlineButtonTypeUrlAuth",
    "InlineButtonTypeWebView",
    "InlineButtonTypeCallback",
    "InlineButtonTypeGame",
    "InlineButtonTypeBuy",
    "InlineButtonTypeSwitchInline",
    "InlineButtonTypeUserProfile",
    "InputInlineButtonTypeUserProfile",
    "InlineButtonTypeCopy",
    "InlineButtonTypeDisabled",
)
TYPES_INPUT_STICKER_SET = (
    "InputStickerSetEmpty",
    "InputStickerSetID",
    "InputStickerSetShortName",
    "InputStickerSetAnimatedEmoji",
    "InputStickerSetDice",
    "InputStickerSetAnimatedEmojiAnimations",
    "InputStickerSetPremiumGifts",
    "InputStickerSetEmojiGenericAnimations",
    "InputStickerSetEmojiDefaultStatuses",
    "InputStickerSetEmojiDefaultTopicIcons",
    "InputStickerSetEmojiChannelDefaultStatuses",
    "InputStickerSetTonGifts",
)
TYPES_MESSAGE_ENTITY = (
    "MessageEntityUnknown",
    "MessageEntityMention",
    "MessageEntityHashtag",
    "MessageEntityBotCommand",
    "MessageEntityUrl",
    "MessageEntityEmail",
    "MessageEntityBold",
    "MessageEntityItalic",
    "MessageEntityCode",
    "MessageEntityPre",
    "MessageEntityTextUrl",
    "MessageEntityMentionName",
    "InputMessageEntityMentionName",
    "MessageEntityPhone",
    "MessageEntityCashtag",
    "MessageEntityUnderline",
    "MessageEntityStrike",
    "MessageEntityBankCard",
    "MessageEntitySpoiler",
    "MessageEntityCustomEmoji",
    "MessageEntityBlockquote",
    "MessageEntityFormattedDate",
    "MessageEntityDiffInsert",
    "MessageEntityDiffReplace",
    "MessageEntityDiffDelete",
)
TYPES_MESSAGE_EXTENDED_MEDIA = ("MessageExtendedMediaPreview", "MessageExtendedMedia")
TYPES_MESSAGE_MEDIA = (
    "MessageMediaEmpty",
    "MessageMediaPhoto",
    "MessageMediaGeo",
    "MessageMediaContact",
    "MessageMediaUnsupported",
    "MessageMediaDocument",
    "MessageMediaWebPage",
    "MessageMediaVenue",
    "MessageMediaGame",
    "MessageMediaInvoice",
    "MessageMediaGeoLive",
    "MessageMediaPoll",
    "MessageMediaDice",
    "MessageMediaStory",
    "MessageMediaGiveaway",
    "MessageMediaGiveawayResults",
    "MessageMediaPaidMedia",
    "MessageMediaToDo",
    "MessageMediaVideoStream",
)
TYPES_NOTIFICATION_SOUND = (
    "NotificationSoundDefault",
    "NotificationSoundNone",
    "NotificationSoundLocal",
    "NotificationSoundRingtone",
)
TYPES_PAGE_BLOCK = (
    "PageBlockUnsupported",
    "PageBlockTitle",
    "PageBlockSubtitle",
    "PageBlockAuthorDate",
    "PageBlockHeader",
    "PageBlockSubheader",
    "PageBlockParagraph",
    "PageBlockPreformatted",
    "PageBlockFooter",
    "PageBlockDivider",
    "PageBlockAnchor",
    "PageBlockList",
    "PageBlockBlockquote",
    "PageBlockPullquote",
    "PageBlockPhoto",
    "PageBlockVideo",
    "PageBlockCover",
    "PageBlockEmbed",
    "PageBlockEmbedPost",
    "PageBlockCollage",
    "PageBlockSlideshow",
    "PageBlockChannel",
    "PageBlockAudio",
    "PageBlockKicker",
    "PageBlockTable",
    "PageBlockOrderedList",
    "PageBlockDetails",
    "PageBlockRelatedArticles",
    "PageBlockMap",
    "PageBlockHeading1",
    "PageBlockHeading2",
    "PageBlockHeading3",
    "PageBlockHeading4",
    "PageBlockHeading5",
    "PageBlockHeading6",
    "PageBlockMath",
    "PageBlockThinking",
    "InputPageBlockMap",
    "PageBlockBlockquoteBlocks",
    "PageBlockButtonRow",
    "PageBlockDocument",
)
TYPES_PEER = ("PeerUser", "PeerChat", "PeerChannel")
TYPES_PEER_COLOR = ("PeerColor", "PeerColorCollectible", "InputPeerColorCollectible")
TYPES_PHOTO = ("PhotoEmpty", "Photo")
TYPES_PHOTO_SIZE = (
    "PhotoSizeEmpty",
    "PhotoSize",
    "PhotoCachedSize",
    "PhotoStrippedSize",
    "PhotoSizeProgressive",
    "PhotoPathSize",
)
TYPES_REACTION = ("ReactionEmpty", "ReactionEmoji", "ReactionCustomEmoji", "ReactionPaid")
TYPES_RICH_TEXT = (
    "TextEmpty",
    "TextPlain",
    "TextBold",
    "TextItalic",
    "TextUnderline",
    "TextStrike",
    "TextFixed",
    "TextUrl",
    "TextEmail",
    "TextConcat",
    "TextSubscript",
    "TextSuperscript",
    "TextMarked",
    "TextPhone",
    "TextImage",
    "TextAnchor",
    "TextMath",
    "TextCustomEmoji",
    "TextSpoiler",
    "TextMention",
    "TextHashtag",
    "TextBotCommand",
    "TextCashtag",
    "TextAutoUrl",
    "TextAutoEmail",
    "TextAutoPhone",
    "TextBankCard",
    "TextMentionName",
    "TextDate",
    "TextDiff",
    "TextButton",
)
TYPES_STARS_AMOUNT = ("StarsAmount", "StarsTonAmount")
TYPES_STAR_GIFT = ("StarGift", "StarGiftUnique")
TYPES_STAR_GIFT_ATTRIBUTE_RARITY = (
    "StarGiftAttributeRarity",
    "StarGiftAttributeRarityUncommon",
    "StarGiftAttributeRarityRare",
    "StarGiftAttributeRarityEpic",
    "StarGiftAttributeRarityLegendary",
)
TYPES_STORY_ITEM = ("StoryItemDeleted", "StoryItemSkipped", "StoryItem")
TYPES_VIDEO_SIZE = ("VideoSize", "VideoSizeEmojiMarkup", "VideoSizeStickerMarkup")
TYPES_WALL_PAPER = ("WallPaper", "WallPaperNoFile")

PROFILE_FIELD_MANIFEST: Mapping[str, ProfileConstructorRule] = {
    "BaseThemeArctic": ProfileConstructorRule(0x5B11125A, {}),
    "BaseThemeClassic": ProfileConstructorRule(0xC3A12462, {}),
    "BaseThemeDay": ProfileConstructorRule(0xFBD81688, {}),
    "BaseThemeNight": ProfileConstructorRule(0xB7B31EA8, {}),
    "BaseThemeTinted": ProfileConstructorRule(0x6D5F77EE, {}),
    "Birthday": ProfileConstructorRule(
        0x6C8E1E06,
        {
            "day": ProfileFieldRule(("int",)),
            "month": ProfileFieldRule(("int",)),
            "year": ProfileFieldRule(("int",), optional=True),
        },
    ),
    "BotAppSettings": ProfileConstructorRule(
        0xC99B1950,
        {
            "placeholder_path": ProfileFieldRule(("bytes",), optional=True),
            "background_color": ProfileFieldRule(("int",), optional=True),
            "background_dark_color": ProfileFieldRule(("int",), optional=True),
            "header_color": ProfileFieldRule(("int",), optional=True),
            "header_dark_color": ProfileFieldRule(("int",), optional=True),
        },
    ),
    "BotCommand": ProfileConstructorRule(
        0x9852D6D2,
        {
            "command": ProfileFieldRule(("str",)),
            "description": ProfileFieldRule(("str",)),
            "ephemeral": ProfileFieldRule(("bool",), optional=True, flag=True),
        },
    ),
    "BotInfo": ProfileConstructorRule(
        0x4D8A0299,
        {
            "has_preview_medias": ProfileFieldRule(("bool",), optional=True, flag=True),
            "user_id": ProfileFieldRule(("int",), optional=True),
            "description": ProfileFieldRule(("str",), optional=True),
            "description_photo": ProfileFieldRule(TYPES_PHOTO, optional=True),
            "description_document": ProfileFieldRule(TYPES_DOCUMENT, optional=True),
            "commands": ProfileFieldRule(("BotCommand",), optional=True, vector=True),
            "menu_button": ProfileFieldRule(
                ("BotMenuButtonDefault", "BotMenuButtonCommands", "BotMenuButton"), optional=True
            ),
            "privacy_policy_url": ProfileFieldRule(("str",), optional=True),
            "app_settings": ProfileFieldRule(("BotAppSettings",), optional=True),
            "verifier_settings": ProfileFieldRule(("BotVerifierSettings",), optional=True),
        },
    ),
    "BotMenuButton": ProfileConstructorRule(
        0xC7B57CE6,
        {
            "text": ProfileFieldRule(("str",)),
            "url": ProfileFieldRule(("str",)),
        },
    ),
    "BotMenuButtonCommands": ProfileConstructorRule(0x4258C205, {}),
    "BotMenuButtonDefault": ProfileConstructorRule(0x7533A588, {}),
    "BotVerification": ProfileConstructorRule(
        0xF93CD45C,
        {
            "bot_id": ProfileFieldRule(("int",)),
            "icon": ProfileFieldRule(("int",)),
            "description": ProfileFieldRule(("str",)),
        },
    ),
    "BotVerifierSettings": ProfileConstructorRule(
        0xB0CD6617,
        {
            "icon": ProfileFieldRule(("int",)),
            "company": ProfileFieldRule(("str",)),
            "can_modify_custom_description": ProfileFieldRule(("bool",), optional=True, flag=True),
            "custom_description": ProfileFieldRule(("str",), optional=True),
        },
    ),
    "BusinessAwayMessage": ProfileConstructorRule(
        0xEF156A5C,
        {
            "shortcut_id": ProfileFieldRule(("int",)),
            "schedule": ProfileFieldRule(
                (
                    "BusinessAwayMessageScheduleAlways",
                    "BusinessAwayMessageScheduleOutsideWorkHours",
                    "BusinessAwayMessageScheduleCustom",
                )
            ),
            "recipients": ProfileFieldRule(("BusinessRecipients",)),
            "offline_only": ProfileFieldRule(("bool",), optional=True, flag=True),
        },
    ),
    "BusinessAwayMessageScheduleAlways": ProfileConstructorRule(0xC9B9E2B9, {}),
    "BusinessAwayMessageScheduleCustom": ProfileConstructorRule(
        0xCC4D9ECC,
        {
            "start_date": ProfileFieldRule(("datetime",), optional=True),
            "end_date": ProfileFieldRule(("datetime",), optional=True),
        },
    ),
    "BusinessAwayMessageScheduleOutsideWorkHours": ProfileConstructorRule(0xC3F2F501, {}),
    "BusinessGreetingMessage": ProfileConstructorRule(
        0xE519ABAB,
        {
            "shortcut_id": ProfileFieldRule(("int",)),
            "recipients": ProfileFieldRule(("BusinessRecipients",)),
            "no_activity_days": ProfileFieldRule(("int",)),
        },
    ),
    "BusinessIntro": ProfileConstructorRule(
        0x5A0A066D,
        {
            "title": ProfileFieldRule(("str",)),
            "description": ProfileFieldRule(("str",)),
            "sticker": ProfileFieldRule(TYPES_DOCUMENT, optional=True),
        },
    ),
    "BusinessLocation": ProfileConstructorRule(
        0xAC5C1AF7,
        {
            "address": ProfileFieldRule(("str",)),
            "geo_point": ProfileFieldRule(TYPES_GEO_POINT, optional=True),
        },
    ),
    "BusinessRecipients": ProfileConstructorRule(
        0x21108FF7,
        {
            "existing_chats": ProfileFieldRule(("bool",), optional=True, flag=True),
            "new_chats": ProfileFieldRule(("bool",), optional=True, flag=True),
            "contacts": ProfileFieldRule(("bool",), optional=True, flag=True),
            "non_contacts": ProfileFieldRule(("bool",), optional=True, flag=True),
            "exclude_selected": ProfileFieldRule(("bool",), optional=True, flag=True),
            "users": ProfileFieldRule(("int",), optional=True, vector=True),
        },
    ),
    "BusinessWeeklyOpen": ProfileConstructorRule(
        0x120B1AB9,
        {
            "start_minute": ProfileFieldRule(("int",)),
            "end_minute": ProfileFieldRule(("int",)),
        },
    ),
    "BusinessWorkHours": ProfileConstructorRule(
        0x8C92B098,
        {
            "timezone_id": ProfileFieldRule(("str",)),
            "weekly_open": ProfileFieldRule(("BusinessWeeklyOpen",), vector=True),
            "open_now": ProfileFieldRule(("bool",), optional=True, flag=True),
        },
    ),
    "Channel": ProfileConstructorRule(
        0xD49F34C6,
        {
            "id": ProfileFieldRule(("int",)),
            "title": ProfileFieldRule(("str",)),
            "photo": ProfileFieldRule(TYPES_CHAT_PHOTO),
            "date": ProfileFieldRule(("datetime",), optional=True),
            "creator": ProfileFieldRule(("bool",), optional=True, flag=True),
            "left": ProfileFieldRule(("bool",), optional=True, flag=True),
            "broadcast": ProfileFieldRule(("bool",), optional=True, flag=True),
            "verified": ProfileFieldRule(("bool",), optional=True, flag=True),
            "megagroup": ProfileFieldRule(("bool",), optional=True, flag=True),
            "restricted": ProfileFieldRule(("bool",), optional=True, flag=True),
            "signatures": ProfileFieldRule(("bool",), optional=True, flag=True),
            "min": ProfileFieldRule(("bool",), optional=True, flag=True),
            "scam": ProfileFieldRule(("bool",), optional=True, flag=True),
            "has_link": ProfileFieldRule(("bool",), optional=True, flag=True),
            "has_geo": ProfileFieldRule(("bool",), optional=True, flag=True),
            "slowmode_enabled": ProfileFieldRule(("bool",), optional=True, flag=True),
            "call_active": ProfileFieldRule(("bool",), optional=True, flag=True),
            "call_not_empty": ProfileFieldRule(("bool",), optional=True, flag=True),
            "fake": ProfileFieldRule(("bool",), optional=True, flag=True),
            "gigagroup": ProfileFieldRule(("bool",), optional=True, flag=True),
            "noforwards": ProfileFieldRule(("bool",), optional=True, flag=True),
            "join_to_send": ProfileFieldRule(("bool",), optional=True, flag=True),
            "join_request": ProfileFieldRule(("bool",), optional=True, flag=True),
            "forum": ProfileFieldRule(("bool",), optional=True, flag=True),
            "stories_hidden": ProfileFieldRule(("bool",), optional=True, flag=True),
            "stories_hidden_min": ProfileFieldRule(("bool",), optional=True, flag=True),
            "stories_unavailable": ProfileFieldRule(("bool",), optional=True, flag=True),
            "signature_profiles": ProfileFieldRule(("bool",), optional=True, flag=True),
            "autotranslation": ProfileFieldRule(("bool",), optional=True, flag=True),
            "broadcast_messages_allowed": ProfileFieldRule(("bool",), optional=True, flag=True),
            "monoforum": ProfileFieldRule(("bool",), optional=True, flag=True),
            "forum_tabs": ProfileFieldRule(("bool",), optional=True, flag=True),
            "access_hash": ProfileFieldRule(("int",), classification=ProfileFieldClass.CAPABILITY, optional=True),
            "username": ProfileFieldRule(("str",), optional=True),
            "restriction_reason": ProfileFieldRule(("RestrictionReason",), optional=True, vector=True),
            "admin_rights": ProfileFieldRule(("ChatAdminRights",), optional=True),
            "banned_rights": ProfileFieldRule(("ChatBannedRights",), optional=True),
            "default_banned_rights": ProfileFieldRule(("ChatBannedRights",), optional=True),
            "participants_count": ProfileFieldRule(("int",), optional=True),
            "usernames": ProfileFieldRule(("Username",), optional=True, vector=True),
            "stories_max_id": ProfileFieldRule(("RecentStory",), optional=True),
            "color": ProfileFieldRule(TYPES_PEER_COLOR, optional=True),
            "profile_color": ProfileFieldRule(TYPES_PEER_COLOR, optional=True),
            "emoji_status": ProfileFieldRule(TYPES_EMOJI_STATUS, optional=True),
            "level": ProfileFieldRule(("int",), optional=True),
            "subscription_until_date": ProfileFieldRule(("datetime",), optional=True),
            "bot_verification_icon": ProfileFieldRule(("int",), optional=True),
            "send_paid_messages_stars": ProfileFieldRule(("int",), optional=True),
            "linked_monoforum_id": ProfileFieldRule(("int",), optional=True),
            "linked_community_id": ProfileFieldRule(("int",), optional=True),
        },
    ),
    "ChannelForbidden": ProfileConstructorRule(
        0x17D493D5,
        {
            "id": ProfileFieldRule(("int",)),
            "access_hash": ProfileFieldRule(("int",), classification=ProfileFieldClass.CAPABILITY),
            "title": ProfileFieldRule(("str",)),
            "broadcast": ProfileFieldRule(("bool",), optional=True, flag=True),
            "megagroup": ProfileFieldRule(("bool",), optional=True, flag=True),
            "monoforum": ProfileFieldRule(("bool",), optional=True, flag=True),
            "until_date": ProfileFieldRule(("datetime",), optional=True),
        },
    ),
    "Chat": ProfileConstructorRule(
        0x41CBF256,
        {
            "id": ProfileFieldRule(("int",)),
            "title": ProfileFieldRule(("str",)),
            "photo": ProfileFieldRule(TYPES_CHAT_PHOTO),
            "participants_count": ProfileFieldRule(("int",)),
            "date": ProfileFieldRule(("datetime",), optional=True),
            "version": ProfileFieldRule(("int",)),
            "creator": ProfileFieldRule(("bool",), optional=True, flag=True),
            "left": ProfileFieldRule(("bool",), optional=True, flag=True),
            "deactivated": ProfileFieldRule(("bool",), optional=True, flag=True),
            "call_active": ProfileFieldRule(("bool",), optional=True, flag=True),
            "call_not_empty": ProfileFieldRule(("bool",), optional=True, flag=True),
            "noforwards": ProfileFieldRule(("bool",), optional=True, flag=True),
            "migrated_to": ProfileFieldRule(
                ("InputChannelEmpty", "InputChannel", "InputChannelFromMessage"), optional=True
            ),
            "admin_rights": ProfileFieldRule(("ChatAdminRights",), optional=True),
            "default_banned_rights": ProfileFieldRule(("ChatBannedRights",), optional=True),
        },
    ),
    "ChatAdminRights": ProfileConstructorRule(
        0x5FB224D5,
        {
            "change_info": ProfileFieldRule(("bool",), optional=True, flag=True),
            "post_messages": ProfileFieldRule(("bool",), optional=True, flag=True),
            "edit_messages": ProfileFieldRule(("bool",), optional=True, flag=True),
            "delete_messages": ProfileFieldRule(("bool",), optional=True, flag=True),
            "ban_users": ProfileFieldRule(("bool",), optional=True, flag=True),
            "invite_users": ProfileFieldRule(("bool",), optional=True, flag=True),
            "pin_messages": ProfileFieldRule(("bool",), optional=True, flag=True),
            "add_admins": ProfileFieldRule(("bool",), optional=True, flag=True),
            "anonymous": ProfileFieldRule(("bool",), optional=True, flag=True),
            "manage_call": ProfileFieldRule(("bool",), optional=True, flag=True),
            "other": ProfileFieldRule(("bool",), optional=True, flag=True),
            "manage_topics": ProfileFieldRule(("bool",), optional=True, flag=True),
            "post_stories": ProfileFieldRule(("bool",), optional=True, flag=True),
            "edit_stories": ProfileFieldRule(("bool",), optional=True, flag=True),
            "delete_stories": ProfileFieldRule(("bool",), optional=True, flag=True),
            "manage_direct_messages": ProfileFieldRule(("bool",), optional=True, flag=True),
            "manage_ranks": ProfileFieldRule(("bool",), optional=True, flag=True),
            "manage_linked_peers": ProfileFieldRule(("bool",), optional=True, flag=True),
            "manage_welcome_messages": ProfileFieldRule(("bool",), optional=True, flag=True),
        },
    ),
    "ChatBannedRights": ProfileConstructorRule(
        0x9F120418,
        {
            "until_date": ProfileFieldRule(("datetime",), optional=True),
            "view_messages": ProfileFieldRule(("bool",), optional=True, flag=True),
            "send_messages": ProfileFieldRule(("bool",), optional=True, flag=True),
            "send_media": ProfileFieldRule(("bool",), optional=True, flag=True),
            "send_stickers": ProfileFieldRule(("bool",), optional=True, flag=True),
            "send_gifs": ProfileFieldRule(("bool",), optional=True, flag=True),
            "send_games": ProfileFieldRule(("bool",), optional=True, flag=True),
            "send_inline": ProfileFieldRule(("bool",), optional=True, flag=True),
            "embed_links": ProfileFieldRule(("bool",), optional=True, flag=True),
            "send_polls": ProfileFieldRule(("bool",), optional=True, flag=True),
            "change_info": ProfileFieldRule(("bool",), optional=True, flag=True),
            "invite_users": ProfileFieldRule(("bool",), optional=True, flag=True),
            "pin_messages": ProfileFieldRule(("bool",), optional=True, flag=True),
            "manage_topics": ProfileFieldRule(("bool",), optional=True, flag=True),
            "send_photos": ProfileFieldRule(("bool",), optional=True, flag=True),
            "send_videos": ProfileFieldRule(("bool",), optional=True, flag=True),
            "send_roundvideos": ProfileFieldRule(("bool",), optional=True, flag=True),
            "send_audios": ProfileFieldRule(("bool",), optional=True, flag=True),
            "send_voices": ProfileFieldRule(("bool",), optional=True, flag=True),
            "send_docs": ProfileFieldRule(("bool",), optional=True, flag=True),
            "send_plain": ProfileFieldRule(("bool",), optional=True, flag=True),
            "edit_rank": ProfileFieldRule(("bool",), optional=True, flag=True),
            "send_reactions": ProfileFieldRule(("bool",), optional=True, flag=True),
            "manage_linked_peers": ProfileFieldRule(("bool",), optional=True, flag=True),
        },
    ),
    "ChatEmpty": ProfileConstructorRule(
        0x29562865,
        {
            "id": ProfileFieldRule(("int",)),
        },
    ),
    "ChatForbidden": ProfileConstructorRule(
        0x6592A1A7,
        {
            "id": ProfileFieldRule(("int",)),
            "title": ProfileFieldRule(("str",)),
        },
    ),
    "ChatPhoto": ProfileConstructorRule(
        0x1C6E1C11,
        {
            "photo_id": ProfileFieldRule(("int",)),
            "dc_id": ProfileFieldRule(("int",), classification=ProfileFieldClass.CAPABILITY),
            "has_video": ProfileFieldRule(("bool",), optional=True, flag=True),
            "stripped_thumb": ProfileFieldRule(("bytes",), optional=True),
        },
    ),
    "ChatPhotoEmpty": ProfileConstructorRule(0x37C1011C, {}),
    "ChatTheme": ProfileConstructorRule(
        0xC3DFFC04,
        {
            "emoticon": ProfileFieldRule(("str",)),
        },
    ),
    "ChatThemeUniqueGift": ProfileConstructorRule(
        0x3458F9C8,
        {
            "gift": ProfileFieldRule(TYPES_STAR_GIFT),
            "theme_settings": ProfileFieldRule(("ThemeSettings",), vector=True),
        },
    ),
    "Community": ProfileConstructorRule(
        0x65EFE954,
        {
            "id": ProfileFieldRule(("int",)),
            "title": ProfileFieldRule(("str",)),
            "photo": ProfileFieldRule(TYPES_CHAT_PHOTO),
            "date": ProfileFieldRule(("datetime",), optional=True),
            "creator": ProfileFieldRule(("bool",), optional=True, flag=True),
            "left": ProfileFieldRule(("bool",), optional=True, flag=True),
            "min": ProfileFieldRule(("bool",), optional=True, flag=True),
            "collapsed_in_dialogs": ProfileFieldRule(("bool",), optional=True, flag=True),
            "access_hash": ProfileFieldRule(("int",), classification=ProfileFieldClass.CAPABILITY, optional=True),
            "admin_rights": ProfileFieldRule(("ChatAdminRights",), optional=True),
            "default_banned_rights": ProfileFieldRule(("ChatBannedRights",), optional=True),
        },
    ),
    "CommunityForbidden": ProfileConstructorRule(
        0xFD3CDAB8,
        {
            "id": ProfileFieldRule(("int",)),
            "title": ProfileFieldRule(("str",)),
            "access_hash": ProfileFieldRule(("int",), classification=ProfileFieldClass.CAPABILITY, optional=True),
        },
    ),
    "DisallowedGiftsSettings": ProfileConstructorRule(
        0x71F276C4,
        {
            "disallow_unlimited_stargifts": ProfileFieldRule(("bool",), optional=True, flag=True),
            "disallow_limited_stargifts": ProfileFieldRule(("bool",), optional=True, flag=True),
            "disallow_unique_stargifts": ProfileFieldRule(("bool",), optional=True, flag=True),
            "disallow_premium_gifts": ProfileFieldRule(("bool",), optional=True, flag=True),
            "disallow_stargifts_from_channels": ProfileFieldRule(("bool",), optional=True, flag=True),
        },
    ),
    "Document": ProfileConstructorRule(
        0x8FD4C4D8,
        {
            "id": ProfileFieldRule(("int",)),
            "access_hash": ProfileFieldRule(("int",), classification=ProfileFieldClass.CAPABILITY),
            "file_reference": ProfileFieldRule(("bytes",), classification=ProfileFieldClass.CAPABILITY),
            "date": ProfileFieldRule(("datetime",), optional=True),
            "mime_type": ProfileFieldRule(("str",)),
            "size": ProfileFieldRule(("int",)),
            "dc_id": ProfileFieldRule(("int",), classification=ProfileFieldClass.CAPABILITY),
            "attributes": ProfileFieldRule(TYPES_DOCUMENT_ATTRIBUTE, vector=True),
            "thumbs": ProfileFieldRule(TYPES_PHOTO_SIZE, optional=True, vector=True),
            "video_thumbs": ProfileFieldRule(TYPES_VIDEO_SIZE, optional=True, vector=True),
        },
    ),
    "DocumentAttributeAnimated": ProfileConstructorRule(0x11B58939, {}),
    "DocumentAttributeAudio": ProfileConstructorRule(
        0x9852F9C6,
        {
            "duration": ProfileFieldRule(("int",)),
            "voice": ProfileFieldRule(("bool",), optional=True, flag=True),
            "title": ProfileFieldRule(("str",), optional=True),
            "performer": ProfileFieldRule(("str",), optional=True),
            "waveform": ProfileFieldRule(("bytes",), optional=True),
        },
    ),
    "DocumentAttributeCustomEmoji": ProfileConstructorRule(
        0xFD149899,
        {
            "alt": ProfileFieldRule(("str",)),
            "stickerset": ProfileFieldRule(TYPES_INPUT_STICKER_SET),
            "free": ProfileFieldRule(("bool",), optional=True, flag=True),
            "text_color": ProfileFieldRule(("bool",), optional=True, flag=True),
        },
    ),
    "DocumentAttributeFilename": ProfileConstructorRule(
        0x15590068,
        {
            "file_name": ProfileFieldRule(("str",)),
        },
    ),
    "DocumentAttributeHasStickers": ProfileConstructorRule(0x9801D2F7, {}),
    "DocumentAttributeImageSize": ProfileConstructorRule(
        0x6C37C15C,
        {
            "w": ProfileFieldRule(("int",)),
            "h": ProfileFieldRule(("int",)),
        },
    ),
    "DocumentAttributeSticker": ProfileConstructorRule(
        0x6319D612,
        {
            "alt": ProfileFieldRule(("str",)),
            "stickerset": ProfileFieldRule(TYPES_INPUT_STICKER_SET),
            "mask": ProfileFieldRule(("bool",), optional=True, flag=True),
            "mask_coords": ProfileFieldRule(("MaskCoords",), optional=True),
        },
    ),
    "DocumentAttributeVideo": ProfileConstructorRule(
        0x43C57C48,
        {
            "duration": ProfileFieldRule(("float",)),
            "w": ProfileFieldRule(("int",)),
            "h": ProfileFieldRule(("int",)),
            "round_message": ProfileFieldRule(("bool",), optional=True, flag=True),
            "supports_streaming": ProfileFieldRule(("bool",), optional=True, flag=True),
            "nosound": ProfileFieldRule(("bool",), optional=True, flag=True),
            "preload_prefix_size": ProfileFieldRule(("int",), optional=True),
            "video_start_ts": ProfileFieldRule(("float",), optional=True),
            "video_codec": ProfileFieldRule(("str",), optional=True),
        },
    ),
    "DocumentEmpty": ProfileConstructorRule(
        0x36F8C871,
        {
            "id": ProfileFieldRule(("int",)),
        },
    ),
    "EmojiStatus": ProfileConstructorRule(
        0xE7FF068A,
        {
            "document_id": ProfileFieldRule(("int",)),
            "until": ProfileFieldRule(("datetime",), optional=True),
        },
    ),
    "EmojiStatusCollectible": ProfileConstructorRule(
        0x7184603B,
        {
            "collectible_id": ProfileFieldRule(("int",)),
            "document_id": ProfileFieldRule(("int",)),
            "title": ProfileFieldRule(("str",)),
            "slug": ProfileFieldRule(("str",)),
            "pattern_document_id": ProfileFieldRule(("int",)),
            "center_color": ProfileFieldRule(("int",)),
            "edge_color": ProfileFieldRule(("int",)),
            "pattern_color": ProfileFieldRule(("int",)),
            "text_color": ProfileFieldRule(("int",)),
            "until": ProfileFieldRule(("datetime",), optional=True),
        },
    ),
    "EmojiStatusEmpty": ProfileConstructorRule(0x2DE11AAE, {}),
    "Game": ProfileConstructorRule(
        0xBDF9653B,
        {
            "id": ProfileFieldRule(("int",)),
            "access_hash": ProfileFieldRule(("int",), classification=ProfileFieldClass.CAPABILITY),
            "short_name": ProfileFieldRule(("str",)),
            "title": ProfileFieldRule(("str",)),
            "description": ProfileFieldRule(("str",)),
            "photo": ProfileFieldRule(TYPES_PHOTO),
            "document": ProfileFieldRule(TYPES_DOCUMENT, optional=True),
        },
    ),
    "GeoPoint": ProfileConstructorRule(
        0xB2A2F663,
        {
            "long": ProfileFieldRule(("float",)),
            "lat": ProfileFieldRule(("float",)),
            "access_hash": ProfileFieldRule(("int",), classification=ProfileFieldClass.CAPABILITY),
            "accuracy_radius": ProfileFieldRule(("int",), optional=True),
        },
    ),
    "GeoPointAddress": ProfileConstructorRule(
        0xDE4C5D93,
        {
            "country_iso2": ProfileFieldRule(("str",)),
            "state": ProfileFieldRule(("str",), optional=True),
            "city": ProfileFieldRule(("str",), optional=True),
            "street": ProfileFieldRule(("str",), optional=True),
        },
    ),
    "GeoPointEmpty": ProfileConstructorRule(0x1117DD5F, {}),
    "InlineButtonTypeBuy": ProfileConstructorRule(0x48BAD7A5, {}),
    "InlineButtonTypeCallback": ProfileConstructorRule(
        0x2955BC38,
        {
            "data": ProfileFieldRule(("bytes",)),
            "requires_password": ProfileFieldRule(("bool",), optional=True, flag=True),
        },
    ),
    "InlineButtonTypeCopy": ProfileConstructorRule(
        0xB41D3272,
        {
            "copy_text": ProfileFieldRule(("str",)),
        },
    ),
    "InlineButtonTypeDisabled": ProfileConstructorRule(0xA438619D, {}),
    "InlineButtonTypeGame": ProfileConstructorRule(0x5CD3709D, {}),
    "InlineButtonTypeSwitchInline": ProfileConstructorRule(
        0x93773FF5,
        {
            "query": ProfileFieldRule(("str",)),
            "same_peer": ProfileFieldRule(("bool",), optional=True, flag=True),
            "peer_types": ProfileFieldRule(
                (
                    "InlineQueryPeerTypeSameBotPM",
                    "InlineQueryPeerTypePM",
                    "InlineQueryPeerTypeChat",
                    "InlineQueryPeerTypeMegagroup",
                    "InlineQueryPeerTypeBroadcast",
                    "InlineQueryPeerTypeBotPM",
                ),
                optional=True,
                vector=True,
            ),
        },
    ),
    "InlineButtonTypeUrl": ProfileConstructorRule(
        0xECA4F8D4,
        {
            "url": ProfileFieldRule(("str",)),
        },
    ),
    "InlineButtonTypeUrlAuth": ProfileConstructorRule(
        0xBFD02DA2,
        {
            "url": ProfileFieldRule(("str",)),
            "button_id": ProfileFieldRule(("int",)),
            "fwd_text": ProfileFieldRule(("str",), optional=True),
        },
    ),
    "InlineButtonTypeUserProfile": ProfileConstructorRule(
        0x3FA33FCF,
        {
            "user_id": ProfileFieldRule(("int",)),
        },
    ),
    "InlineButtonTypeWebView": ProfileConstructorRule(
        0x3BCAB5B4,
        {
            "url": ProfileFieldRule(("str",)),
        },
    ),
    "InlineQueryPeerTypeBotPM": ProfileConstructorRule(0x0E3B2D0C, {}),
    "InlineQueryPeerTypeBroadcast": ProfileConstructorRule(0x6334EE9A, {}),
    "InlineQueryPeerTypeChat": ProfileConstructorRule(0xD766C50A, {}),
    "InlineQueryPeerTypeMegagroup": ProfileConstructorRule(0x5EC4BE43, {}),
    "InlineQueryPeerTypePM": ProfileConstructorRule(0x833C0FAC, {}),
    "InlineQueryPeerTypeSameBotPM": ProfileConstructorRule(0x3081ED9D, {}),
    "InputChannel": ProfileConstructorRule(
        0xF35AEC28,
        {
            "channel_id": ProfileFieldRule(("int",)),
            "access_hash": ProfileFieldRule(("int",), classification=ProfileFieldClass.CAPABILITY),
        },
    ),
    "InputChannelEmpty": ProfileConstructorRule(0xEE8C1E86, {}),
    "InputChannelFromMessage": ProfileConstructorRule(
        0x5B934F9D,
        {
            "peer": ProfileFieldRule(
                (
                    "InputPeerEmpty",
                    "InputPeerSelf",
                    "InputPeerChat",
                    "InputPeerUser",
                    "InputPeerChannel",
                    "InputPeerUserFromMessage",
                    "InputPeerChannelFromMessage",
                )
            ),
            "msg_id": ProfileFieldRule(("int",)),
            "channel_id": ProfileFieldRule(("int",)),
        },
    ),
    "InputEmojiStatusCollectible": ProfileConstructorRule(0x07141DBF, {}, protocol_only=True),
    "InputGroupCall": ProfileConstructorRule(
        0xD8AA840F,
        {
            "id": ProfileFieldRule(("int",)),
            "access_hash": ProfileFieldRule(("int",), classification=ProfileFieldClass.CAPABILITY),
        },
    ),
    "InputGroupCallInviteMessage": ProfileConstructorRule(
        0x8C10603F,
        {
            "msg_id": ProfileFieldRule(("int",)),
        },
    ),
    "InputGroupCallSlug": ProfileConstructorRule(
        0xFE06823F,
        {
            "slug": ProfileFieldRule(("str",)),
        },
    ),
    "InputInlineButtonTypeUrlAuth": ProfileConstructorRule(0x9961BCB4, {}, protocol_only=True),
    "InputInlineButtonTypeUserProfile": ProfileConstructorRule(0x53F3CE5A, {}, protocol_only=True),
    "InputMediaAreaChannelPost": ProfileConstructorRule(0x2271F2BF, {}, protocol_only=True),
    "InputMediaAreaVenue": ProfileConstructorRule(0xB282217F, {}, protocol_only=True),
    "InputMessageEntityMentionName": ProfileConstructorRule(0x208E68C9, {}, protocol_only=True),
    "InputPageBlockMap": ProfileConstructorRule(0x574B617F, {}, protocol_only=True),
    "InputPeerChannel": ProfileConstructorRule(0x27BCBBFC, {}, protocol_only=True),
    "InputPeerChannelFromMessage": ProfileConstructorRule(0xBD2A0840, {}, protocol_only=True),
    "InputPeerChat": ProfileConstructorRule(0x35A95CB9, {}, protocol_only=True),
    "InputPeerColorCollectible": ProfileConstructorRule(0xB8EA86A9, {}, protocol_only=True),
    "InputPeerEmpty": ProfileConstructorRule(0x7F3B18EA, {}, protocol_only=True),
    "InputPeerSelf": ProfileConstructorRule(0x7DA07EC9, {}, protocol_only=True),
    "InputPeerUser": ProfileConstructorRule(0xDDE8A54C, {}, protocol_only=True),
    "InputPeerUserFromMessage": ProfileConstructorRule(0xA87B0A1C, {}, protocol_only=True),
    "InputPollAnswer": ProfileConstructorRule(0x199FED96, {}, protocol_only=True),
    "InputStickerSetAnimatedEmoji": ProfileConstructorRule(0x028703C8, {}),
    "InputStickerSetAnimatedEmojiAnimations": ProfileConstructorRule(0x0CDE3739, {}),
    "InputStickerSetDice": ProfileConstructorRule(
        0xE67F520E,
        {
            "emoticon": ProfileFieldRule(("str",)),
        },
    ),
    "InputStickerSetEmojiChannelDefaultStatuses": ProfileConstructorRule(0x49748553, {}),
    "InputStickerSetEmojiDefaultStatuses": ProfileConstructorRule(0x29D0F5EE, {}),
    "InputStickerSetEmojiDefaultTopicIcons": ProfileConstructorRule(0x44C1F8E9, {}),
    "InputStickerSetEmojiGenericAnimations": ProfileConstructorRule(0x04C4D4CE, {}),
    "InputStickerSetEmpty": ProfileConstructorRule(0xFFB62B95, {}),
    "InputStickerSetID": ProfileConstructorRule(
        0x9DE7A269,
        {
            "id": ProfileFieldRule(("int",)),
            "access_hash": ProfileFieldRule(("int",), classification=ProfileFieldClass.CAPABILITY),
        },
    ),
    "InputStickerSetPremiumGifts": ProfileConstructorRule(0xC88B3B02, {}),
    "InputStickerSetShortName": ProfileConstructorRule(
        0x861CC8A0,
        {
            "short_name": ProfileFieldRule(("str",)),
        },
    ),
    "InputStickerSetTonGifts": ProfileConstructorRule(0x1CF671A0, {}),
    "MaskCoords": ProfileConstructorRule(
        0xAED6DBB2,
        {
            "n": ProfileFieldRule(("int",)),
            "x": ProfileFieldRule(("float",)),
            "y": ProfileFieldRule(("float",)),
            "zoom": ProfileFieldRule(("float",)),
        },
    ),
    "MediaAreaChannelPost": ProfileConstructorRule(
        0x770416AF,
        {
            "coordinates": ProfileFieldRule(("MediaAreaCoordinates",)),
            "channel_id": ProfileFieldRule(("int",)),
            "msg_id": ProfileFieldRule(("int",)),
        },
    ),
    "MediaAreaCoordinates": ProfileConstructorRule(
        0xCFC9E002,
        {
            "x": ProfileFieldRule(("float",)),
            "y": ProfileFieldRule(("float",)),
            "w": ProfileFieldRule(("float",)),
            "h": ProfileFieldRule(("float",)),
            "rotation": ProfileFieldRule(("float",)),
            "radius": ProfileFieldRule(("float",), optional=True),
        },
    ),
    "MediaAreaGeoPoint": ProfileConstructorRule(
        0xCAD5452D,
        {
            "coordinates": ProfileFieldRule(("MediaAreaCoordinates",)),
            "geo": ProfileFieldRule(TYPES_GEO_POINT),
            "address": ProfileFieldRule(("GeoPointAddress",), optional=True),
        },
    ),
    "MediaAreaStarGift": ProfileConstructorRule(
        0x5787686D,
        {
            "coordinates": ProfileFieldRule(("MediaAreaCoordinates",)),
            "slug": ProfileFieldRule(("str",)),
        },
    ),
    "MediaAreaSuggestedReaction": ProfileConstructorRule(
        0x14455871,
        {
            "coordinates": ProfileFieldRule(("MediaAreaCoordinates",)),
            "reaction": ProfileFieldRule(TYPES_REACTION),
            "dark": ProfileFieldRule(("bool",), optional=True, flag=True),
            "flipped": ProfileFieldRule(("bool",), optional=True, flag=True),
        },
    ),
    "MediaAreaUrl": ProfileConstructorRule(
        0x37381085,
        {
            "coordinates": ProfileFieldRule(("MediaAreaCoordinates",)),
            "url": ProfileFieldRule(("str",)),
        },
    ),
    "MediaAreaVenue": ProfileConstructorRule(
        0xBE82DB9C,
        {
            "coordinates": ProfileFieldRule(("MediaAreaCoordinates",)),
            "geo": ProfileFieldRule(TYPES_GEO_POINT),
            "title": ProfileFieldRule(("str",)),
            "address": ProfileFieldRule(("str",)),
            "provider": ProfileFieldRule(("str",)),
            "venue_id": ProfileFieldRule(("str",)),
            "venue_type": ProfileFieldRule(("str",)),
        },
    ),
    "MediaAreaWeather": ProfileConstructorRule(
        0x49A6549C,
        {
            "coordinates": ProfileFieldRule(("MediaAreaCoordinates",)),
            "emoji": ProfileFieldRule(("str",)),
            "temperature_c": ProfileFieldRule(("float",)),
            "color": ProfileFieldRule(("int",)),
        },
    ),
    "MessageEntityBankCard": ProfileConstructorRule(
        0x761E6AF4,
        {
            "offset": ProfileFieldRule(("int",)),
            "length": ProfileFieldRule(("int",)),
        },
    ),
    "MessageEntityBlockquote": ProfileConstructorRule(
        0xF1CCAAAC,
        {
            "offset": ProfileFieldRule(("int",)),
            "length": ProfileFieldRule(("int",)),
            "collapsed": ProfileFieldRule(("bool",), optional=True, flag=True),
        },
    ),
    "MessageEntityBold": ProfileConstructorRule(
        0xBD610BC9,
        {
            "offset": ProfileFieldRule(("int",)),
            "length": ProfileFieldRule(("int",)),
        },
    ),
    "MessageEntityBotCommand": ProfileConstructorRule(
        0x6CEF8AC7,
        {
            "offset": ProfileFieldRule(("int",)),
            "length": ProfileFieldRule(("int",)),
        },
    ),
    "MessageEntityCashtag": ProfileConstructorRule(
        0x4C4E743F,
        {
            "offset": ProfileFieldRule(("int",)),
            "length": ProfileFieldRule(("int",)),
        },
    ),
    "MessageEntityCode": ProfileConstructorRule(
        0x28A20571,
        {
            "offset": ProfileFieldRule(("int",)),
            "length": ProfileFieldRule(("int",)),
        },
    ),
    "MessageEntityCustomEmoji": ProfileConstructorRule(
        0xC8CF05F8,
        {
            "offset": ProfileFieldRule(("int",)),
            "length": ProfileFieldRule(("int",)),
            "document_id": ProfileFieldRule(("int",)),
        },
    ),
    "MessageEntityDiffDelete": ProfileConstructorRule(
        0x0652C1C5,
        {
            "offset": ProfileFieldRule(("int",)),
            "length": ProfileFieldRule(("int",)),
        },
    ),
    "MessageEntityDiffInsert": ProfileConstructorRule(
        0x71777116,
        {
            "offset": ProfileFieldRule(("int",)),
            "length": ProfileFieldRule(("int",)),
        },
    ),
    "MessageEntityDiffReplace": ProfileConstructorRule(
        0xC6C1E5A7,
        {
            "offset": ProfileFieldRule(("int",)),
            "length": ProfileFieldRule(("int",)),
            "old_text": ProfileFieldRule(("str",)),
        },
    ),
    "MessageEntityEmail": ProfileConstructorRule(
        0x64E475C2,
        {
            "offset": ProfileFieldRule(("int",)),
            "length": ProfileFieldRule(("int",)),
        },
    ),
    "MessageEntityFormattedDate": ProfileConstructorRule(
        0x904AC7C7,
        {
            "offset": ProfileFieldRule(("int",)),
            "length": ProfileFieldRule(("int",)),
            "date": ProfileFieldRule(("datetime",), optional=True),
            "relative": ProfileFieldRule(("bool",), optional=True, flag=True),
            "short_time": ProfileFieldRule(("bool",), optional=True, flag=True),
            "long_time": ProfileFieldRule(("bool",), optional=True, flag=True),
            "short_date": ProfileFieldRule(("bool",), optional=True, flag=True),
            "long_date": ProfileFieldRule(("bool",), optional=True, flag=True),
            "day_of_week": ProfileFieldRule(("bool",), optional=True, flag=True),
        },
    ),
    "MessageEntityHashtag": ProfileConstructorRule(
        0x6F635B0D,
        {
            "offset": ProfileFieldRule(("int",)),
            "length": ProfileFieldRule(("int",)),
        },
    ),
    "MessageEntityItalic": ProfileConstructorRule(
        0x826F8B60,
        {
            "offset": ProfileFieldRule(("int",)),
            "length": ProfileFieldRule(("int",)),
        },
    ),
    "MessageEntityMention": ProfileConstructorRule(
        0xFA04579D,
        {
            "offset": ProfileFieldRule(("int",)),
            "length": ProfileFieldRule(("int",)),
        },
    ),
    "MessageEntityMentionName": ProfileConstructorRule(
        0xDC7B1140,
        {
            "offset": ProfileFieldRule(("int",)),
            "length": ProfileFieldRule(("int",)),
            "user_id": ProfileFieldRule(("int",)),
        },
    ),
    "MessageEntityPhone": ProfileConstructorRule(
        0x9B69E34B,
        {
            "offset": ProfileFieldRule(("int",)),
            "length": ProfileFieldRule(("int",)),
        },
    ),
    "MessageEntityPre": ProfileConstructorRule(
        0x73924BE0,
        {
            "offset": ProfileFieldRule(("int",)),
            "length": ProfileFieldRule(("int",)),
            "language": ProfileFieldRule(("str",)),
        },
    ),
    "MessageEntitySpoiler": ProfileConstructorRule(
        0x32CA960F,
        {
            "offset": ProfileFieldRule(("int",)),
            "length": ProfileFieldRule(("int",)),
        },
    ),
    "MessageEntityStrike": ProfileConstructorRule(
        0xBF0693D4,
        {
            "offset": ProfileFieldRule(("int",)),
            "length": ProfileFieldRule(("int",)),
        },
    ),
    "MessageEntityTextUrl": ProfileConstructorRule(
        0x76A6D327,
        {
            "offset": ProfileFieldRule(("int",)),
            "length": ProfileFieldRule(("int",)),
            "url": ProfileFieldRule(("str",)),
        },
    ),
    "MessageEntityUnderline": ProfileConstructorRule(
        0x9C4E7E8B,
        {
            "offset": ProfileFieldRule(("int",)),
            "length": ProfileFieldRule(("int",)),
        },
    ),
    "MessageEntityUnknown": ProfileConstructorRule(
        0xBB92BA95,
        {
            "offset": ProfileFieldRule(("int",)),
            "length": ProfileFieldRule(("int",)),
        },
    ),
    "MessageEntityUrl": ProfileConstructorRule(
        0x6ED02538,
        {
            "offset": ProfileFieldRule(("int",)),
            "length": ProfileFieldRule(("int",)),
        },
    ),
    "MessageExtendedMedia": ProfileConstructorRule(
        0xEE479C64,
        {
            "media": ProfileFieldRule(TYPES_MESSAGE_MEDIA),
        },
    ),
    "MessageExtendedMediaPreview": ProfileConstructorRule(
        0xAD628CC8,
        {
            "w": ProfileFieldRule(("int",), optional=True),
            "h": ProfileFieldRule(("int",), optional=True),
            "thumb": ProfileFieldRule(TYPES_PHOTO_SIZE, optional=True),
            "video_duration": ProfileFieldRule(("int",), optional=True),
        },
    ),
    "MessageMediaContact": ProfileConstructorRule(
        0x70322949,
        {
            "phone_number": ProfileFieldRule(("str",)),
            "first_name": ProfileFieldRule(("str",)),
            "last_name": ProfileFieldRule(("str",)),
            "vcard": ProfileFieldRule(("str",)),
            "user_id": ProfileFieldRule(("int",)),
        },
    ),
    "MessageMediaDice": ProfileConstructorRule(
        0x08CBEC07,
        {
            "value": ProfileFieldRule(("int",)),
            "emoticon": ProfileFieldRule(("str",)),
            "game_outcome": ProfileFieldRule(("messages.EmojiGameOutcome",), optional=True),
        },
    ),
    "MessageMediaDocument": ProfileConstructorRule(
        0x52D8CCD9,
        {
            "nopremium": ProfileFieldRule(("bool",), optional=True, flag=True),
            "spoiler": ProfileFieldRule(("bool",), optional=True, flag=True),
            "video": ProfileFieldRule(("bool",), optional=True, flag=True),
            "round": ProfileFieldRule(("bool",), optional=True, flag=True),
            "voice": ProfileFieldRule(("bool",), optional=True, flag=True),
            "document": ProfileFieldRule(TYPES_DOCUMENT, optional=True),
            "alt_documents": ProfileFieldRule(TYPES_DOCUMENT, optional=True, vector=True),
            "video_cover": ProfileFieldRule(TYPES_PHOTO, optional=True),
            "video_timestamp": ProfileFieldRule(("int",), optional=True),
            "ttl_seconds": ProfileFieldRule(("int",), optional=True),
        },
    ),
    "MessageMediaEmpty": ProfileConstructorRule(0x3DED6320, {}),
    "MessageMediaGame": ProfileConstructorRule(
        0xFDB19008,
        {
            "game": ProfileFieldRule(("Game",)),
        },
    ),
    "MessageMediaGeo": ProfileConstructorRule(
        0x56E0D474,
        {
            "geo": ProfileFieldRule(TYPES_GEO_POINT),
        },
    ),
    "MessageMediaGeoLive": ProfileConstructorRule(
        0xB940C666,
        {
            "geo": ProfileFieldRule(TYPES_GEO_POINT),
            "period": ProfileFieldRule(("int",)),
            "heading": ProfileFieldRule(("int",), optional=True),
            "proximity_notification_radius": ProfileFieldRule(("int",), optional=True),
        },
    ),
    "MessageMediaGiveaway": ProfileConstructorRule(
        0xAA073BEB,
        {
            "channels": ProfileFieldRule(("int",), vector=True),
            "quantity": ProfileFieldRule(("int",)),
            "until_date": ProfileFieldRule(("datetime",), optional=True),
            "only_new_subscribers": ProfileFieldRule(("bool",), optional=True, flag=True),
            "winners_are_visible": ProfileFieldRule(("bool",), optional=True, flag=True),
            "countries_iso2": ProfileFieldRule(("str",), optional=True, vector=True),
            "prize_description": ProfileFieldRule(("str",), optional=True),
            "months": ProfileFieldRule(("int",), optional=True),
            "stars": ProfileFieldRule(("int",), optional=True),
        },
    ),
    "MessageMediaGiveawayResults": ProfileConstructorRule(
        0xCEAA3EA1,
        {
            "channel_id": ProfileFieldRule(("int",)),
            "launch_msg_id": ProfileFieldRule(("int",)),
            "winners_count": ProfileFieldRule(("int",)),
            "unclaimed_count": ProfileFieldRule(("int",)),
            "winners": ProfileFieldRule(("int",), vector=True),
            "until_date": ProfileFieldRule(("datetime",), optional=True),
            "only_new_subscribers": ProfileFieldRule(("bool",), optional=True, flag=True),
            "refunded": ProfileFieldRule(("bool",), optional=True, flag=True),
            "additional_peers_count": ProfileFieldRule(("int",), optional=True),
            "months": ProfileFieldRule(("int",), optional=True),
            "stars": ProfileFieldRule(("int",), optional=True),
            "prize_description": ProfileFieldRule(("str",), optional=True),
        },
    ),
    "MessageMediaInvoice": ProfileConstructorRule(
        0xF6A548D3,
        {
            "title": ProfileFieldRule(("str",)),
            "description": ProfileFieldRule(("str",)),
            "currency": ProfileFieldRule(("str",)),
            "total_amount": ProfileFieldRule(("int",)),
            "start_param": ProfileFieldRule(("str",)),
            "shipping_address_requested": ProfileFieldRule(("bool",), optional=True, flag=True),
            "test": ProfileFieldRule(("bool",), optional=True, flag=True),
            "photo": ProfileFieldRule(("WebDocument", "WebDocumentNoProxy"), optional=True),
            "receipt_msg_id": ProfileFieldRule(("int",), optional=True),
            "extended_media": ProfileFieldRule(TYPES_MESSAGE_EXTENDED_MEDIA, optional=True),
        },
    ),
    "MessageMediaPaidMedia": ProfileConstructorRule(
        0xA8852491,
        {
            "stars_amount": ProfileFieldRule(("int",)),
            "extended_media": ProfileFieldRule(TYPES_MESSAGE_EXTENDED_MEDIA, vector=True),
        },
    ),
    "MessageMediaPhoto": ProfileConstructorRule(
        0xE216EB63,
        {
            "spoiler": ProfileFieldRule(("bool",), optional=True, flag=True),
            "live_photo": ProfileFieldRule(("bool",), optional=True, flag=True),
            "photo": ProfileFieldRule(TYPES_PHOTO, optional=True),
            "ttl_seconds": ProfileFieldRule(("int",), optional=True),
            "video": ProfileFieldRule(TYPES_DOCUMENT, optional=True),
        },
    ),
    "MessageMediaPoll": ProfileConstructorRule(
        0x773F4E66,
        {
            "poll": ProfileFieldRule(("Poll",)),
            "results": ProfileFieldRule(("PollResults",)),
            "attached_media": ProfileFieldRule(TYPES_MESSAGE_MEDIA, optional=True),
        },
    ),
    "MessageMediaStory": ProfileConstructorRule(
        0x68CB6283,
        {
            "peer": ProfileFieldRule(TYPES_PEER),
            "id": ProfileFieldRule(("int",)),
            "via_mention": ProfileFieldRule(("bool",), optional=True, flag=True),
            "story": ProfileFieldRule(TYPES_STORY_ITEM, optional=True),
        },
    ),
    "MessageMediaToDo": ProfileConstructorRule(
        0x8A53B014,
        {
            "todo": ProfileFieldRule(("TodoList",)),
            "completions": ProfileFieldRule(("TodoCompletion",), optional=True, vector=True),
        },
    ),
    "MessageMediaUnsupported": ProfileConstructorRule(0x9F84F49E, {}),
    "MessageMediaVenue": ProfileConstructorRule(
        0x2EC0533F,
        {
            "geo": ProfileFieldRule(TYPES_GEO_POINT),
            "title": ProfileFieldRule(("str",)),
            "address": ProfileFieldRule(("str",)),
            "provider": ProfileFieldRule(("str",)),
            "venue_id": ProfileFieldRule(("str",)),
            "venue_type": ProfileFieldRule(("str",)),
        },
    ),
    "MessageMediaVideoStream": ProfileConstructorRule(
        0xCA5CAB89,
        {
            "call": ProfileFieldRule(("InputGroupCall", "InputGroupCallSlug", "InputGroupCallInviteMessage")),
            "rtmp_stream": ProfileFieldRule(("bool",), optional=True, flag=True),
        },
    ),
    "MessageMediaWebPage": ProfileConstructorRule(
        0xDDF10C3B,
        {
            "webpage": ProfileFieldRule(("WebPageEmpty", "WebPagePending", "WebPage", "WebPageNotModified")),
            "force_large_media": ProfileFieldRule(("bool",), optional=True, flag=True),
            "force_small_media": ProfileFieldRule(("bool",), optional=True, flag=True),
            "manual": ProfileFieldRule(("bool",), optional=True, flag=True),
            "safe": ProfileFieldRule(("bool",), optional=True, flag=True),
        },
    ),
    "NotificationSoundDefault": ProfileConstructorRule(0x97E8BEBE, {}),
    "NotificationSoundLocal": ProfileConstructorRule(
        0x830B9AE4,
        {
            "title": ProfileFieldRule(("str",)),
            "data": ProfileFieldRule(("str",)),
        },
    ),
    "NotificationSoundNone": ProfileConstructorRule(0x6F0C34DF, {}),
    "NotificationSoundRingtone": ProfileConstructorRule(
        0xFF6C8049,
        {
            "id": ProfileFieldRule(("int",)),
        },
    ),
    "Page": ProfileConstructorRule(
        0x98657F0D,
        {
            "url": ProfileFieldRule(("str",)),
            "blocks": ProfileFieldRule(TYPES_PAGE_BLOCK, vector=True),
            "photos": ProfileFieldRule(TYPES_PHOTO, vector=True),
            "documents": ProfileFieldRule(TYPES_DOCUMENT, vector=True),
            "part": ProfileFieldRule(("bool",), optional=True, flag=True),
            "rtl": ProfileFieldRule(("bool",), optional=True, flag=True),
            "v2": ProfileFieldRule(("bool",), optional=True, flag=True),
            "views": ProfileFieldRule(("int",), optional=True),
        },
    ),
    "PageBlockAnchor": ProfileConstructorRule(
        0xCE0D37B0,
        {
            "name": ProfileFieldRule(("str",)),
        },
    ),
    "PageBlockAudio": ProfileConstructorRule(
        0x804361EA,
        {
            "audio_id": ProfileFieldRule(("int",)),
            "caption": ProfileFieldRule(("PageCaption",)),
        },
    ),
    "PageBlockAuthorDate": ProfileConstructorRule(
        0xBAAFE5E0,
        {
            "author": ProfileFieldRule(TYPES_RICH_TEXT),
            "published_date": ProfileFieldRule(("datetime",), optional=True),
        },
    ),
    "PageBlockBlockquote": ProfileConstructorRule(
        0x66D1670B,
        {
            "text": ProfileFieldRule(TYPES_RICH_TEXT),
            "caption": ProfileFieldRule(TYPES_RICH_TEXT),
            "collapsed": ProfileFieldRule(("bool",), optional=True, flag=True),
        },
    ),
    "PageBlockBlockquoteBlocks": ProfileConstructorRule(
        0x0E6E47C4,
        {
            "blocks": ProfileFieldRule(TYPES_PAGE_BLOCK, vector=True),
            "caption": ProfileFieldRule(TYPES_RICH_TEXT),
        },
    ),
    "PageBlockButtonRow": ProfileConstructorRule(
        0x6D640318,
        {
            "buttons": ProfileFieldRule(("PageButton",), vector=True),
            "align_left": ProfileFieldRule(("bool",), optional=True, flag=True),
            "align_center": ProfileFieldRule(("bool",), optional=True, flag=True),
            "align_right": ProfileFieldRule(("bool",), optional=True, flag=True),
        },
    ),
    "PageBlockChannel": ProfileConstructorRule(
        0xEF1751B5,
        {
            "channel": ProfileFieldRule(
                ("ChatEmpty", "Chat", "ChatForbidden", "Channel", "ChannelForbidden", "CommunityForbidden", "Community")
            ),
        },
    ),
    "PageBlockCollage": ProfileConstructorRule(
        0x65A0FA4D,
        {
            "items": ProfileFieldRule(TYPES_PAGE_BLOCK, vector=True),
            "caption": ProfileFieldRule(("PageCaption",)),
        },
    ),
    "PageBlockCover": ProfileConstructorRule(
        0x39F23300,
        {
            "cover": ProfileFieldRule(TYPES_PAGE_BLOCK),
        },
    ),
    "PageBlockDetails": ProfileConstructorRule(
        0x76768BED,
        {
            "blocks": ProfileFieldRule(TYPES_PAGE_BLOCK, vector=True),
            "title": ProfileFieldRule(TYPES_RICH_TEXT),
            "open": ProfileFieldRule(("bool",), optional=True, flag=True),
        },
    ),
    "PageBlockDivider": ProfileConstructorRule(0xDB20B188, {}),
    "PageBlockDocument": ProfileConstructorRule(
        0x38FA3BA3,
        {
            "document_id": ProfileFieldRule(("int",)),
            "caption": ProfileFieldRule(("PageCaption",)),
        },
    ),
    "PageBlockEmbed": ProfileConstructorRule(
        0xA8718DC5,
        {
            "caption": ProfileFieldRule(("PageCaption",)),
            "full_width": ProfileFieldRule(("bool",), optional=True, flag=True),
            "allow_scrolling": ProfileFieldRule(("bool",), optional=True, flag=True),
            "url": ProfileFieldRule(("str",), optional=True),
            "html": ProfileFieldRule(("str",), optional=True),
            "poster_photo_id": ProfileFieldRule(("int",), optional=True),
            "w": ProfileFieldRule(("int",), optional=True),
            "h": ProfileFieldRule(("int",), optional=True),
        },
    ),
    "PageBlockEmbedPost": ProfileConstructorRule(
        0xF259A80B,
        {
            "url": ProfileFieldRule(("str",)),
            "webpage_id": ProfileFieldRule(("int",)),
            "author_photo_id": ProfileFieldRule(("int",)),
            "author": ProfileFieldRule(("str",)),
            "date": ProfileFieldRule(("datetime",), optional=True),
            "blocks": ProfileFieldRule(TYPES_PAGE_BLOCK, vector=True),
            "caption": ProfileFieldRule(("PageCaption",)),
        },
    ),
    "PageBlockFooter": ProfileConstructorRule(
        0x48870999,
        {
            "text": ProfileFieldRule(TYPES_RICH_TEXT),
        },
    ),
    "PageBlockHeader": ProfileConstructorRule(
        0xBFD064EC,
        {
            "text": ProfileFieldRule(TYPES_RICH_TEXT),
        },
    ),
    "PageBlockHeading1": ProfileConstructorRule(
        0xBAFF072F,
        {
            "text": ProfileFieldRule(TYPES_RICH_TEXT),
        },
    ),
    "PageBlockHeading2": ProfileConstructorRule(
        0x096B2AEC,
        {
            "text": ProfileFieldRule(TYPES_RICH_TEXT),
        },
    ),
    "PageBlockHeading3": ProfileConstructorRule(
        0x67E731AD,
        {
            "text": ProfileFieldRule(TYPES_RICH_TEXT),
        },
    ),
    "PageBlockHeading4": ProfileConstructorRule(
        0xB532772B,
        {
            "text": ProfileFieldRule(TYPES_RICH_TEXT),
        },
    ),
    "PageBlockHeading5": ProfileConstructorRule(
        0xDBBE6C6A,
        {
            "text": ProfileFieldRule(TYPES_RICH_TEXT),
        },
    ),
    "PageBlockHeading6": ProfileConstructorRule(
        0x682A41A9,
        {
            "text": ProfileFieldRule(TYPES_RICH_TEXT),
        },
    ),
    "PageBlockKicker": ProfileConstructorRule(
        0x1E148390,
        {
            "text": ProfileFieldRule(TYPES_RICH_TEXT),
        },
    ),
    "PageBlockList": ProfileConstructorRule(
        0xE4E88011,
        {
            "items": ProfileFieldRule(("PageListItemText", "PageListItemBlocks"), vector=True),
        },
    ),
    "PageBlockMap": ProfileConstructorRule(
        0xA44F3EF6,
        {
            "geo": ProfileFieldRule(TYPES_GEO_POINT),
            "zoom": ProfileFieldRule(("int",)),
            "w": ProfileFieldRule(("int",)),
            "h": ProfileFieldRule(("int",)),
            "caption": ProfileFieldRule(("PageCaption",)),
        },
    ),
    "PageBlockMath": ProfileConstructorRule(
        0x59080C20,
        {
            "source": ProfileFieldRule(("str",)),
        },
    ),
    "PageBlockOrderedList": ProfileConstructorRule(
        0x1FD6F6C1,
        {
            "items": ProfileFieldRule(("PageListOrderedItemText", "PageListOrderedItemBlocks"), vector=True),
            "reversed": ProfileFieldRule(("bool",), optional=True, flag=True),
            "start": ProfileFieldRule(("int",), optional=True),
            "type": ProfileFieldRule(("str",), optional=True),
        },
    ),
    "PageBlockParagraph": ProfileConstructorRule(
        0x467A0766,
        {
            "text": ProfileFieldRule(TYPES_RICH_TEXT),
        },
    ),
    "PageBlockPhoto": ProfileConstructorRule(
        0x1759C560,
        {
            "photo_id": ProfileFieldRule(("int",)),
            "caption": ProfileFieldRule(("PageCaption",)),
            "spoiler": ProfileFieldRule(("bool",), optional=True, flag=True),
            "url": ProfileFieldRule(("str",), optional=True),
            "webpage_id": ProfileFieldRule(("int",), optional=True),
        },
    ),
    "PageBlockPreformatted": ProfileConstructorRule(
        0xC070D93E,
        {
            "text": ProfileFieldRule(TYPES_RICH_TEXT),
            "language": ProfileFieldRule(("str",)),
        },
    ),
    "PageBlockPullquote": ProfileConstructorRule(
        0x4F4456D3,
        {
            "text": ProfileFieldRule(TYPES_RICH_TEXT),
            "caption": ProfileFieldRule(TYPES_RICH_TEXT),
        },
    ),
    "PageBlockRelatedArticles": ProfileConstructorRule(
        0x16115A96,
        {
            "title": ProfileFieldRule(TYPES_RICH_TEXT),
            "articles": ProfileFieldRule(("PageRelatedArticle",), vector=True),
        },
    ),
    "PageBlockSlideshow": ProfileConstructorRule(
        0x031F9590,
        {
            "items": ProfileFieldRule(TYPES_PAGE_BLOCK, vector=True),
            "caption": ProfileFieldRule(("PageCaption",)),
        },
    ),
    "PageBlockSubheader": ProfileConstructorRule(
        0xF12BB6E1,
        {
            "text": ProfileFieldRule(TYPES_RICH_TEXT),
        },
    ),
    "PageBlockSubtitle": ProfileConstructorRule(
        0x8FFA9A1F,
        {
            "text": ProfileFieldRule(TYPES_RICH_TEXT),
        },
    ),
    "PageBlockTable": ProfileConstructorRule(
        0xBF4DEA82,
        {
            "title": ProfileFieldRule(TYPES_RICH_TEXT),
            "rows": ProfileFieldRule(("PageTableRow",), vector=True),
            "bordered": ProfileFieldRule(("bool",), optional=True, flag=True),
            "striped": ProfileFieldRule(("bool",), optional=True, flag=True),
            "compact": ProfileFieldRule(("bool",), optional=True, flag=True),
        },
    ),
    "PageBlockThinking": ProfileConstructorRule(
        0x3C29A3E2,
        {
            "text": ProfileFieldRule(TYPES_RICH_TEXT),
        },
    ),
    "PageBlockTitle": ProfileConstructorRule(
        0x70ABC3FD,
        {
            "text": ProfileFieldRule(TYPES_RICH_TEXT),
        },
    ),
    "PageBlockUnsupported": ProfileConstructorRule(0x13567E8A, {}),
    "PageBlockVideo": ProfileConstructorRule(
        0x7C8FE7B6,
        {
            "video_id": ProfileFieldRule(("int",)),
            "caption": ProfileFieldRule(("PageCaption",)),
            "autoplay": ProfileFieldRule(("bool",), optional=True, flag=True),
            "loop": ProfileFieldRule(("bool",), optional=True, flag=True),
            "spoiler": ProfileFieldRule(("bool",), optional=True, flag=True),
        },
    ),
    "PageButton": ProfileConstructorRule(
        0x692A5488,
        {
            "text": ProfileFieldRule(TYPES_RICH_TEXT),
            "type": ProfileFieldRule(TYPES_INLINE_BUTTON_TYPE),
            "style": ProfileFieldRule(("RichButtonStyle",), optional=True),
        },
    ),
    "PageCaption": ProfileConstructorRule(
        0x6F747657,
        {
            "text": ProfileFieldRule(TYPES_RICH_TEXT),
            "credit": ProfileFieldRule(TYPES_RICH_TEXT),
        },
    ),
    "PageListItemBlocks": ProfileConstructorRule(
        0x63CA67AA,
        {
            "blocks": ProfileFieldRule(TYPES_PAGE_BLOCK, vector=True),
            "checkbox": ProfileFieldRule(("bool",), optional=True, flag=True),
            "checked": ProfileFieldRule(("bool",), optional=True, flag=True),
        },
    ),
    "PageListItemText": ProfileConstructorRule(
        0x2F58683C,
        {
            "text": ProfileFieldRule(TYPES_RICH_TEXT),
            "checkbox": ProfileFieldRule(("bool",), optional=True, flag=True),
            "checked": ProfileFieldRule(("bool",), optional=True, flag=True),
        },
    ),
    "PageListOrderedItemBlocks": ProfileConstructorRule(
        0x8FF2D5F0,
        {
            "blocks": ProfileFieldRule(TYPES_PAGE_BLOCK, vector=True),
            "checkbox": ProfileFieldRule(("bool",), optional=True, flag=True),
            "checked": ProfileFieldRule(("bool",), optional=True, flag=True),
            "num": ProfileFieldRule(("str",), optional=True),
            "value": ProfileFieldRule(("int",), optional=True),
            "type": ProfileFieldRule(("str",), optional=True),
        },
    ),
    "PageListOrderedItemText": ProfileConstructorRule(
        0x15031189,
        {
            "text": ProfileFieldRule(TYPES_RICH_TEXT),
            "checkbox": ProfileFieldRule(("bool",), optional=True, flag=True),
            "checked": ProfileFieldRule(("bool",), optional=True, flag=True),
            "num": ProfileFieldRule(("str",), optional=True),
            "value": ProfileFieldRule(("int",), optional=True),
            "type": ProfileFieldRule(("str",), optional=True),
        },
    ),
    "PageRelatedArticle": ProfileConstructorRule(
        0xB390DC08,
        {
            "url": ProfileFieldRule(("str",)),
            "webpage_id": ProfileFieldRule(("int",)),
            "title": ProfileFieldRule(("str",), optional=True),
            "description": ProfileFieldRule(("str",), optional=True),
            "photo_id": ProfileFieldRule(("int",), optional=True),
            "author": ProfileFieldRule(("str",), optional=True),
            "published_date": ProfileFieldRule(("datetime",), optional=True),
        },
    ),
    "PageTableCell": ProfileConstructorRule(
        0x34566B6A,
        {
            "header": ProfileFieldRule(("bool",), optional=True, flag=True),
            "align_center": ProfileFieldRule(("bool",), optional=True, flag=True),
            "align_right": ProfileFieldRule(("bool",), optional=True, flag=True),
            "valign_middle": ProfileFieldRule(("bool",), optional=True, flag=True),
            "valign_bottom": ProfileFieldRule(("bool",), optional=True, flag=True),
            "text": ProfileFieldRule(TYPES_RICH_TEXT, optional=True),
            "colspan": ProfileFieldRule(("int",), optional=True),
            "rowspan": ProfileFieldRule(("int",), optional=True),
        },
    ),
    "PageTableRow": ProfileConstructorRule(
        0xE0C0C5E5,
        {
            "cells": ProfileFieldRule(("PageTableCell",), vector=True),
        },
    ),
    "PeerChannel": ProfileConstructorRule(
        0xA2A5371E,
        {
            "channel_id": ProfileFieldRule(("int",)),
        },
    ),
    "PeerChat": ProfileConstructorRule(
        0x36C6019A,
        {
            "chat_id": ProfileFieldRule(("int",)),
        },
    ),
    "PeerColor": ProfileConstructorRule(
        0xB54B5ACF,
        {
            "color": ProfileFieldRule(("int",), optional=True),
            "background_emoji_id": ProfileFieldRule(("int",), optional=True),
        },
    ),
    "PeerColorCollectible": ProfileConstructorRule(
        0xB9C0639A,
        {
            "collectible_id": ProfileFieldRule(("int",)),
            "gift_emoji_id": ProfileFieldRule(("int",)),
            "background_emoji_id": ProfileFieldRule(("int",)),
            "accent_color": ProfileFieldRule(("int",)),
            "colors": ProfileFieldRule(("int",), vector=True),
            "dark_accent_color": ProfileFieldRule(("int",), optional=True),
            "dark_colors": ProfileFieldRule(("int",), optional=True, vector=True),
        },
    ),
    "PeerNotifySettings": ProfileConstructorRule(
        0x99622C0C,
        {
            "show_previews": ProfileFieldRule(("bool",), classification=ProfileFieldClass.PRIVATE_FACT, optional=True),
            "silent": ProfileFieldRule(("bool",), classification=ProfileFieldClass.PRIVATE_FACT, optional=True),
            "mute_until": ProfileFieldRule(("datetime",), classification=ProfileFieldClass.PRIVATE_FACT, optional=True),
            "ios_sound": ProfileFieldRule(
                TYPES_NOTIFICATION_SOUND, classification=ProfileFieldClass.PRIVATE_FACT, optional=True
            ),
            "android_sound": ProfileFieldRule(
                TYPES_NOTIFICATION_SOUND, classification=ProfileFieldClass.PRIVATE_FACT, optional=True
            ),
            "other_sound": ProfileFieldRule(
                TYPES_NOTIFICATION_SOUND, classification=ProfileFieldClass.PRIVATE_FACT, optional=True
            ),
            "stories_muted": ProfileFieldRule(("bool",), classification=ProfileFieldClass.PRIVATE_FACT, optional=True),
            "stories_hide_sender": ProfileFieldRule(
                ("bool",), classification=ProfileFieldClass.PRIVATE_FACT, optional=True
            ),
            "stories_ios_sound": ProfileFieldRule(
                TYPES_NOTIFICATION_SOUND, classification=ProfileFieldClass.PRIVATE_FACT, optional=True
            ),
            "stories_android_sound": ProfileFieldRule(
                TYPES_NOTIFICATION_SOUND, classification=ProfileFieldClass.PRIVATE_FACT, optional=True
            ),
            "stories_other_sound": ProfileFieldRule(
                TYPES_NOTIFICATION_SOUND, classification=ProfileFieldClass.PRIVATE_FACT, optional=True
            ),
        },
    ),
    "PeerSettings": ProfileConstructorRule(
        0xF47741F7,
        {
            "report_spam": ProfileFieldRule(
                ("bool",), classification=ProfileFieldClass.PRIVATE_FACT, optional=True, flag=True
            ),
            "add_contact": ProfileFieldRule(
                ("bool",), classification=ProfileFieldClass.PRIVATE_FACT, optional=True, flag=True
            ),
            "block_contact": ProfileFieldRule(
                ("bool",), classification=ProfileFieldClass.PRIVATE_FACT, optional=True, flag=True
            ),
            "share_contact": ProfileFieldRule(
                ("bool",), classification=ProfileFieldClass.PRIVATE_FACT, optional=True, flag=True
            ),
            "need_contacts_exception": ProfileFieldRule(
                ("bool",), classification=ProfileFieldClass.PRIVATE_FACT, optional=True, flag=True
            ),
            "report_geo": ProfileFieldRule(
                ("bool",), classification=ProfileFieldClass.PRIVATE_FACT, optional=True, flag=True
            ),
            "autoarchived": ProfileFieldRule(
                ("bool",), classification=ProfileFieldClass.PRIVATE_FACT, optional=True, flag=True
            ),
            "invite_members": ProfileFieldRule(
                ("bool",), classification=ProfileFieldClass.PRIVATE_FACT, optional=True, flag=True
            ),
            "request_chat_broadcast": ProfileFieldRule(
                ("bool",), classification=ProfileFieldClass.PRIVATE_FACT, optional=True, flag=True
            ),
            "business_bot_paused": ProfileFieldRule(
                ("bool",), classification=ProfileFieldClass.PRIVATE_FACT, optional=True, flag=True
            ),
            "business_bot_can_reply": ProfileFieldRule(
                ("bool",), classification=ProfileFieldClass.PRIVATE_FACT, optional=True, flag=True
            ),
            "geo_distance": ProfileFieldRule(("int",), classification=ProfileFieldClass.PRIVATE_FACT, optional=True),
            "request_chat_title": ProfileFieldRule(
                ("str",), classification=ProfileFieldClass.PRIVATE_FACT, optional=True
            ),
            "request_chat_date": ProfileFieldRule(
                ("datetime",), classification=ProfileFieldClass.PRIVATE_FACT, optional=True
            ),
            "business_bot_id": ProfileFieldRule(("int",), classification=ProfileFieldClass.PRIVATE_FACT, optional=True),
            "business_bot_manage_url": ProfileFieldRule(
                ("str",), classification=ProfileFieldClass.PRIVATE_FACT, optional=True
            ),
            "charge_paid_message_stars": ProfileFieldRule(
                ("int",), classification=ProfileFieldClass.PRIVATE_FACT, optional=True
            ),
            "registration_month": ProfileFieldRule(
                ("str",), classification=ProfileFieldClass.PRIVATE_FACT, optional=True
            ),
            "phone_country": ProfileFieldRule(("str",), classification=ProfileFieldClass.PRIVATE_FACT, optional=True),
            "name_change_date": ProfileFieldRule(
                ("datetime",), classification=ProfileFieldClass.PRIVATE_FACT, optional=True
            ),
            "photo_change_date": ProfileFieldRule(
                ("datetime",), classification=ProfileFieldClass.PRIVATE_FACT, optional=True
            ),
        },
    ),
    "PeerStories": ProfileConstructorRule(
        0x9A35E999,
        {
            "peer": ProfileFieldRule(TYPES_PEER),
            "stories": ProfileFieldRule(TYPES_STORY_ITEM, vector=True),
            "max_read_id": ProfileFieldRule(("int",), optional=True),
        },
    ),
    "PeerUser": ProfileConstructorRule(
        0x59511722,
        {
            "user_id": ProfileFieldRule(("int",)),
        },
    ),
    "Photo": ProfileConstructorRule(
        0xFB197A65,
        {
            "id": ProfileFieldRule(("int",)),
            "access_hash": ProfileFieldRule(("int",), classification=ProfileFieldClass.CAPABILITY),
            "file_reference": ProfileFieldRule(("bytes",), classification=ProfileFieldClass.CAPABILITY),
            "date": ProfileFieldRule(("datetime",), optional=True),
            "sizes": ProfileFieldRule(TYPES_PHOTO_SIZE, vector=True),
            "dc_id": ProfileFieldRule(("int",), classification=ProfileFieldClass.CAPABILITY),
            "has_stickers": ProfileFieldRule(("bool",), optional=True, flag=True),
            "video_sizes": ProfileFieldRule(TYPES_VIDEO_SIZE, optional=True, vector=True),
        },
    ),
    "PhotoCachedSize": ProfileConstructorRule(
        0x021E1AD6,
        {
            "type": ProfileFieldRule(("str",)),
            "w": ProfileFieldRule(("int",)),
            "h": ProfileFieldRule(("int",)),
            "bytes": ProfileFieldRule(("bytes",), classification=ProfileFieldClass.CAPABILITY),
        },
    ),
    "PhotoEmpty": ProfileConstructorRule(
        0x2331B22D,
        {
            "id": ProfileFieldRule(("int",)),
        },
    ),
    "PhotoPathSize": ProfileConstructorRule(
        0xD8214D41,
        {
            "type": ProfileFieldRule(("str",)),
            "bytes": ProfileFieldRule(("bytes",), classification=ProfileFieldClass.CAPABILITY),
        },
    ),
    "PhotoSize": ProfileConstructorRule(
        0x75C78E60,
        {
            "type": ProfileFieldRule(("str",)),
            "w": ProfileFieldRule(("int",)),
            "h": ProfileFieldRule(("int",)),
            "size": ProfileFieldRule(("int",)),
        },
    ),
    "PhotoSizeEmpty": ProfileConstructorRule(
        0x0E17E23C,
        {
            "type": ProfileFieldRule(("str",)),
        },
    ),
    "PhotoSizeProgressive": ProfileConstructorRule(
        0xFA3EFB95,
        {
            "type": ProfileFieldRule(("str",)),
            "w": ProfileFieldRule(("int",)),
            "h": ProfileFieldRule(("int",)),
            "sizes": ProfileFieldRule(("int",), vector=True),
        },
    ),
    "PhotoStrippedSize": ProfileConstructorRule(
        0xE0B0BC2E,
        {
            "type": ProfileFieldRule(("str",)),
            "bytes": ProfileFieldRule(("bytes",), classification=ProfileFieldClass.CAPABILITY),
        },
    ),
    "Poll": ProfileConstructorRule(
        0x966E2DBF,
        {
            "id": ProfileFieldRule(("int",)),
            "question": ProfileFieldRule(("TextWithEntities",)),
            "answers": ProfileFieldRule(("PollAnswer", "InputPollAnswer"), vector=True),
            "hash": ProfileFieldRule(("int",)),
            "closed": ProfileFieldRule(("bool",), optional=True, flag=True),
            "public_voters": ProfileFieldRule(("bool",), optional=True, flag=True),
            "multiple_choice": ProfileFieldRule(("bool",), optional=True, flag=True),
            "quiz": ProfileFieldRule(("bool",), optional=True, flag=True),
            "open_answers": ProfileFieldRule(("bool",), optional=True, flag=True),
            "revoting_disabled": ProfileFieldRule(("bool",), optional=True, flag=True),
            "shuffle_answers": ProfileFieldRule(("bool",), optional=True, flag=True),
            "hide_results_until_close": ProfileFieldRule(("bool",), optional=True, flag=True),
            "creator": ProfileFieldRule(("bool",), optional=True, flag=True),
            "subscribers_only": ProfileFieldRule(("bool",), optional=True, flag=True),
            "close_period": ProfileFieldRule(("int",), optional=True),
            "close_date": ProfileFieldRule(("datetime",), optional=True),
            "countries_iso2": ProfileFieldRule(("str",), optional=True, vector=True),
        },
    ),
    "PollAnswer": ProfileConstructorRule(
        0x4B7D786A,
        {
            "text": ProfileFieldRule(("TextWithEntities",)),
            "option": ProfileFieldRule(("bytes",)),
            "media": ProfileFieldRule(TYPES_MESSAGE_MEDIA, optional=True),
            "added_by": ProfileFieldRule(TYPES_PEER, optional=True),
            "date": ProfileFieldRule(("datetime",), optional=True),
        },
    ),
    "PollAnswerVoters": ProfileConstructorRule(
        0x3645230A,
        {
            "option": ProfileFieldRule(("bytes",)),
            "chosen": ProfileFieldRule(("bool",), optional=True, flag=True),
            "correct": ProfileFieldRule(("bool",), optional=True, flag=True),
            "voters": ProfileFieldRule(("int",), optional=True),
            "recent_voters": ProfileFieldRule(TYPES_PEER, optional=True, vector=True),
        },
    ),
    "PollResults": ProfileConstructorRule(
        0xBA7BB15E,
        {
            "min": ProfileFieldRule(("bool",), optional=True, flag=True),
            "has_unread_votes": ProfileFieldRule(("bool",), optional=True, flag=True),
            "can_view_stats": ProfileFieldRule(("bool",), optional=True, flag=True),
            "results": ProfileFieldRule(("PollAnswerVoters",), optional=True, vector=True),
            "total_voters": ProfileFieldRule(("int",), optional=True),
            "recent_voters": ProfileFieldRule(TYPES_PEER, optional=True, vector=True),
            "solution": ProfileFieldRule(("str",), optional=True),
            "solution_entities": ProfileFieldRule(TYPES_MESSAGE_ENTITY, optional=True, vector=True),
            "solution_media": ProfileFieldRule(TYPES_MESSAGE_MEDIA, optional=True),
        },
    ),
    "PrivacyValueAllowAll": ProfileConstructorRule(0x65427B82, {}),
    "PrivacyValueAllowBots": ProfileConstructorRule(0x21461B5D, {}),
    "PrivacyValueAllowChatParticipants": ProfileConstructorRule(
        0x6B134E8E,
        {
            "chats": ProfileFieldRule(("int",), vector=True),
        },
    ),
    "PrivacyValueAllowCloseFriends": ProfileConstructorRule(0xF7E8D89B, {}),
    "PrivacyValueAllowContacts": ProfileConstructorRule(0xFFFE1BAC, {}),
    "PrivacyValueAllowPremium": ProfileConstructorRule(0xECE9814B, {}),
    "PrivacyValueAllowUsers": ProfileConstructorRule(
        0xB8905FB2,
        {
            "users": ProfileFieldRule(("int",), vector=True),
        },
    ),
    "PrivacyValueDisallowAll": ProfileConstructorRule(0x8B73E763, {}),
    "PrivacyValueDisallowBots": ProfileConstructorRule(0xF6A5F82F, {}),
    "PrivacyValueDisallowChatParticipants": ProfileConstructorRule(
        0x41C87565,
        {
            "chats": ProfileFieldRule(("int",), vector=True),
        },
    ),
    "PrivacyValueDisallowContacts": ProfileConstructorRule(0xF888FA1A, {}),
    "PrivacyValueDisallowUsers": ProfileConstructorRule(
        0xE4621141,
        {
            "users": ProfileFieldRule(("int",), vector=True),
        },
    ),
    "ProfileTabFiles": ProfileConstructorRule(0xAB339C00, {}),
    "ProfileTabGifs": ProfileConstructorRule(0xA2C0F695, {}),
    "ProfileTabGifts": ProfileConstructorRule(0x4D4BD46A, {}),
    "ProfileTabLinks": ProfileConstructorRule(0xD3656499, {}),
    "ProfileTabMedia": ProfileConstructorRule(0x72C64955, {}),
    "ProfileTabMusic": ProfileConstructorRule(0x9F27D26E, {}),
    "ProfileTabPosts": ProfileConstructorRule(0xB98CD696, {}),
    "ProfileTabVoice": ProfileConstructorRule(0xE477092E, {}),
    "ReactionCount": ProfileConstructorRule(
        0xA3D1CB80,
        {
            "reaction": ProfileFieldRule(TYPES_REACTION),
            "count": ProfileFieldRule(("int",)),
            "chosen_order": ProfileFieldRule(("int",), optional=True),
        },
    ),
    "ReactionCustomEmoji": ProfileConstructorRule(
        0x8935FC73,
        {
            "document_id": ProfileFieldRule(("int",)),
        },
    ),
    "ReactionEmoji": ProfileConstructorRule(
        0x1B2286B8,
        {
            "emoticon": ProfileFieldRule(("str",)),
        },
    ),
    "ReactionEmpty": ProfileConstructorRule(0x79F5D419, {}),
    "ReactionPaid": ProfileConstructorRule(0x523DA4EB, {}),
    "RecentStory": ProfileConstructorRule(
        0x711D692D,
        {
            "live": ProfileFieldRule(("bool",), optional=True, flag=True),
            "max_id": ProfileFieldRule(("int",), optional=True),
        },
    ),
    "RestrictionReason": ProfileConstructorRule(
        0xD072ACB4,
        {
            "platform": ProfileFieldRule(("str",)),
            "reason": ProfileFieldRule(("str",)),
            "text": ProfileFieldRule(("str",)),
        },
    ),
    "RichButtonStyle": ProfileConstructorRule(
        0x03C610BD,
        {
            "bg_primary": ProfileFieldRule(("bool",), optional=True, flag=True),
            "bg_danger": ProfileFieldRule(("bool",), optional=True, flag=True),
            "bg_success": ProfileFieldRule(("bool",), optional=True, flag=True),
            "link": ProfileFieldRule(("bool",), optional=True, flag=True),
        },
    ),
    "StarGift": ProfileConstructorRule(
        0x313A9547,
        {
            "id": ProfileFieldRule(("int",)),
            "sticker": ProfileFieldRule(TYPES_DOCUMENT),
            "stars": ProfileFieldRule(("int",)),
            "convert_stars": ProfileFieldRule(("int",)),
            "limited": ProfileFieldRule(("bool",), optional=True, flag=True),
            "sold_out": ProfileFieldRule(("bool",), optional=True, flag=True),
            "birthday": ProfileFieldRule(("bool",), optional=True, flag=True),
            "require_premium": ProfileFieldRule(("bool",), optional=True, flag=True),
            "limited_per_user": ProfileFieldRule(("bool",), optional=True, flag=True),
            "peer_color_available": ProfileFieldRule(("bool",), optional=True, flag=True),
            "auction": ProfileFieldRule(("bool",), optional=True, flag=True),
            "availability_remains": ProfileFieldRule(("int",), optional=True),
            "availability_total": ProfileFieldRule(("int",), optional=True),
            "availability_resale": ProfileFieldRule(("int",), optional=True),
            "first_sale_date": ProfileFieldRule(("datetime",), optional=True),
            "last_sale_date": ProfileFieldRule(("datetime",), optional=True),
            "upgrade_stars": ProfileFieldRule(("int",), optional=True),
            "resell_min_stars": ProfileFieldRule(("int",), optional=True),
            "title": ProfileFieldRule(("str",), optional=True),
            "released_by": ProfileFieldRule(TYPES_PEER, optional=True),
            "per_user_total": ProfileFieldRule(("int",), optional=True),
            "per_user_remains": ProfileFieldRule(("int",), optional=True),
            "locked_until_date": ProfileFieldRule(("datetime",), optional=True),
            "auction_slug": ProfileFieldRule(("str",), optional=True),
            "gifts_per_round": ProfileFieldRule(("int",), optional=True),
            "auction_start_date": ProfileFieldRule(("datetime",), optional=True),
            "upgrade_variants": ProfileFieldRule(("int",), optional=True),
            "background": ProfileFieldRule(("StarGiftBackground",), optional=True),
        },
    ),
    "StarGiftAttributeBackdrop": ProfileConstructorRule(
        0x9F2504E4,
        {
            "name": ProfileFieldRule(("str",)),
            "backdrop_id": ProfileFieldRule(("int",)),
            "center_color": ProfileFieldRule(("int",)),
            "edge_color": ProfileFieldRule(("int",)),
            "pattern_color": ProfileFieldRule(("int",)),
            "text_color": ProfileFieldRule(("int",)),
            "rarity": ProfileFieldRule(TYPES_STAR_GIFT_ATTRIBUTE_RARITY),
        },
    ),
    "StarGiftAttributeModel": ProfileConstructorRule(
        0x565251E2,
        {
            "name": ProfileFieldRule(("str",)),
            "document": ProfileFieldRule(TYPES_DOCUMENT),
            "rarity": ProfileFieldRule(TYPES_STAR_GIFT_ATTRIBUTE_RARITY),
            "crafted": ProfileFieldRule(("bool",), optional=True, flag=True),
        },
    ),
    "StarGiftAttributeOriginalDetails": ProfileConstructorRule(
        0xE0BFF26C,
        {
            "recipient_id": ProfileFieldRule(TYPES_PEER),
            "date": ProfileFieldRule(("datetime",), optional=True),
            "sender_id": ProfileFieldRule(TYPES_PEER, optional=True),
            "message": ProfileFieldRule(("TextWithEntities",), optional=True),
        },
    ),
    "StarGiftAttributePattern": ProfileConstructorRule(
        0x4E7085EA,
        {
            "name": ProfileFieldRule(("str",)),
            "document": ProfileFieldRule(TYPES_DOCUMENT),
            "rarity": ProfileFieldRule(TYPES_STAR_GIFT_ATTRIBUTE_RARITY),
        },
    ),
    "StarGiftAttributeRarity": ProfileConstructorRule(
        0x36437737,
        {
            "permille": ProfileFieldRule(("int",)),
        },
    ),
    "StarGiftAttributeRarityEpic": ProfileConstructorRule(0x78FBF3A8, {}),
    "StarGiftAttributeRarityLegendary": ProfileConstructorRule(0xCEF7E7A8, {}),
    "StarGiftAttributeRarityRare": ProfileConstructorRule(0xF08D516B, {}),
    "StarGiftAttributeRarityUncommon": ProfileConstructorRule(0xDBCE6389, {}),
    "StarGiftBackground": ProfileConstructorRule(
        0xAFF56398,
        {
            "center_color": ProfileFieldRule(("int",)),
            "edge_color": ProfileFieldRule(("int",)),
            "text_color": ProfileFieldRule(("int",)),
        },
    ),
    "StarGiftUnique": ProfileConstructorRule(
        0x85F0A9CD,
        {
            "id": ProfileFieldRule(("int",)),
            "gift_id": ProfileFieldRule(("int",)),
            "title": ProfileFieldRule(("str",)),
            "slug": ProfileFieldRule(("str",)),
            "num": ProfileFieldRule(("int",)),
            "attributes": ProfileFieldRule(
                (
                    "StarGiftAttributeModel",
                    "StarGiftAttributePattern",
                    "StarGiftAttributeBackdrop",
                    "StarGiftAttributeOriginalDetails",
                ),
                vector=True,
            ),
            "availability_issued": ProfileFieldRule(("int",)),
            "availability_total": ProfileFieldRule(("int",)),
            "require_premium": ProfileFieldRule(("bool",), optional=True, flag=True),
            "resale_ton_only": ProfileFieldRule(("bool",), optional=True, flag=True),
            "theme_available": ProfileFieldRule(("bool",), optional=True, flag=True),
            "burned": ProfileFieldRule(("bool",), optional=True, flag=True),
            "crafted": ProfileFieldRule(("bool",), optional=True, flag=True),
            "owner_id": ProfileFieldRule(TYPES_PEER, optional=True),
            "owner_name": ProfileFieldRule(("str",), optional=True),
            "owner_address": ProfileFieldRule(("str",), optional=True),
            "gift_address": ProfileFieldRule(("str",), optional=True),
            "resell_amount": ProfileFieldRule(TYPES_STARS_AMOUNT, optional=True, vector=True),
            "released_by": ProfileFieldRule(TYPES_PEER, optional=True),
            "value_amount": ProfileFieldRule(("int",), optional=True),
            "value_currency": ProfileFieldRule(("str",), optional=True),
            "value_usd_amount": ProfileFieldRule(("int",), optional=True),
            "theme_peer": ProfileFieldRule(TYPES_PEER, optional=True),
            "peer_color": ProfileFieldRule(TYPES_PEER_COLOR, optional=True),
            "host_id": ProfileFieldRule(TYPES_PEER, optional=True),
            "offer_min_stars": ProfileFieldRule(("int",), optional=True),
            "craft_chance_permille": ProfileFieldRule(("int",), optional=True),
        },
    ),
    "StarRefProgram": ProfileConstructorRule(
        0xDD0C66F2,
        {
            "bot_id": ProfileFieldRule(("int",)),
            "commission_permille": ProfileFieldRule(("int",)),
            "duration_months": ProfileFieldRule(("int",), optional=True),
            "end_date": ProfileFieldRule(("datetime",), optional=True),
            "daily_revenue_per_user": ProfileFieldRule(TYPES_STARS_AMOUNT, optional=True),
        },
    ),
    "StarsAmount": ProfileConstructorRule(
        0xBBB6B4A3,
        {
            "amount": ProfileFieldRule(("int",)),
            "nanos": ProfileFieldRule(("int",)),
        },
    ),
    "StarsRating": ProfileConstructorRule(
        0x1B0E4F07,
        {
            "level": ProfileFieldRule(("int",)),
            "current_level_stars": ProfileFieldRule(("int",)),
            "stars": ProfileFieldRule(("int",)),
            "next_level_stars": ProfileFieldRule(("int",), optional=True),
        },
    ),
    "StarsTonAmount": ProfileConstructorRule(
        0x74AEE3E0,
        {
            "amount": ProfileFieldRule(("int",)),
        },
    ),
    "StoryFwdHeader": ProfileConstructorRule(
        0xB826E150,
        {
            "modified": ProfileFieldRule(("bool",), optional=True, flag=True),
            "from_": ProfileFieldRule(TYPES_PEER, optional=True),
            "from_name": ProfileFieldRule(("str",), optional=True),
            "story_id": ProfileFieldRule(("int",), optional=True),
        },
    ),
    "StoryItem": ProfileConstructorRule(
        0x16A4B93C,
        {
            "id": ProfileFieldRule(("int",)),
            "date": ProfileFieldRule(("datetime",), optional=True),
            "expire_date": ProfileFieldRule(("datetime",), optional=True),
            "media": ProfileFieldRule(TYPES_MESSAGE_MEDIA),
            "pinned": ProfileFieldRule(("bool",), optional=True, flag=True),
            "public": ProfileFieldRule(("bool",), optional=True, flag=True),
            "close_friends": ProfileFieldRule(("bool",), optional=True, flag=True),
            "min": ProfileFieldRule(("bool",), optional=True, flag=True),
            "noforwards": ProfileFieldRule(("bool",), optional=True, flag=True),
            "edited": ProfileFieldRule(("bool",), optional=True, flag=True),
            "contacts": ProfileFieldRule(("bool",), optional=True, flag=True),
            "selected_contacts": ProfileFieldRule(("bool",), optional=True, flag=True),
            "out": ProfileFieldRule(("bool",), optional=True, flag=True),
            "from_id": ProfileFieldRule(TYPES_PEER, optional=True),
            "fwd_from": ProfileFieldRule(("StoryFwdHeader",), optional=True),
            "caption": ProfileFieldRule(("str",), optional=True),
            "entities": ProfileFieldRule(TYPES_MESSAGE_ENTITY, optional=True, vector=True),
            "media_areas": ProfileFieldRule(
                (
                    "MediaAreaVenue",
                    "InputMediaAreaVenue",
                    "MediaAreaGeoPoint",
                    "MediaAreaSuggestedReaction",
                    "MediaAreaChannelPost",
                    "InputMediaAreaChannelPost",
                    "MediaAreaUrl",
                    "MediaAreaWeather",
                    "MediaAreaStarGift",
                ),
                optional=True,
                vector=True,
            ),
            "privacy": ProfileFieldRule(
                (
                    "PrivacyValueAllowContacts",
                    "PrivacyValueAllowAll",
                    "PrivacyValueAllowUsers",
                    "PrivacyValueDisallowContacts",
                    "PrivacyValueDisallowAll",
                    "PrivacyValueDisallowUsers",
                    "PrivacyValueAllowChatParticipants",
                    "PrivacyValueDisallowChatParticipants",
                    "PrivacyValueAllowCloseFriends",
                    "PrivacyValueAllowPremium",
                    "PrivacyValueAllowBots",
                    "PrivacyValueDisallowBots",
                ),
                optional=True,
                vector=True,
            ),
            "views": ProfileFieldRule(("StoryViews",), optional=True),
            "sent_reaction": ProfileFieldRule(TYPES_REACTION, optional=True),
            "albums": ProfileFieldRule(("int",), optional=True, vector=True),
            "music": ProfileFieldRule(TYPES_DOCUMENT, optional=True),
        },
    ),
    "StoryItemDeleted": ProfileConstructorRule(
        0x51E6EE4F,
        {
            "id": ProfileFieldRule(("int",)),
        },
    ),
    "StoryItemSkipped": ProfileConstructorRule(
        0xFFADC913,
        {
            "id": ProfileFieldRule(("int",)),
            "date": ProfileFieldRule(("datetime",), optional=True),
            "expire_date": ProfileFieldRule(("datetime",), optional=True),
            "close_friends": ProfileFieldRule(("bool",), optional=True, flag=True),
            "live": ProfileFieldRule(("bool",), optional=True, flag=True),
        },
    ),
    "StoryViews": ProfileConstructorRule(
        0x8D595CD6,
        {
            "views_count": ProfileFieldRule(("int",)),
            "has_viewers": ProfileFieldRule(("bool",), optional=True, flag=True),
            "forwards_count": ProfileFieldRule(("int",), optional=True),
            "reactions": ProfileFieldRule(("ReactionCount",), optional=True, vector=True),
            "reactions_count": ProfileFieldRule(("int",), optional=True),
            "recent_viewers": ProfileFieldRule(("int",), optional=True, vector=True),
        },
    ),
    "TextAnchor": ProfileConstructorRule(
        0x35553762,
        {
            "text": ProfileFieldRule(TYPES_RICH_TEXT),
            "name": ProfileFieldRule(("str",)),
        },
    ),
    "TextAutoEmail": ProfileConstructorRule(
        0xC556A45D,
        {
            "text": ProfileFieldRule(TYPES_RICH_TEXT),
        },
    ),
    "TextAutoPhone": ProfileConstructorRule(
        0x24C26789,
        {
            "text": ProfileFieldRule(TYPES_RICH_TEXT),
        },
    ),
    "TextAutoUrl": ProfileConstructorRule(
        0xAC6A83AA,
        {
            "text": ProfileFieldRule(TYPES_RICH_TEXT),
        },
    ),
    "TextBankCard": ProfileConstructorRule(
        0xB956812D,
        {
            "text": ProfileFieldRule(TYPES_RICH_TEXT),
        },
    ),
    "TextBold": ProfileConstructorRule(
        0x6724ABC4,
        {
            "text": ProfileFieldRule(TYPES_RICH_TEXT),
        },
    ),
    "TextBotCommand": ProfileConstructorRule(
        0x02FF29D3,
        {
            "text": ProfileFieldRule(TYPES_RICH_TEXT),
        },
    ),
    "TextButton": ProfileConstructorRule(
        0xAFC79CD6,
        {
            "text": ProfileFieldRule(TYPES_RICH_TEXT),
            "type": ProfileFieldRule(TYPES_INLINE_BUTTON_TYPE),
            "style": ProfileFieldRule(("RichButtonStyle",), optional=True),
        },
    ),
    "TextCashtag": ProfileConstructorRule(
        0x7B9E1801,
        {
            "text": ProfileFieldRule(TYPES_RICH_TEXT),
        },
    ),
    "TextConcat": ProfileConstructorRule(
        0x7E6260D7,
        {
            "texts": ProfileFieldRule(TYPES_RICH_TEXT, vector=True),
        },
    ),
    "TextCustomEmoji": ProfileConstructorRule(
        0xA26156C0,
        {
            "document_id": ProfileFieldRule(("int",)),
            "alt": ProfileFieldRule(("str",)),
        },
    ),
    "TextDate": ProfileConstructorRule(
        0xA5B45E2B,
        {
            "text": ProfileFieldRule(TYPES_RICH_TEXT),
            "date": ProfileFieldRule(("datetime",), optional=True),
            "relative": ProfileFieldRule(("bool",), optional=True, flag=True),
            "short_time": ProfileFieldRule(("bool",), optional=True, flag=True),
            "long_time": ProfileFieldRule(("bool",), optional=True, flag=True),
            "short_date": ProfileFieldRule(("bool",), optional=True, flag=True),
            "long_date": ProfileFieldRule(("bool",), optional=True, flag=True),
            "day_of_week": ProfileFieldRule(("bool",), optional=True, flag=True),
        },
    ),
    "TextDiff": ProfileConstructorRule(
        0x9686CB50,
        {
            "text": ProfileFieldRule(TYPES_RICH_TEXT),
            "old_text": ProfileFieldRule(TYPES_RICH_TEXT),
        },
    ),
    "TextEmail": ProfileConstructorRule(
        0xDE5A0DD6,
        {
            "text": ProfileFieldRule(TYPES_RICH_TEXT),
            "email": ProfileFieldRule(("str",)),
        },
    ),
    "TextEmpty": ProfileConstructorRule(0xDC3D824F, {}),
    "TextFixed": ProfileConstructorRule(
        0x6C3F19B9,
        {
            "text": ProfileFieldRule(TYPES_RICH_TEXT),
        },
    ),
    "TextHashtag": ProfileConstructorRule(
        0x519524EA,
        {
            "text": ProfileFieldRule(TYPES_RICH_TEXT),
        },
    ),
    "TextImage": ProfileConstructorRule(
        0x081CCF4F,
        {
            "document_id": ProfileFieldRule(("int",)),
            "w": ProfileFieldRule(("int",)),
            "h": ProfileFieldRule(("int",)),
        },
    ),
    "TextItalic": ProfileConstructorRule(
        0xD912A59C,
        {
            "text": ProfileFieldRule(TYPES_RICH_TEXT),
        },
    ),
    "TextMarked": ProfileConstructorRule(
        0x034B8621,
        {
            "text": ProfileFieldRule(TYPES_RICH_TEXT),
        },
    ),
    "TextMath": ProfileConstructorRule(
        0x9D2EAC97,
        {
            "source": ProfileFieldRule(("str",)),
        },
    ),
    "TextMention": ProfileConstructorRule(
        0xCD24CF44,
        {
            "text": ProfileFieldRule(TYPES_RICH_TEXT),
        },
    ),
    "TextMentionName": ProfileConstructorRule(
        0x01A9FBFC,
        {
            "text": ProfileFieldRule(TYPES_RICH_TEXT),
            "user_id": ProfileFieldRule(("int",)),
        },
    ),
    "TextPhone": ProfileConstructorRule(
        0x1CCB966A,
        {
            "text": ProfileFieldRule(TYPES_RICH_TEXT),
            "phone": ProfileFieldRule(("str",)),
        },
    ),
    "TextPlain": ProfileConstructorRule(
        0x744694E0,
        {
            "text": ProfileFieldRule(("str",)),
        },
    ),
    "TextSpoiler": ProfileConstructorRule(
        0x4C2A5D62,
        {
            "text": ProfileFieldRule(TYPES_RICH_TEXT),
        },
    ),
    "TextStrike": ProfileConstructorRule(
        0x9BF8BB95,
        {
            "text": ProfileFieldRule(TYPES_RICH_TEXT),
        },
    ),
    "TextSubscript": ProfileConstructorRule(
        0xED6A8504,
        {
            "text": ProfileFieldRule(TYPES_RICH_TEXT),
        },
    ),
    "TextSuperscript": ProfileConstructorRule(
        0xC7FB5E01,
        {
            "text": ProfileFieldRule(TYPES_RICH_TEXT),
        },
    ),
    "TextUnderline": ProfileConstructorRule(
        0xC12622C4,
        {
            "text": ProfileFieldRule(TYPES_RICH_TEXT),
        },
    ),
    "TextUrl": ProfileConstructorRule(
        0x3C2884C1,
        {
            "text": ProfileFieldRule(TYPES_RICH_TEXT),
            "url": ProfileFieldRule(("str",)),
            "webpage_id": ProfileFieldRule(("int",)),
        },
    ),
    "TextWithEntities": ProfileConstructorRule(
        0x751F3146,
        {
            "text": ProfileFieldRule(("str",)),
            "entities": ProfileFieldRule(TYPES_MESSAGE_ENTITY, vector=True),
        },
    ),
    "ThemeSettings": ProfileConstructorRule(
        0xFA58B6D4,
        {
            "base_theme": ProfileFieldRule(
                ("BaseThemeClassic", "BaseThemeDay", "BaseThemeNight", "BaseThemeTinted", "BaseThemeArctic")
            ),
            "accent_color": ProfileFieldRule(("int",)),
            "message_colors_animated": ProfileFieldRule(("bool",), optional=True, flag=True),
            "outbox_accent_color": ProfileFieldRule(("int",), optional=True),
            "message_colors": ProfileFieldRule(("int",), optional=True, vector=True),
            "wallpaper": ProfileFieldRule(TYPES_WALL_PAPER, optional=True),
        },
    ),
    "TodoCompletion": ProfileConstructorRule(
        0x221BB5E4,
        {
            "id": ProfileFieldRule(("int",)),
            "completed_by": ProfileFieldRule(TYPES_PEER),
            "date": ProfileFieldRule(("datetime",), optional=True),
        },
    ),
    "TodoItem": ProfileConstructorRule(
        0xCBA9A52F,
        {
            "id": ProfileFieldRule(("int",)),
            "title": ProfileFieldRule(("TextWithEntities",)),
        },
    ),
    "TodoList": ProfileConstructorRule(
        0x49B92A26,
        {
            "title": ProfileFieldRule(("TextWithEntities",)),
            "list": ProfileFieldRule(("TodoItem",), vector=True),
            "others_can_append": ProfileFieldRule(("bool",), optional=True, flag=True),
            "others_can_complete": ProfileFieldRule(("bool",), optional=True, flag=True),
        },
    ),
    "User": ProfileConstructorRule(
        0xB1B8CC83,
        {
            "id": ProfileFieldRule(("int",)),
            "is_self": ProfileFieldRule(
                ("bool",), classification=ProfileFieldClass.PRIVATE_FACT, optional=True, flag=True
            ),
            "contact": ProfileFieldRule(
                ("bool",), classification=ProfileFieldClass.PRIVATE_FACT, optional=True, flag=True
            ),
            "mutual_contact": ProfileFieldRule(
                ("bool",), classification=ProfileFieldClass.PRIVATE_FACT, optional=True, flag=True
            ),
            "deleted": ProfileFieldRule(("bool",), optional=True, flag=True),
            "bot": ProfileFieldRule(("bool",), optional=True, flag=True),
            "bot_chat_history": ProfileFieldRule(("bool",), optional=True, flag=True),
            "bot_nochats": ProfileFieldRule(("bool",), optional=True, flag=True),
            "verified": ProfileFieldRule(("bool",), optional=True, flag=True),
            "restricted": ProfileFieldRule(("bool",), optional=True, flag=True),
            "min": ProfileFieldRule(("bool",), classification=ProfileFieldClass.CAPABILITY, optional=True, flag=True),
            "bot_inline_geo": ProfileFieldRule(("bool",), optional=True, flag=True),
            "support": ProfileFieldRule(("bool",), optional=True, flag=True),
            "scam": ProfileFieldRule(("bool",), optional=True, flag=True),
            "apply_min_photo": ProfileFieldRule(
                ("bool",), classification=ProfileFieldClass.CAPABILITY, optional=True, flag=True
            ),
            "fake": ProfileFieldRule(("bool",), optional=True, flag=True),
            "bot_attach_menu": ProfileFieldRule(("bool",), optional=True, flag=True),
            "premium": ProfileFieldRule(("bool",), optional=True, flag=True),
            "attach_menu_enabled": ProfileFieldRule(("bool",), optional=True, flag=True),
            "bot_can_edit": ProfileFieldRule(("bool",), optional=True, flag=True),
            "close_friend": ProfileFieldRule(
                ("bool",), classification=ProfileFieldClass.PRIVATE_FACT, optional=True, flag=True
            ),
            "stories_hidden": ProfileFieldRule(
                ("bool",), classification=ProfileFieldClass.PRIVATE_FACT, optional=True, flag=True
            ),
            "stories_unavailable": ProfileFieldRule(("bool",), optional=True, flag=True),
            "contact_require_premium": ProfileFieldRule(("bool",), optional=True, flag=True),
            "bot_business": ProfileFieldRule(("bool",), optional=True, flag=True),
            "bot_has_main_app": ProfileFieldRule(("bool",), optional=True, flag=True),
            "bot_forum_view": ProfileFieldRule(("bool",), optional=True, flag=True),
            "bot_forum_can_manage_topics": ProfileFieldRule(("bool",), optional=True, flag=True),
            "bot_can_manage_bots": ProfileFieldRule(("bool",), optional=True, flag=True),
            "bot_guestchat": ProfileFieldRule(("bool",), optional=True, flag=True),
            "bot_guard": ProfileFieldRule(("bool",), optional=True, flag=True),
            "access_hash": ProfileFieldRule(("int",), classification=ProfileFieldClass.CAPABILITY, optional=True),
            "first_name": ProfileFieldRule(("str",), optional=True, clear_on_none=True),
            "last_name": ProfileFieldRule(("str",), optional=True, clear_on_none=True),
            "username": ProfileFieldRule(("str",), optional=True, clear_on_none=True),
            "phone": ProfileFieldRule(("str",), classification=ProfileFieldClass.PRIVATE_FACT, optional=True),
            "photo": ProfileFieldRule(("UserProfilePhotoEmpty", "UserProfilePhoto"), optional=True, clear_on_none=True),
            "status": ProfileFieldRule(
                (
                    "UserStatusEmpty",
                    "UserStatusOnline",
                    "UserStatusOffline",
                    "UserStatusRecently",
                    "UserStatusLastWeek",
                    "UserStatusLastMonth",
                ),
                optional=True,
            ),
            "bot_info_version": ProfileFieldRule(("int",), optional=True),
            "restriction_reason": ProfileFieldRule(
                ("RestrictionReason",), optional=True, vector=True, clear_on_none=True
            ),
            "bot_inline_placeholder": ProfileFieldRule(("str",), optional=True),
            "lang_code": ProfileFieldRule(("str",), optional=True),
            "emoji_status": ProfileFieldRule(TYPES_EMOJI_STATUS, optional=True, clear_on_none=True),
            "usernames": ProfileFieldRule(("Username",), optional=True, vector=True, clear_on_none=True),
            "stories_max_id": ProfileFieldRule(("RecentStory",), optional=True),
            "color": ProfileFieldRule(TYPES_PEER_COLOR, optional=True),
            "profile_color": ProfileFieldRule(TYPES_PEER_COLOR, optional=True),
            "bot_active_users": ProfileFieldRule(("int",), optional=True),
            "bot_verification_icon": ProfileFieldRule(("int",), optional=True),
            "send_paid_messages_stars": ProfileFieldRule(("int",), optional=True),
            "linked_community_id": ProfileFieldRule(("int",), optional=True),
        },
    ),
    "UserEmpty": ProfileConstructorRule(
        0xD3BC4B7A,
        {
            "id": ProfileFieldRule(("int",)),
        },
    ),
    "UserFull": ProfileConstructorRule(
        0x06CBE645,
        {
            "id": ProfileFieldRule(("int",)),
            "settings": ProfileFieldRule(("PeerSettings",), classification=ProfileFieldClass.PRIVATE_FACT),
            "notify_settings": ProfileFieldRule(("PeerNotifySettings",), classification=ProfileFieldClass.PRIVATE_FACT),
            "common_chats_count": ProfileFieldRule(("int",)),
            "blocked": ProfileFieldRule(
                ("bool",), classification=ProfileFieldClass.PRIVATE_FACT, optional=True, flag=True
            ),
            "phone_calls_available": ProfileFieldRule(("bool",), optional=True, flag=True),
            "phone_calls_private": ProfileFieldRule(
                ("bool",), classification=ProfileFieldClass.PRIVATE_FACT, optional=True, flag=True
            ),
            "can_pin_message": ProfileFieldRule(("bool",), optional=True, flag=True),
            "has_scheduled": ProfileFieldRule(("bool",), optional=True, flag=True),
            "video_calls_available": ProfileFieldRule(("bool",), optional=True, flag=True),
            "voice_messages_forbidden": ProfileFieldRule(
                ("bool",), classification=ProfileFieldClass.PRIVATE_FACT, optional=True, flag=True
            ),
            "translations_disabled": ProfileFieldRule(
                ("bool",), classification=ProfileFieldClass.PRIVATE_FACT, optional=True, flag=True
            ),
            "stories_pinned_available": ProfileFieldRule(("bool",), optional=True, flag=True),
            "blocked_my_stories_from": ProfileFieldRule(
                ("bool",), classification=ProfileFieldClass.PRIVATE_FACT, optional=True, flag=True
            ),
            "wallpaper_overridden": ProfileFieldRule(
                ("bool",), classification=ProfileFieldClass.PRIVATE_FACT, optional=True, flag=True
            ),
            "contact_require_premium": ProfileFieldRule(
                ("bool",), classification=ProfileFieldClass.PRIVATE_FACT, optional=True, flag=True
            ),
            "read_dates_private": ProfileFieldRule(
                ("bool",), classification=ProfileFieldClass.PRIVATE_FACT, optional=True, flag=True
            ),
            "sponsored_enabled": ProfileFieldRule(
                ("bool",), classification=ProfileFieldClass.PRIVATE_FACT, optional=True, flag=True
            ),
            "can_view_revenue": ProfileFieldRule(
                ("bool",), classification=ProfileFieldClass.PRIVATE_FACT, optional=True, flag=True
            ),
            "bot_can_manage_emoji_status": ProfileFieldRule(("bool",), optional=True, flag=True),
            "display_gifts_button": ProfileFieldRule(("bool",), optional=True, flag=True),
            "noforwards_my_enabled": ProfileFieldRule(
                ("bool",), classification=ProfileFieldClass.PRIVATE_FACT, optional=True, flag=True
            ),
            "noforwards_peer_enabled": ProfileFieldRule(
                ("bool",), classification=ProfileFieldClass.PRIVATE_FACT, optional=True, flag=True
            ),
            "unofficial_security_risk": ProfileFieldRule(
                ("bool",), classification=ProfileFieldClass.PRIVATE_FACT, optional=True, flag=True
            ),
            "about": ProfileFieldRule(("str",), optional=True, clear_on_none=True),
            "personal_photo": ProfileFieldRule(
                TYPES_PHOTO, classification=ProfileFieldClass.PRIVATE_FACT, optional=True, clear_on_none=True
            ),
            "profile_photo": ProfileFieldRule(TYPES_PHOTO, optional=True),
            "fallback_photo": ProfileFieldRule(TYPES_PHOTO, optional=True),
            "bot_info": ProfileFieldRule(("BotInfo",), optional=True),
            "pinned_msg_id": ProfileFieldRule(("int",), optional=True),
            "folder_id": ProfileFieldRule(("int",), classification=ProfileFieldClass.PRIVATE_FACT, optional=True),
            "ttl_period": ProfileFieldRule(("int",), classification=ProfileFieldClass.PRIVATE_FACT, optional=True),
            "theme": ProfileFieldRule(("ChatTheme", "ChatThemeUniqueGift"), optional=True),
            "private_forward_name": ProfileFieldRule(
                ("str",), classification=ProfileFieldClass.PRIVATE_FACT, optional=True
            ),
            "bot_group_admin_rights": ProfileFieldRule(("ChatAdminRights",), optional=True),
            "bot_broadcast_admin_rights": ProfileFieldRule(("ChatAdminRights",), optional=True),
            "wallpaper": ProfileFieldRule(TYPES_WALL_PAPER, optional=True),
            "stories": ProfileFieldRule(("PeerStories",), optional=True),
            "business_work_hours": ProfileFieldRule(("BusinessWorkHours",), optional=True),
            "business_location": ProfileFieldRule(("BusinessLocation",), optional=True),
            "business_greeting_message": ProfileFieldRule(
                ("BusinessGreetingMessage",), classification=ProfileFieldClass.PRIVATE_FACT, optional=True
            ),
            "business_away_message": ProfileFieldRule(
                ("BusinessAwayMessage",), classification=ProfileFieldClass.PRIVATE_FACT, optional=True
            ),
            "business_intro": ProfileFieldRule(("BusinessIntro",), optional=True),
            "birthday": ProfileFieldRule(("Birthday",), optional=True),
            "personal_channel_id": ProfileFieldRule(("int",), optional=True, clear_on_none=True),
            "personal_channel_message": ProfileFieldRule(("int",), optional=True),
            "stargifts_count": ProfileFieldRule(("int",), optional=True),
            "starref_program": ProfileFieldRule(("StarRefProgram",), optional=True),
            "bot_verification": ProfileFieldRule(("BotVerification",), optional=True),
            "send_paid_messages_stars": ProfileFieldRule(("int",), optional=True),
            "disallowed_gifts": ProfileFieldRule(("DisallowedGiftsSettings",), optional=True),
            "stars_rating": ProfileFieldRule(("StarsRating",), optional=True),
            "stars_my_pending_rating": ProfileFieldRule(
                ("StarsRating",), classification=ProfileFieldClass.PRIVATE_FACT, optional=True
            ),
            "stars_my_pending_rating_date": ProfileFieldRule(
                ("datetime",), classification=ProfileFieldClass.PRIVATE_FACT, optional=True
            ),
            "main_tab": ProfileFieldRule(
                (
                    "ProfileTabPosts",
                    "ProfileTabGifts",
                    "ProfileTabMedia",
                    "ProfileTabFiles",
                    "ProfileTabMusic",
                    "ProfileTabVoice",
                    "ProfileTabLinks",
                    "ProfileTabGifs",
                ),
                optional=True,
            ),
            "saved_music": ProfileFieldRule(TYPES_DOCUMENT, optional=True),
            "note": ProfileFieldRule(
                ("TextWithEntities",), classification=ProfileFieldClass.PRIVATE_FACT, optional=True, clear_on_none=True
            ),
            "bot_manager_id": ProfileFieldRule(("int",), classification=ProfileFieldClass.PRIVATE_FACT, optional=True),
        },
    ),
    "UserProfilePhoto": ProfileConstructorRule(
        0x82D1F706,
        {
            "photo_id": ProfileFieldRule(("int",)),
            "dc_id": ProfileFieldRule(("int",), classification=ProfileFieldClass.CAPABILITY),
            "has_video": ProfileFieldRule(("bool",), optional=True, flag=True),
            "personal": ProfileFieldRule(("bool",), optional=True, flag=True),
            "stripped_thumb": ProfileFieldRule(("bytes",), optional=True),
        },
    ),
    "UserProfilePhotoEmpty": ProfileConstructorRule(0x4F11BAE1, {}),
    "UserStatusEmpty": ProfileConstructorRule(0x09D05049, {}),
    "UserStatusLastMonth": ProfileConstructorRule(
        0x65899777,
        {
            "by_me": ProfileFieldRule(("bool",), optional=True, flag=True),
        },
    ),
    "UserStatusLastWeek": ProfileConstructorRule(
        0x541A1D1A,
        {
            "by_me": ProfileFieldRule(("bool",), optional=True, flag=True),
        },
    ),
    "UserStatusOffline": ProfileConstructorRule(
        0x008C703F,
        {
            "was_online": ProfileFieldRule(("datetime",), optional=True),
        },
    ),
    "UserStatusOnline": ProfileConstructorRule(
        0xEDB93949,
        {
            "expires": ProfileFieldRule(("datetime",), optional=True),
        },
    ),
    "UserStatusRecently": ProfileConstructorRule(
        0x7B197DC8,
        {
            "by_me": ProfileFieldRule(("bool",), optional=True, flag=True),
        },
    ),
    "Username": ProfileConstructorRule(
        0xB4073647,
        {
            "username": ProfileFieldRule(("str",)),
            "editable": ProfileFieldRule(("bool",), optional=True, flag=True),
            "active": ProfileFieldRule(("bool",), optional=True, flag=True),
        },
    ),
    "VideoSize": ProfileConstructorRule(
        0xDE33B094,
        {
            "type": ProfileFieldRule(("str",)),
            "w": ProfileFieldRule(("int",)),
            "h": ProfileFieldRule(("int",)),
            "size": ProfileFieldRule(("int",)),
            "video_start_ts": ProfileFieldRule(("float",), optional=True),
        },
    ),
    "VideoSizeEmojiMarkup": ProfileConstructorRule(
        0xF85C413C,
        {
            "emoji_id": ProfileFieldRule(("int",)),
            "background_colors": ProfileFieldRule(("int",), vector=True),
        },
    ),
    "VideoSizeStickerMarkup": ProfileConstructorRule(
        0x0DA082FE,
        {
            "stickerset": ProfileFieldRule(TYPES_INPUT_STICKER_SET),
            "sticker_id": ProfileFieldRule(("int",)),
            "background_colors": ProfileFieldRule(("int",), vector=True),
        },
    ),
    "WallPaper": ProfileConstructorRule(
        0xA437C3ED,
        {
            "id": ProfileFieldRule(("int",)),
            "access_hash": ProfileFieldRule(("int",), classification=ProfileFieldClass.CAPABILITY),
            "slug": ProfileFieldRule(("str",)),
            "document": ProfileFieldRule(TYPES_DOCUMENT),
            "creator": ProfileFieldRule(("bool",), optional=True, flag=True),
            "default": ProfileFieldRule(("bool",), optional=True, flag=True),
            "pattern": ProfileFieldRule(("bool",), optional=True, flag=True),
            "dark": ProfileFieldRule(("bool",), optional=True, flag=True),
            "settings": ProfileFieldRule(("WallPaperSettings",), optional=True),
        },
    ),
    "WallPaperNoFile": ProfileConstructorRule(
        0xE0804116,
        {
            "id": ProfileFieldRule(("int",)),
            "default": ProfileFieldRule(("bool",), optional=True, flag=True),
            "dark": ProfileFieldRule(("bool",), optional=True, flag=True),
            "settings": ProfileFieldRule(("WallPaperSettings",), optional=True),
        },
    ),
    "WallPaperSettings": ProfileConstructorRule(
        0x372EFCD0,
        {
            "blur": ProfileFieldRule(("bool",), optional=True, flag=True),
            "motion": ProfileFieldRule(("bool",), optional=True, flag=True),
            "background_color": ProfileFieldRule(("int",), optional=True),
            "second_background_color": ProfileFieldRule(("int",), optional=True),
            "third_background_color": ProfileFieldRule(("int",), optional=True),
            "fourth_background_color": ProfileFieldRule(("int",), optional=True),
            "intensity": ProfileFieldRule(("int",), optional=True),
            "rotation": ProfileFieldRule(("int",), optional=True),
            "emoticon": ProfileFieldRule(("str",), optional=True),
        },
    ),
    "WebDocument": ProfileConstructorRule(
        0x1C570ED1,
        {
            "url": ProfileFieldRule(("str",)),
            "access_hash": ProfileFieldRule(("int",), classification=ProfileFieldClass.CAPABILITY),
            "size": ProfileFieldRule(("int",)),
            "mime_type": ProfileFieldRule(("str",)),
            "attributes": ProfileFieldRule(TYPES_DOCUMENT_ATTRIBUTE, vector=True),
        },
    ),
    "WebDocumentNoProxy": ProfileConstructorRule(
        0xF9C8BCC6,
        {
            "url": ProfileFieldRule(("str",)),
            "size": ProfileFieldRule(("int",)),
            "mime_type": ProfileFieldRule(("str",)),
            "attributes": ProfileFieldRule(TYPES_DOCUMENT_ATTRIBUTE, vector=True),
        },
    ),
    "WebPage": ProfileConstructorRule(
        0xE89C45B2,
        {
            "id": ProfileFieldRule(("int",)),
            "url": ProfileFieldRule(("str",)),
            "display_url": ProfileFieldRule(("str",)),
            "hash": ProfileFieldRule(("int",)),
            "has_large_media": ProfileFieldRule(("bool",), optional=True, flag=True),
            "video_cover_photo": ProfileFieldRule(("bool",), optional=True, flag=True),
            "type": ProfileFieldRule(("str",), optional=True),
            "site_name": ProfileFieldRule(("str",), optional=True),
            "title": ProfileFieldRule(("str",), optional=True),
            "description": ProfileFieldRule(("str",), optional=True),
            "photo": ProfileFieldRule(TYPES_PHOTO, optional=True),
            "embed_url": ProfileFieldRule(("str",), optional=True),
            "embed_type": ProfileFieldRule(("str",), optional=True),
            "embed_width": ProfileFieldRule(("int",), optional=True),
            "embed_height": ProfileFieldRule(("int",), optional=True),
            "duration": ProfileFieldRule(("int",), optional=True),
            "author": ProfileFieldRule(("str",), optional=True),
            "document": ProfileFieldRule(TYPES_DOCUMENT, optional=True),
            "cached_page": ProfileFieldRule(("Page",), optional=True),
            "attributes": ProfileFieldRule(
                (
                    "WebPageAttributeTheme",
                    "WebPageAttributeStory",
                    "WebPageAttributeStickerSet",
                    "WebPageAttributeUniqueStarGift",
                    "WebPageAttributeStarGiftCollection",
                    "WebPageAttributeStarGiftAuction",
                    "WebPageAttributeAiComposeTone",
                ),
                optional=True,
                vector=True,
            ),
        },
    ),
    "WebPageAttributeAiComposeTone": ProfileConstructorRule(
        0x7781FE18,
        {
            "emoji_id": ProfileFieldRule(("int",)),
        },
    ),
    "WebPageAttributeStarGiftAuction": ProfileConstructorRule(
        0x01C641C2,
        {
            "gift": ProfileFieldRule(TYPES_STAR_GIFT),
            "end_date": ProfileFieldRule(("datetime",), optional=True),
        },
    ),
    "WebPageAttributeStarGiftCollection": ProfileConstructorRule(
        0x31CAD303,
        {
            "icons": ProfileFieldRule(TYPES_DOCUMENT, vector=True),
        },
    ),
    "WebPageAttributeStickerSet": ProfileConstructorRule(
        0x50CC03D3,
        {
            "stickers": ProfileFieldRule(TYPES_DOCUMENT, vector=True),
            "emojis": ProfileFieldRule(("bool",), optional=True, flag=True),
            "text_color": ProfileFieldRule(("bool",), optional=True, flag=True),
        },
    ),
    "WebPageAttributeStory": ProfileConstructorRule(
        0x2E94C3E7,
        {
            "peer": ProfileFieldRule(TYPES_PEER),
            "id": ProfileFieldRule(("int",)),
            "story": ProfileFieldRule(TYPES_STORY_ITEM, optional=True),
        },
    ),
    "WebPageAttributeTheme": ProfileConstructorRule(
        0x54B56617,
        {
            "documents": ProfileFieldRule(TYPES_DOCUMENT, optional=True, vector=True),
            "settings": ProfileFieldRule(("ThemeSettings",), optional=True),
        },
    ),
    "WebPageAttributeUniqueStarGift": ProfileConstructorRule(
        0xCF6F6DB8,
        {
            "gift": ProfileFieldRule(TYPES_STAR_GIFT),
        },
    ),
    "WebPageEmpty": ProfileConstructorRule(
        0x211A1788,
        {
            "id": ProfileFieldRule(("int",)),
            "url": ProfileFieldRule(("str",), optional=True),
        },
    ),
    "WebPageNotModified": ProfileConstructorRule(
        0x7311CA11,
        {
            "cached_page_views": ProfileFieldRule(("int",), optional=True),
        },
    ),
    "WebPagePending": ProfileConstructorRule(
        0xB0D13E47,
        {
            "id": ProfileFieldRule(("int",)),
            "date": ProfileFieldRule(("datetime",), optional=True),
            "url": ProfileFieldRule(("str",), optional=True),
        },
    ),
    "messages.EmojiGameOutcome": ProfileConstructorRule(
        0xDA2AD647,
        {
            "seed": ProfileFieldRule(("bytes",)),
            "stake_ton_amount": ProfileFieldRule(("int",)),
            "ton_amount": ProfileFieldRule(("int",)),
        },
    ),
}
