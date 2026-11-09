"""Semantic text from a retained Telegram checklist fact."""

from typing import Literal, NamedTuple


class TodoContent(NamedTuple):
    text: str
    status: Literal["captured", "unknown", "unsupported"]


def _title_content(title: object) -> TodoContent:
    if not isinstance(title, dict):
        return TodoContent("", "unknown")
    if title.get("_") != "TextWithEntities":
        return TodoContent("", "unsupported")
    text = title.get("text")
    return TodoContent(text, "captured") if isinstance(text, str) else TodoContent("", "unknown")


def _item_content(item: object) -> TodoContent:
    if not isinstance(item, dict):
        return TodoContent("", "unknown")
    if item.get("_") != "TodoItem":
        return TodoContent("", "unsupported")
    return _title_content(item.get("title"))


def _combine_content(parts: list[TodoContent]) -> TodoContent:
    statuses = {part.status for part in parts}
    status: Literal["captured", "unknown", "unsupported"] = (
        "unsupported" if "unsupported" in statuses else "unknown" if "unknown" in statuses else "captured"
    )
    return TodoContent("\n".join(part.text for part in parts if part.text), status)


def todo_content(payload: object) -> TodoContent:
    if not isinstance(payload, dict) or not payload:
        return TodoContent("", "unknown")
    if payload.get("_") != "MessageMediaToDo":
        return TodoContent("", "unsupported")
    todo = payload.get("todo")
    if not isinstance(todo, dict):
        return TodoContent("", "unknown")
    if todo.get("_") != "TodoList":
        return TodoContent("", "unsupported")
    parts = [_title_content(todo.get("title"))]
    items = todo.get("list")
    if isinstance(items, list):
        parts.extend(_item_content(item) for item in items)
    else:
        parts.append(TodoContent("", "unknown"))
    return _combine_content(parts)
