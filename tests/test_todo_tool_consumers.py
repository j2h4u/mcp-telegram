from mcp_telegram.tools._base import omit_none_mapping_values
from mcp_telegram.tools.account_trace import _attach_trace_content_metadata
from mcp_telegram.tools.activity import _structured_comment


def test_trace_and_activity_keep_complete_todo_fact() -> None:
    payload = {"_": "MessageMediaToDo", "todo": None, "completions": []}
    item = {"text": "task", "media_kind": "todo", "media_payload": payload}
    trace = {"groups": [{"evidence": [dict(item)]}]}
    _attach_trace_content_metadata(trace)
    evidence = trace["groups"][0]["evidence"][0]
    cleaned = cast(dict[str, object], omit_none_mapping_values(evidence))
    assert cast(dict[str, object], cleaned["media_content"])["todo"] == payload
    activity = _structured_comment({**item, "dialog_id": 1, "message_id": 2})
    cleaned_activity = cast(dict[str, object], omit_none_mapping_values(activity))
    assert cast(dict[str, object], cleaned_activity["media"])["todo"] == payload


from typing import cast
