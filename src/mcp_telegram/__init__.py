import asyncio
import sqlite3
from typing import Annotated, cast

from typer import Argument, BadParameter, Context, Option, Typer

from .config import ConfigError, HttpServerConfig, load_config, resolve_http_server_config, resolve_logging_config

app = Typer()


def _resolve_http_host(host: str | None, *, base: HttpServerConfig | None = None) -> str:
    try:
        # Supplying the model default keeps host validation independent from a
        # malformed port override, matching the previous CLI behavior.
        defaults = HttpServerConfig() if base is None else base
        return resolve_http_server_config(host=host, port=defaults.port, base=defaults).host
    except ConfigError as exc:
        raise BadParameter(str(exc)) from exc


def _resolve_http_port(port: int | None, *, base: HttpServerConfig | None = None) -> int:
    try:
        return resolve_http_server_config(port=port, base=base).port
    except ConfigError as exc:
        raise BadParameter(str(exc)) from exc


def _row_first_int(row: tuple[object | None, ...] | None) -> int:
    if row is None:
        return 0
    value = row[0]
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return 0


@app.callback(invoke_without_command=True)
def _run(ctx: Context) -> None:
    if ctx.invoked_subcommand is None:
        # This will run if no subcommand is specified
        run()


@app.command()
def run() -> None:
    """Run the mcp-telegram server."""
    from . import server as _server

    asyncio.run(_server.run_mcp_server())


@app.command()
def logout() -> None:
    """Logout from Telegram API."""
    from .telegram import logout_from_telegram

    asyncio.run(logout_from_telegram())


@app.command()
def sync() -> None:
    """Run the sync daemon (owns TelegramClient exclusively)."""
    import logging
    import sys

    from .daemon import sync_main

    log_level = resolve_logging_config().level
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        stream=sys.stderr,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
    )
    asyncio.run(sync_main())


@app.command()
def serve(
    host: Annotated[
        str | None,
        Option(
            "--host",
            help="HTTP bind host for the Streamable HTTP MCP endpoint.",
            envvar="MCP_TELEGRAM_HTTP_HOST",
        ),
    ] = None,
    port: Annotated[
        int | None,
        Option(
            "--port",
            help="HTTP bind port for the Streamable HTTP MCP endpoint.",
            envvar="MCP_TELEGRAM_HTTP_PORT",
        ),
    ] = None,
) -> None:
    """Run the sync daemon and Streamable HTTP MCP endpoint in one process."""
    import logging
    import sys

    from . import server as _server
    from .daemon import sync_main

    operator_config = load_config()
    resolved_host = _resolve_http_host(host, base=operator_config.http)
    resolved_port = _resolve_http_port(port, base=operator_config.http)
    log_level = resolve_logging_config().level
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        stream=sys.stderr,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
    )

    async def _run() -> None:
        sync_task = asyncio.create_task(sync_main(), name="sync-daemon")
        http_task = asyncio.create_task(
            _server.run_mcp_http_server(host=resolved_host, port=resolved_port),
            name="mcp-http",
        )
        tasks = {sync_task, http_task}
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in pending:
            try:
                await task
            except asyncio.CancelledError:
                pass
        for task in done:
            exc = task.exception()
            if exc is not None:
                raise exc

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# feedback sub-app — admin queue management for submit_feedback (Phase 48)
# ---------------------------------------------------------------------------

feedback_app = Typer(help="Inspect and manage agent feedback queue.")
app.add_typer(feedback_app, name="feedback")

_SMOKE_FEEDBACK_CONTEXT = "smoke-integration.json automated test"
_SMOKE_FEEDBACK_HARNESS = "devtools.mcp_client.cli"
_OPERATOR_FEEDBACK_SQL = "(context IS NULL OR context != ? OR harness IS NULL OR harness != ?)"
_SMOKE_FEEDBACK_SQL = "context = ? AND harness = ?"


def _feedback_list_select_rows(conn: sqlite3.Connection, limit: int, show_all: bool) -> list[tuple]:
    base_select = (
        "SELECT id, submitted_at, severity, status, status_changed_at, "
        "status_comment, message, context, model, harness FROM feedback"
    )
    order_limit = " ORDER BY submitted_at DESC, id DESC LIMIT ?"
    if show_all:
        return conn.execute(base_select + order_limit, (limit,)).fetchall()
    return conn.execute(
        base_select + " WHERE status IN ('open','in_progress')" + f" AND {_OPERATOR_FEEDBACK_SQL}" + order_limit,
        (_SMOKE_FEEDBACK_CONTEXT, _SMOKE_FEEDBACK_HARNESS, limit),
    ).fetchall()


def _feedback_list_empty_message(conn: sqlite3.Connection, show_all: bool) -> str:
    if show_all:
        return "No feedback recorded yet."
    return _feedback_list_default_empty_message(conn)


def _feedback_list_default_empty_message(conn: sqlite3.Connection) -> str:
    row = cast(tuple[object | None, ...] | None, conn.execute("SELECT COUNT(*) FROM feedback").fetchone())
    total = _row_first_int(row)
    if total == 0:
        return "No feedback recorded yet."
    visible_open_row = cast(
        tuple[object | None, ...] | None,
        conn.execute(
            f"SELECT COUNT(*) FROM feedback WHERE status IN ('open','in_progress') AND {_OPERATOR_FEEDBACK_SQL}",
            (_SMOKE_FEEDBACK_CONTEXT, _SMOKE_FEEDBACK_HARNESS),
        ).fetchone(),
    )
    if _row_first_int(visible_open_row) > 0:
        return "No feedback shown. Increase --limit to display open feedback."
    smoke_open_row = cast(
        tuple[object | None, ...] | None,
        conn.execute(
            f"SELECT COUNT(*) FROM feedback WHERE status IN ('open','in_progress') AND {_SMOKE_FEEDBACK_SQL}",
            (_SMOKE_FEEDBACK_CONTEXT, _SMOKE_FEEDBACK_HARNESS),
        ).fetchone(),
    )
    if _row_first_int(smoke_open_row) > 0:
        return "No operator-actionable feedback. Use --all to show automated smoke entries and history."
    return "No open or in-progress feedback. Use --all to show history."


def _feedback_list_print_row(
    row: tuple[int, int, str | None, str, int | None, str | None, str, str | None, str | None, str | None],
) -> None:
    from datetime import UTC
    from datetime import datetime as _dt

    (
        rid,
        ts,
        sev,
        status,
        status_changed_at,
        status_comment,
        msg,
        ctx,
        mdl,
        harn,
    ) = row

    sev_tag = f"[{sev}]" if sev else "[?]"
    status_tag = f"[{status}]"
    ts_human = _dt.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d %H:%M")
    metadata_parts = [f"id={rid}", sev_tag, status_tag, ts_human]
    if status_changed_at:
        changed_human = _dt.fromtimestamp(status_changed_at, tz=UTC).strftime("%Y-%m-%d %H:%M")
        metadata_parts.append(f"changed={changed_human}")
    if mdl:
        metadata_parts.append(f"model={mdl}")
    if harn:
        metadata_parts.append(f"harness={harn}")
    print(" ".join(metadata_parts))
    print(f"  message: {msg}")
    if ctx:
        print(f"  context: {ctx}")
    if status_comment:
        print(f"  status_comment: {status_comment}")
    print()  # blank line between rows


@feedback_app.command("list")
def feedback_list(
    limit: Annotated[int, Option(help="Max rows to display (default 50).")] = 50,
    show_all: Annotated[bool, Option("--all", help="Include done and dismissed items.")] = False,
) -> None:
    """List recent agent feedback (most-recent first).

    Default view shows only `open` and `in_progress` items -- what needs
    attention. Use --all to include `done` and `dismissed` history.
    """
    from .config import load_config
    from .feedback_db import get_feedback_db_path

    path = get_feedback_db_path(load_config().state.dir)
    if not path.exists():
        print("No feedback recorded yet.")
        return

    # Open with a short busy_timeout — daemon holds WAL but reads are non-blocking.
    # See Pitfall 3 in 48-RESEARCH.md.
    conn = sqlite3.connect(str(path), timeout=5.0)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        rows = _feedback_list_select_rows(conn, limit, show_all)
        if not rows:
            print(_feedback_list_empty_message(conn, show_all))
            return
    finally:
        conn.close()

    for row in rows:
        _feedback_list_print_row(row)


@feedback_app.command("status")
def feedback_status(
    feedback_id: Annotated[int, Argument(help="Feedback row id (see `feedback list`).")],
    status: Annotated[
        str,
        Argument(help="New status: open | in_progress | done | dismissed"),
    ],
    reason: Annotated[
        str | None,
        Option("--reason", help="Optional rationale for this status change."),
    ] = None,
) -> None:
    """Set the status of a feedback row.

    Routes through the daemon Unix socket — feedback.db is daemon-write-only.
    Validates the status string locally before opening the socket so an
    invalid value never reaches the daemon.
    """
    import sys

    from .daemon_client import daemon_connection
    from .feedback_db import VALID_STATUSES

    if status not in VALID_STATUSES:
        valid_list = ", ".join(sorted(VALID_STATUSES))
        print(f"Invalid status '{status}'. Must be one of: {valid_list}")
        sys.exit(1)

    async def _run() -> None:
        async with daemon_connection() as conn:
            response = await conn.update_feedback_status(
                feedback_id=feedback_id,
                status=status,
                reason=reason,
            )
        if response.get("ok"):
            data = response.get("data", {}) or {}
            print(data.get("message", f"Feedback {feedback_id} -> {status}"))
        else:
            err_msg = response.get("message") or response.get("error") or "unknown error"
            print(f"Error: {err_msg}")
            sys.exit(1)

    asyncio.run(_run())
