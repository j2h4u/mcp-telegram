import asyncio
from typing import Annotated

from typer import Context, Option, Typer

app = Typer()


@app.callback(invoke_without_command=True)
def _run(ctx: Context) -> None:
    if ctx.invoked_subcommand is None:
        # This will run if no subcommand is specified
        run()


@app.command()
def sign_in(
    api_id: Annotated[str, Option(help="Telegram API id")],
    api_hash: Annotated[str, Option(help="Telegram API hash")],
    phone_number: Annotated[str, Option(help="Phone number with country code")],
) -> None:
    """Connect to Telegram API."""
    from .telegram import connect_to_telegram

    asyncio.run(connect_to_telegram(api_id, api_hash, phone_number))


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


# ---------------------------------------------------------------------------
# feedback sub-app — admin queue management for SubmitFeedback (Phase 48)
# ---------------------------------------------------------------------------

feedback_app = Typer(help="Inspect and manage agent feedback queue.")
app.add_typer(feedback_app, name="feedback")


@feedback_app.command("list")
def feedback_list(
    limit: Annotated[int, Option(help="Max rows to display (default 50).")] = 50,
) -> None:
    """List recent agent feedback (most-recent first)."""
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
        rows = conn.execute(
            "SELECT id, submitted_at, severity, message, context, model, harness "
            "FROM feedback ORDER BY submitted_at DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        print("No feedback recorded yet.")
        return

    for rid, ts, sev, msg, ctx, mdl, harn in rows:
        sev_tag = f"[{sev}]" if sev else "[?]"
        ts_human = _dt.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
        metadata_parts = [f"id={rid}", sev_tag, ts_human]
        if mdl:
            metadata_parts.append(f"model={mdl}")
        if harn:
            metadata_parts.append(f"harness={harn}")
        print(" ".join(metadata_parts))
        print(f"  message: {msg}")
        if ctx:
            print(f"  context: {ctx}")
        print()  # blank line between rows


@feedback_app.command("delete")
def feedback_delete(feedback_id: int) -> None:
    """Delete a feedback row by id."""
    import sqlite3
    import sys

    from .feedback_db import get_feedback_db_path

    path = get_feedback_db_path()
    if not path.exists():
        print("No feedback database — nothing to delete.")
        sys.exit(1)

    # ──────────────────────────────────────────────────────────────────────
    # Concurrent-writer note (admin-tool exception to "daemon is sole writer"):
    # The daemon may be INSERT-ing new feedback rows at the exact moment this
    # CLI runs DELETE. SQLite WAL mode allows ONE writer at a time, so the
    # second writer waits up to busy_timeout (5s here) before raising
    # OperationalError. For a low-volume admin tool this is acceptable —
    # operator retries if the lock is held longer than 5s. The two writers
    # never corrupt each other because:
    #   1. WAL serialises writes (one at a time)
    #   2. busy_timeout=5000 gives a bounded wait, not an unbounded hang
    #   3. AUTOINCREMENT id avoids primary-key collisions even mid-write
    # See Pitfall 3 in 48-RESEARCH.md.
    # ──────────────────────────────────────────────────────────────────────
    conn = sqlite3.connect(str(path), timeout=5.0)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        cur = conn.execute("DELETE FROM feedback WHERE id = ?", (feedback_id,))
        conn.commit()
        if cur.rowcount == 0:
            print(f"Feedback id {feedback_id} not found.")
            sys.exit(1)
        print(f"Deleted feedback id {feedback_id}.")
    finally:
        conn.close()
