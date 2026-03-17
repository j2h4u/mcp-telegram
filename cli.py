from __future__ import annotations

import asyncio
import json
import logging
from functools import wraps

import typer
from rich.console import Console
from rich.json import JSON
from rich.table import Table

from mcp_telegram import server
from mcp_telegram.cache import TopicMetadataCache
from mcp_telegram.forum_topics import (
    fetch_forum_topics_page,
    load_dialog_topics,
    normalize_topic_metadata,
    refresh_topic_by_id,
    topic_row_text,
)
from mcp_telegram.models import TOPIC_METADATA_TTL_SECONDS
from mcp_telegram.telegram import create_client
from mcp_telegram.tools import get_entity_cache

logging.basicConfig(level=logging.DEBUG)
app = typer.Typer()


def typer_async(f):  # noqa: ANN001, ANN201
    @wraps(f)
    def wrapper(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        return asyncio.run(f(*args, **kwargs))

    return wrapper


def _require_dialog_id(entity: object) -> int:
    """Return the integer dialog id for one resolved Telegram entity."""
    dialog_id = getattr(entity, "id", None)
    if not isinstance(dialog_id, int):
        raise ValueError("Resolved dialog is missing an integer id")
    return dialog_id


def _topic_metadata_json(topic: dict[str, object] | None) -> str:
    """Return stable JSON for one topic metadata record."""
    return json.dumps(topic, ensure_ascii=False, sort_keys=True)


async def _connect_debug_client() -> tuple[object, bool]:
    """Return one connected Telegram client and whether this command connected it."""
    client = create_client()
    if client.is_connected():
        return client, False

    await client.connect()
    return client, True


@app.command()
@typer_async
async def list_tools() -> None:
    """List available tools."""

    console = Console()

    # Create a table
    table = Table(title="Available Tools")

    # Add three columns
    table.add_column("Name", style="cyan")
    table.add_column("Description", style="magenta")
    table.add_column("Schema", style="green")
    for tool in await server.list_tools():
        json_data = json.dumps(tool.inputSchema["properties"])
        table.add_row(tool.name, tool.description, JSON(json_data))

    console.print(table)


@app.command()
@typer_async
async def call_tool(
    name: str = typer.Option(help="Name of the tool"),
    arguments: str = typer.Option(help="Arguments for the tool as JSON string"),
) -> None:
    """Handle tool calls for command line run."""
    for response in await server.call_tool(name, json.loads(arguments)):
        typer.echo(response)


@app.command("debug-topic-catalog")
@typer_async
async def debug_topic_catalog(
    dialog: str = typer.Option(help="Dialog name or identifier"),
    page_size: int = typer.Option(100, min=1, help="Raw forum-topic page size"),
) -> None:
    """Inspect forum-topic catalog pages and the normalized cached view."""
    try:
        client, owns_connection = await _connect_debug_client()
        try:
            entity = await client.get_entity(dialog)
            dialog_id = _require_dialog_id(entity)
            cache = get_entity_cache()
            topic_cache = TopicMetadataCache(cache._conn)

            typer.echo(f"dialog_id={dialog_id} page_size={page_size}")

            offset_date: object | None = None
            offset_id = 0
            offset_topic = 0
            page_number = 1

            while True:
                page_topics, total_count = await fetch_forum_topics_page(
                    client,
                    entity=entity,
                    offset_date=offset_date,
                    offset_id=offset_id,
                    offset_topic=offset_topic,
                    limit=page_size,
                )
                if not page_topics:
                    if page_number == 1:
                        typer.echo("no_topics_found")
                    break

                typer.echo(
                    " ".join(
                        [
                            f"page={page_number}",
                            f"offset_topic={offset_topic}",
                            f"offset_id={offset_id}",
                            f"fetched={len(page_topics)}",
                            f"total_count={total_count}",
                        ]
                    )
                )
                for raw_topic in page_topics:
                    typer.echo(topic_row_text(normalize_topic_metadata(raw_topic)))

                last_topic = page_topics[-1]
                next_offset_topic = getattr(last_topic, "id", None)
                next_offset_id = getattr(last_topic, "top_message", 0) or 0
                next_offset_date = getattr(last_topic, "date", None)
                if not isinstance(next_offset_topic, int):
                    break
                if (
                    next_offset_topic == offset_topic
                    and next_offset_id == offset_id
                    and next_offset_date == offset_date
                ):
                    break

                offset_topic = next_offset_topic
                offset_id = int(next_offset_id)
                offset_date = next_offset_date
                page_number += 1

                if len(page_topics) < page_size:
                    break

            topic_catalog = await load_dialog_topics(
                client,
                entity=entity,
                dialog_id=dialog_id,
                topic_cache=topic_cache,
                ttl_seconds=TOPIC_METADATA_TTL_SECONDS,
            )
            typer.echo(
                " ".join(
                    [
                        f'normalized_catalog_count={len(topic_catalog["metadata_by_id"])}',
                        f'active_count={len(topic_catalog["choices"])}',
                        f'deleted_count={len(topic_catalog["deleted_topics"])}',
                    ]
                )
            )
        finally:
            if owns_connection:
                await client.disconnect()
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)


@app.command("debug-topic-by-id")
@typer_async
async def debug_topic_by_id(
    dialog: str = typer.Option(help="Dialog name or identifier"),
    topic_id: int = typer.Option(help="Forum topic id"),
) -> None:
    """Inspect cached topic metadata and one by-id refresh result."""
    try:
        client, owns_connection = await _connect_debug_client()
        try:
            entity = await client.get_entity(dialog)
            dialog_id = _require_dialog_id(entity)
            cache = get_entity_cache()
            topic_cache = TopicMetadataCache(cache._conn)
            topic_catalog = await load_dialog_topics(
                client,
                entity=entity,
                dialog_id=dialog_id,
                topic_cache=topic_cache,
                ttl_seconds=TOPIC_METADATA_TTL_SECONDS,
            )
            cached_topic = topic_catalog["metadata_by_id"].get(topic_id)
            refreshed_topic = await refresh_topic_by_id(
                client,
                entity=entity,
                dialog_id=dialog_id,
                topic_id=topic_id,
                topic_cache=topic_cache,
                ttl_seconds=TOPIC_METADATA_TTL_SECONDS,
            )

            typer.echo(f"dialog_id={dialog_id} topic_id={topic_id}")
            typer.echo(f"cached={_topic_metadata_json(cached_topic)}")
            typer.echo(f"refreshed={_topic_metadata_json(refreshed_topic)}")
        finally:
            if owns_connection:
                await client.disconnect()
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)


if __name__ == "__main__":
    app()
