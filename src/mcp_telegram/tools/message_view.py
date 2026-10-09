"""Compatibility imports for the shared message_view projection."""

from ..message_view import (  # noqa: F401
    _READ_MARKER_METADATA,
    DRAFT_MESSAGE_VIEW_SCHEMA,
    MESSAGE_VIEW_SCHEMA,
    READ_MARKER_SCHEMA,
    ReadMarker,
    __all__,
    _content_facts,
    _context_facts,
    _event_facts,
    _identity_facts,
    _project_sender,
    _reaction_event_payload,
    _reply_context,
    project_draft_message_view,
    project_message_view,
    project_read_markers,
)
