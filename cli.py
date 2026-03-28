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

logging.basicConfig(level=logging.DEBUG)
app = typer.Typer()


def typer_async(f):  # noqa: ANN001, ANN201
    @wraps(f)
    def wrapper(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        return asyncio.run(f(*args, **kwargs))

    return wrapper


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


if __name__ == "__main__":
    app()
