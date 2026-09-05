# ---------------------------------------------------------------------------
# _base.py imports — verify daemon client is available, legacy code removed
# ---------------------------------------------------------------------------


def test_base_exports_daemon_connection() -> None:
    """daemon_connection and DaemonNotRunningError must be importable from _base."""
    from mcp_telegram.tools._base import DaemonNotRunningError, daemon_connection

    assert daemon_connection is not None
    assert DaemonNotRunningError is not None


def test_base_has_no_connected_client() -> None:
    """connected_client must not exist in _base after migration."""
    import mcp_telegram.tools._base as _base_mod

    assert not hasattr(_base_mod, "connected_client"), "connected_client was removed — it should not exist in _base"


def test_base_has_no_disable_telegram_session() -> None:
    """disable_telegram_session must not exist in _base after migration."""
    import mcp_telegram.tools._base as _base_mod

    assert not hasattr(_base_mod, "disable_telegram_session"), (
        "disable_telegram_session was removed — it should not exist in _base"
    )


def test_base_has_no_session_disabled_flag() -> None:
    """_session_disabled module-level flag must not exist in _base after migration."""
    import mcp_telegram.tools._base as _base_mod

    assert not hasattr(_base_mod, "_session_disabled"), "_session_disabled was removed — it should not exist in _base"
