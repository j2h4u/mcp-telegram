"""Canonical dialog-kind filters for recent own-message activity."""

DEFAULT_ACTIVITY_DIALOG_KINDS = ("group", "forum")
_ALLOWED_ACTIVITY_DIALOG_KINDS = {"all", "user", "bot", "group", "forum", "channel", "unknown"}
_ACTIVITY_DIALOG_KIND_ALIASES = {
    "dm": ("user", "bot"),
    "dms": ("user", "bot"),
    "private": ("user", "bot"),
    "personal": ("user", "bot"),
    "direct": ("user", "bot"),
    "groups": ("group", "forum"),
    "supergroup": ("group",),
    "supergroups": ("group",),
    "chat": ("group",),
    "chats": ("group",),
    "forums": ("forum",),
}


def _coerce_activity_dialog_kinds(value: object) -> list[object]:
    if value is None:
        return list(DEFAULT_ACTIVITY_DIALOG_KINDS)
    raw_values = [value] if isinstance(value, str) else value
    if not isinstance(raw_values, list | tuple | set):
        raise ValueError("dialog_kinds must be a list of strings")
    return list(raw_values)


def normalize_activity_dialog_kinds(value: object) -> list[str]:
    """Normalize supported filters, rejecting invalid input before removing filters."""
    raw_values = _coerce_activity_dialog_kinds(value)
    normalized_values: list[str] = []
    for raw in raw_values:
        if not isinstance(raw, str):
            raise ValueError("dialog_kinds entries must be strings")
        normalized = raw.strip().lower()
        if not normalized:
            continue
        for kind in _ACTIVITY_DIALOG_KIND_ALIASES.get(normalized, (normalized,)):
            if kind not in _ALLOWED_ACTIVITY_DIALOG_KINDS:
                allowed = ", ".join(sorted(_ALLOWED_ACTIVITY_DIALOG_KINDS))
                raise ValueError(f"dialog_kinds entries must be one of: {allowed}")
            if kind not in normalized_values:
                normalized_values.append(kind)

    if "all" in normalized_values:
        return ["all"]
    if not normalized_values:
        raise ValueError("dialog_kinds must include at least one kind")
    return normalized_values
