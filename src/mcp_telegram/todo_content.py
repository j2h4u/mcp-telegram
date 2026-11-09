"""Semantic text from a retained Telegram checklist fact."""

from typing import Literal, NamedTuple


class TodoContent(NamedTuple):
    text: str
    status: Literal["captured", "unknown", "unsupported"]


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
    parts: list[str] = []
    status: Literal["captured", "unknown", "unsupported"] = "captured"

    def add_title(title: object) -> None:
        nonlocal status
        if not isinstance(title, dict):
            if status != "unsupported":
                status = "unknown"
        elif title.get("_") != "TextWithEntities":
            status = "unsupported"
        elif isinstance(title.get("text"), str):
            parts.append(title["text"])
        elif status != "unsupported":
            status = "unknown"

    add_title(todo.get("title"))
    items = todo.get("list")
    if not isinstance(items, list):
        if status != "unsupported":
            status = "unknown"
    else:
        for item in items:
            if isinstance(item, dict) and item.get("_") == "TodoItem":
                add_title(item.get("title"))
            elif isinstance(item, dict):
                status = "unsupported"
            elif status != "unsupported":
                status = "unknown"
    return TodoContent("\n".join(part for part in parts if part), status)
