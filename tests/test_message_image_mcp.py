"""Native image delivery through the registered MCP tool and framed IPC."""

import asyncio
import base64
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import pytest
from mcp.types import ImageContent

from mcp_telegram import server
from mcp_telegram.daemon_api import DaemonAPIServer
from mcp_telegram.daemon_client import DaemonConnection
from mcp_telegram.daemon_ipc import REQUEST_LIMIT, write_response
from mcp_telegram.message_images.contracts import ImageReadResult
from mcp_telegram.tools import message_images


class ImageService:
    def __init__(self, image: bytes) -> None:
        self.image = image
        self.queued = 0

    def status(self) -> dict[str, object]:
        return {
            "dialog_id": -123,
            "message_id": 7,
            "content": {
                "state": "available",
                "mime_type": "image/jpeg",
                "byte_size": len(self.image),
                "representation": "telegram_photo",
                "path": "private",
            },
            "acquisition": {"state": "blocked", "reason": "flood_wait", "photo_id": 33},
        }

    async def prepare(self, dialog_id: int, message_id: int) -> dict[str, object]:
        self.queued += 1
        return self.status()

    async def read(self, dialog_id: int, message_id: int) -> ImageReadResult:
        return ImageReadResult(self.status(), self.image, "image/jpeg")


@pytest.mark.asyncio
async def test_native_image_real_framed_ipc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    image = b"\xff\xd8\xff" + b"x" * (4 * 1024 * 1024 - 5) + b"\xff\xd9"
    service = ImageService(image)
    api = DaemonAPIServer.__new__(DaemonAPIServer)
    api.bind_message_image_service(service)
    socket = tmp_path / "daemon.sock"

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = cast(dict[str, object], json.loads(await reader.readline()))
        response = await api._dispatch(request)
        await write_response(writer, response)
        writer.close()
        await writer.wait_closed()

    listener = await asyncio.start_unix_server(handle, path=socket, limit=REQUEST_LIMIT)

    @asynccontextmanager
    async def connection():
        reader, writer = await asyncio.open_unix_connection(socket, limit=REQUEST_LIMIT)
        try:
            yield DaemonConnection(reader, writer)
        finally:
            writer.close()
            await writer.wait_closed()

    monkeypatch.setattr(message_images, "daemon_connection", connection)
    try:
        result = await server.call_tool("get_message_image", {"exact_dialog_id": -123, "message_id": 7})
        assert not result.is_error
        assert len(result.content) == 1 and isinstance(result.content[0], ImageContent)
        assert base64.b64decode(result.content[0].data) == image
        metadata = cast(dict[str, object], result.structured_content)
        assert "private" not in str(metadata) and "photo_id" not in str(metadata)
        assert service.queued == 0
        prepared = await server.call_tool("prepare_message_image", {"exact_dialog_id": -123, "message_id": 7})
        assert not prepared.is_error and prepared.content == [] and service.queued == 1
    finally:
        listener.close()
        await listener.wait_closed()


@pytest.mark.parametrize(
    "encoded", ["!!!", base64.b64encode(b"not jpeg").decode(), "A" * (message_images.MAX_ENCODED_BYTES + 4)]
)
def test_invalid_private_image_payload_rejected(encoded: str) -> None:
    with pytest.raises(ValueError):
        message_images.decode_image(
            encoded,
            {"state": "available", "mime_type": "image/jpeg", "representation": "telegram_photo", "byte_size": 8},
        )


def test_server_rejects_forged_native_content() -> None:
    from mcp.types import TextContent

    from mcp_telegram.tools import ToolResult

    status: dict[str, object] = {
        "content": {"state": "available", "mime_type": "image/jpeg", "representation": "telegram_photo", "byte_size": 5}
    }
    invalid = [
        [TextContent(type="text", text="not an image")],
        [ImageContent(data="!!!", mime_type="image/jpeg")],
        [ImageContent(data="/9j//9k=", mime_type="image/png")],
        [ImageContent(data="/9j//9k=", mime_type="image/jpeg")] * 2,
        [],
    ]
    for content in invalid:
        with pytest.raises(ValueError):
            server._project_tool_result(
                "get_message_image", ToolResult(content=content, structured_content=status), server._CallTelemetry()
            )
    other = server._project_tool_result(
        "list_messages", ToolResult(content=invalid[0], structured_content={}), server._CallTelemetry()
    )
    assert other.content == []
