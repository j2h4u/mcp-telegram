from __future__ import annotations

import pytest
from jsonschema import validate

from mcp_telegram.topic_identity import TOPIC_IDENTITY_SCHEMA, project_topic


@pytest.mark.parametrize(
    ("topic_id", "title", "expected"),
    [
        (7, "  Reports  ", {"title": "Reports"}),
        (7, "", {"topic_id": 7}),
        (None, "  ", None),
        (0, "", None),
    ],
)
def test_project_topic_has_one_universal_title_or_id_shape(
    topic_id: int | None, title: str, expected: dict[str, object] | None
) -> None:
    actual = project_topic(topic_id=topic_id, title=title)
    assert actual == expected
    if actual is not None:
        validate(instance=actual, schema=TOPIC_IDENTITY_SCHEMA)
