"""Application service for daemon-owned feedback operations.

The service owns feedback request validation and response semantics while the
injected store owns SQLite persistence.  Daemon API code depends on this small
port instead of reaching into the feedback database implementation.
"""

from __future__ import annotations

import dataclasses
import logging
import time
from collections.abc import Mapping
from typing import Protocol, cast

from .feedback_contracts import VALID_SEVERITIES, VALID_STATUSES

logger = logging.getLogger(__name__)

_FEEDBACK_MESSAGE_MAX_LEN = 10_000
_FEEDBACK_CONTEXT_MAX_LEN = 2_000
_FEEDBACK_MODEL_MAX_LEN = 200
_FEEDBACK_HARNESS_MAX_LEN = 200


class FeedbackStore(Protocol):
    """Persistence port required by :class:`FeedbackApplicationService`."""

    def insert_feedback(  # noqa: PLR0913
        self,
        *,
        submitted_at: int,
        message: str,
        severity: str | None,
        context: object | None,
        model: object | None,
        harness: object | None,
    ) -> int | None: ...

    def update_feedback_status(
        self,
        *,
        feedback_id: int,
        status: str,
        status_changed_at: int,
        reason: str | None,
    ) -> bool: ...


class FeedbackService(Protocol):
    """Application boundary consumed by the daemon API dispatcher."""

    def submit_feedback(self, req: Mapping[str, object]) -> dict[str, object]: ...

    def update_feedback_status(self, req: Mapping[str, object]) -> dict[str, object]: ...


@dataclasses.dataclass(frozen=True)
class _SubmitFeedbackRequest:
    message: str
    severity: str | None
    context: object | None
    model: object | None
    harness: object | None

    @classmethod
    def parse(cls, req: Mapping[str, object]) -> _SubmitFeedbackRequest:
        message = req.get("message", "")
        if not isinstance(message, str):
            raise ValueError("message must be a string")

        stripped = message.strip()
        if not stripped:
            raise ValueError("message is required")
        if len(message) > _FEEDBACK_MESSAGE_MAX_LEN:
            raise ValueError("message too long (max 10000 chars)")

        severity = req.get("severity")
        if severity is not None and severity not in VALID_SEVERITIES:
            valid_list = ", ".join(sorted(VALID_SEVERITIES))
            raise ValueError(f"severity must be one of: {valid_list}")

        context = req.get("context")
        model = req.get("model")
        harness = req.get("harness")
        if context is not None and len(str(context)) > _FEEDBACK_CONTEXT_MAX_LEN:
            raise ValueError("context too long (max 2000 chars)")
        if model is not None and len(str(model)) > _FEEDBACK_MODEL_MAX_LEN:
            raise ValueError("model too long (max 200 chars)")
        if harness is not None and len(str(harness)) > _FEEDBACK_HARNESS_MAX_LEN:
            raise ValueError("harness too long (max 200 chars)")

        return cls(
            message=stripped,
            severity=cast(str | None, severity),
            context=context,
            model=model,
            harness=harness,
        )


@dataclasses.dataclass(frozen=True)
class _UpdateFeedbackStatusRequest:
    feedback_id: int
    status: str
    reason: str | None

    @classmethod
    def parse(cls, req: Mapping[str, object]) -> _UpdateFeedbackStatusRequest:
        feedback_id = req.get("id")
        if not isinstance(feedback_id, int) or feedback_id <= 0:
            raise ValueError("id must be a positive integer")

        status = req.get("status")
        if status not in VALID_STATUSES:
            valid_list = ", ".join(sorted(VALID_STATUSES))
            raise ValueError(f"status must be one of: {valid_list}")

        reason = req.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise ValueError("reason must be a string or null")

        return cls(feedback_id=feedback_id, status=cast(str, status), reason=reason)


class FeedbackApplicationService:
    """Validate and execute feedback operations through an injected store."""

    def __init__(self, store: FeedbackStore | None) -> None:
        self._store = store

    def submit_feedback(self, req: Mapping[str, object]) -> dict[str, object]:
        try:
            request = _SubmitFeedbackRequest.parse(req)
        except ValueError as exc:
            return {"ok": False, "error": "invalid_input", "message": str(exc)}

        if self._store is None:
            return {"ok": False, "error": "internal", "message": "feedback database not initialised"}

        try:
            feedback_id = self._store.insert_feedback(
                submitted_at=int(time.time()),
                message=request.message,
                severity=request.severity,
                context=request.context,
                model=request.model,
                harness=request.harness,
            )
            return {"ok": True, "data": {"message": "Feedback recorded. Thank you!", "id": feedback_id}}
        except Exception:
            logger.exception("submit_feedback failed")
            return {"ok": False, "error": "internal", "message": "internal error"}

    def update_feedback_status(self, req: Mapping[str, object]) -> dict[str, object]:
        try:
            request = _UpdateFeedbackStatusRequest.parse(req)
        except ValueError as exc:
            return {"ok": False, "error": "invalid_input", "message": str(exc)}

        # T-49-11: no length cap on status_comment by design (single-operator
        # low-volume queue; SQLite handles multi-MB TEXT comfortably).
        if self._store is None:
            return {"ok": False, "error": "internal", "message": "feedback database not initialised"}

        try:
            updated = self._store.update_feedback_status(
                feedback_id=request.feedback_id,
                status=request.status,
                status_changed_at=int(time.time()),
                reason=request.reason,
            )
            if not updated:
                return {
                    "ok": False,
                    "error": "not_found",
                    "message": f"Feedback id {request.feedback_id} not found.",
                }
            return {
                "ok": True,
                "data": {"message": f"Feedback {request.feedback_id} status set to '{request.status}'."},
            }
        except Exception:
            logger.exception("update_feedback_status failed")
            return {"ok": False, "error": "internal", "message": "internal error"}


__all__ = ["FeedbackApplicationService", "FeedbackService", "FeedbackStore"]
