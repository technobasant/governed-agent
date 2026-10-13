"""The agent's side of the MCP boundary.

The agent never imports the warehouse. It sees whatever tools the MCP server lists, with
the server's own descriptions and schemas, and gets back whatever the server returns —
in process for tests, over streamable HTTP when the server runs as its own service.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Protocol

from mcp import Client
from mcp.server.mcpserver import MCPServer

from ga_agent.llm import ToolSpec


class ToolBox(Protocol):
    async def specs(self) -> list[ToolSpec]: ...

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


class McpToolBox:
    def __init__(self, client: Client) -> None:
        self._client = client
        self._specs: list[ToolSpec] | None = None

    async def specs(self) -> list[ToolSpec]:
        if self._specs is None:
            listed = await self._client.list_tools()
            self._specs = [
                ToolSpec(t.name, (t.description or "").strip(), dict(t.input_schema))
                for t in listed.tools
            ]
        return self._specs

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call a tool. Transport failures come back as errors, like any refusal."""
        try:
            result = await self._client.call_tool(name, arguments)
        except Exception as exc:
            return {"ok": False, "error": f"tool call failed: {type(exc).__name__}: {exc}"}
        text = "".join(getattr(c, "text", "") for c in result.content)
        if result.is_error:
            return {"ok": False, "error": text[:500] or "tool error"}
        payload = result.structured_content
        if isinstance(payload, dict):
            return payload
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {"ok": False, "error": "tool returned something other than JSON"}
        return parsed if isinstance(parsed, dict) else {"ok": True, "value": parsed}


def _first_leaf(error: BaseException) -> BaseException:
    while isinstance(error, BaseExceptionGroup) and error.exceptions:
        error = error.exceptions[0]
    return error


@asynccontextmanager
async def connect(target: MCPServer | str, timeout_s: float = 30.0) -> AsyncIterator[McpToolBox]:
    """`target` is an in-process server or the URL of one, e.g. http://mcp:8001/mcp.

    The client runs in a task group, which wraps anything raised inside the `async with`
    in an exception group. The caller gets the original exception back instead."""
    try:
        async with Client(target, read_timeout_seconds=timeout_s) as client:
            yield McpToolBox(client)
    except BaseExceptionGroup as group:
        raise _first_leaf(group) from None
