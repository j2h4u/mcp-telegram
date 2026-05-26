import asyncio
from typing import Annotated

from typer import Argument, Context, Option, Typer

app = Typer()


@app.callback(invoke_without_command=True)
def _run(ctx: Context) -> None:
    if ctx.invoked_subcommand is None:
        # This will run if no subcommand is specified
        run()


@app.command()
def run() -> None:
    """Run the mcp-telegram server."""
    from .server import run_mcp_server

    asyncio.run(run_mcp_server())


@app.command()
def logout() -> None:
    """Logout from Telegram API."""
    from .telegram import logout_from_telegram

    asyncio.run(logout_from_telegram())


@app.command()
def sync() -> None:
    """Run the sync daemon (owns TelegramClient exclusively)."""
    import logging
    import os
    import sys

    from .daemon import sync_main

    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
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
        str,
        Option(
            "--host",
            help="HTTP bind host for the Streamable HTTP MCP endpoint.",
            envvar="MCP_TELEGRAM_HTTP_HOST",
        ),
    ] = "127.0.0.1",
    port: Annotated[
        int,
        Option(
            "--port",
            help="HTTP bind port for the Streamable HTTP MCP endpoint.",
            envvar="MCP_TELEGRAM_HTTP_PORT",
        ),
    ] = 3100,
) -> None:
    """Run the sync daemon and Streamable HTTP MCP endpoint in one process."""
    import logging
    import os
    import sys

    from .daemon import sync_main
    from .server import run_mcp_http_server

    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        stream=sys.stderr,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
    )

    async def _run() -> None:
        sync_task = asyncio.create_task(sync_main(), name="sync-daemon")
        http_task = asyncio.create_task(
            run_mcp_http_server(host=host, port=port),
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


@feedback_app.command("list")
def feedback_list(
    limit: Annotated[int, Option(help="Max rows to display (default 50).")] = 50,
    show_all: Annotated[bool, Option("--all", help="Include done and dismissed items.")] = False,
) -> None:
    """List recent agent feedback (most-recent first).

    Default view shows only `open` and `in_progress` items -- what needs
    attention. Use --all to include `done` and `dismissed` history.
    """
    import sqlite3
    from datetime import datetime as _dt

    from .feedback_db import get_feedback_db_path

    path = get_feedback_db_path()
    if not path.exists():
        print("No feedback recorded yet.")
        return

    # Open with a short busy_timeout — daemon holds WAL but reads are non-blocking.
    # See Pitfall 3 in 48-RESEARCH.md.
    conn = sqlite3.connect(str(path), timeout=5.0)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        base_select = (
            "SELECT id, submitted_at, severity, status, status_changed_at, "
            "status_comment, message, context, model, harness FROM feedback"
        )
        order_limit = " ORDER BY submitted_at DESC, id DESC LIMIT ?"
        if show_all:
            rows = conn.execute(base_select + order_limit, (limit,)).fetchall()
        else:
            rows = conn.execute(
                base_select
                + " WHERE status IN ('open','in_progress')"
                + order_limit,
                (limit,),
            ).fetchall()

        if not rows:
            if show_all:
                # --all is set and we still got nothing -> table is empty.
                print("No feedback recorded yet.")
                return
            # Default filter empty — distinguish "queue empty" from
            # "all open work cleared, history exists".
            total = conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
            if total == 0:
                print("No feedback recorded yet.")
            else:
                print(
                    "No open or in-progress feedback. "
                    "Use --all to show history."
                )
            return
    finally:
        conn.close()

    for (
        rid, ts, sev, status, status_changed_at, status_comment,
        msg, ctx, mdl, harn,
    ) in rows:
        sev_tag = f"[{sev}]" if sev else "[?]"
        status_tag = f"[{status}]"
        ts_human = _dt.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
        metadata_parts = [f"id={rid}", sev_tag, status_tag, ts_human]
        if status_changed_at:
            changed_human = _dt.fromtimestamp(status_changed_at).strftime("%Y-%m-%d %H:%M")
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
