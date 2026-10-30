from typing import cast

import pytest
from jsonschema import validate

from mcp_telegram.server import _bounded_account_protection, _is_valid_active_account_protection
from mcp_telegram.tools._base import ACTIVE_ACCOUNT_PROTECTION_SCHEMA
from tests.test_daemon_api import make_server


@pytest.mark.parametrize(
    ("state", "enabled", "eligible", "due", "mode", "notice_fragment"),
    [
        ("waiting", True, True, 12345.0, "automatic", "trial is scheduled"),
        ("waiting", True, True, None, "automatic", "no next trial time"),
        ("trial", True, False, None, "automatic", "recovery is in progress"),
        ("stability", True, False, None, "automatic", "recovery is in progress"),
        ("manual", True, False, None, "manual", "requires operator action"),
        ("disabled", False, False, None, "manual", "requires operator action"),
    ],
)
def test_protection_notice_follows_automatic_snapshot(  # noqa: PLR0913
    state: str, enabled: bool, eligible: bool, due: float | None, mode: str, notice_fragment: str
) -> None:
    server = make_server()
    automatic = {
        "enabled": enabled,
        "state": state,
        "eligible": eligible,
        "phase": {"waiting": "waiting", "trial": "consumed", "stability": "succeeded", "manual": "exhausted"}.get(
            state
        ),
        "block_reason": "flood_budget_spent" if state == "manual" else None,
        "attempts": 0,
        "max_attempts": 6,
        "next_trial_at_utc": due,
    }
    server._account_safety_status = lambda: {
        "reason": "unresolved_outbound_outcome",
        "automatic_resume": automatic,
    }
    response = server._get_account_protection({})
    protection = cast(dict[str, object], cast(dict[str, object], response["data"])["account_protection"])
    assert protection["recovery"] == mode
    assert protection["automatic_resume"] == automatic
    notice = cast(str, protection["notice"])
    assert notice_fragment in notice
    assert "Retained local reads remain available" in notice
    assert "no confirmed response" in notice
    assert ("operator action" in notice) == (mode == "manual")
    assert _is_valid_active_account_protection(protection)
    validate(_bounded_account_protection(protection), ACTIVE_ACCOUNT_PROTECTION_SCHEMA)


@pytest.mark.parametrize(
    "phase,block_reason", [("waiting", "unsafe_scope_or_outcome"), ("exhausted", "flood_exhausted")]
)
def test_manual_hold_is_not_described_as_automatic(phase: str, block_reason: str) -> None:
    server = make_server()
    server._account_safety_status = lambda: {
        "reason": "unresolved_outbound_outcome",
        "automatic_resume": {
            "enabled": True,
            "state": "manual",
            "eligible": False,
            "phase": phase,
            "block_reason": block_reason,
            "attempts": 0,
            "max_attempts": 6,
        },
    }
    response = server._get_account_protection({})
    protection = cast(dict[str, object], cast(dict[str, object], response["data"])["account_protection"])
    assert protection["recovery"] == "manual"
    assert "requires operator action" in cast(str, protection["notice"])
    assert _is_valid_active_account_protection(protection)


def test_eligible_fencing_is_described_as_preparing() -> None:
    server = make_server()
    server._account_safety_status = lambda: {
        "reason": "unresolved_outbound_outcome",
        "automatic_resume": {
            "enabled": True,
            "state": "manual",
            "eligible": True,
            "phase": "fencing",
            "attempts": 0,
            "max_attempts": 6,
        },
    }
    response = server._get_account_protection({})
    protection = cast(dict[str, object], cast(dict[str, object], response["data"])["account_protection"])
    assert protection["recovery"] == "automatic"
    assert "recovery is preparing" in cast(str, protection["notice"])
