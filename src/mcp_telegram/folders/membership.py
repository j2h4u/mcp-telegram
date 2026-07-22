"""Pure Telegram folder-membership rules."""

from __future__ import annotations

from .contracts import DialogFacts, FolderRule


def _passes_exclusions(folder: FolderRule, dialog: DialogFacts) -> bool:
    if folder.exclude_archived and dialog.archived:
        return False
    if folder.exclude_read and not dialog.unread:
        return False
    return not (folder.exclude_muted and dialog.muted)


def matches(folder: FolderRule, dialog: DialogFacts) -> bool:
    if dialog.dialog_id in folder.excluded_ids:
        return False
    if dialog.dialog_id in folder.included_ids or dialog.dialog_id in folder.pinned_ids:
        return True
    if folder.explicit_only or dialog.category not in folder.categories:
        return False
    return _passes_exclusions(folder, dialog)
