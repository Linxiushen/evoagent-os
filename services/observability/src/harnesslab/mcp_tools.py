from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from harnesslab.models import ToolSpec
from harnesslab.tools import ToolRegistry


class MCPStdioProvider:
    """Optional MCP stdio bridge backed by the official Python SDK."""

    def __init__(
        self,
        command: str,
        args: list[str] | None = None,
        env: Mapping[str, str] | None = None,
        namespace: str = "mcp",
    ) -> None:
        self.command = command
        self.args = args or []
        self.env = dict(env or {})
        self.namespace = namespace
        self._remote_names: dict[str, str] = {}

    async def attach(self, registry: ToolRegistry) -> list[ToolSpec]:
        tools = await self._list_tools()
        specs = []
        for tool in tools:
            local_name = f"{self.namespace}__{tool.name}"
            spec = ToolSpec(
                name=local_name,
                description=tool.description or f"MCP tool {tool.name}",
                input_schema=tool.inputSchema,
                read_only=False,
                source=f"mcp:{self.namespace}",
            )
            self._remote_names[local_name] = tool.name
            registry.register(spec, self._handler(local_name))
            specs.append(spec)
        return specs

    def _handler(self, local_name: str):
        async def call(arguments: dict[str, Any]) -> Any:
            return await self._call_tool(self._remote_names[local_name], arguments)

        return call

    def _params(self):
        try:
            from mcp import StdioServerParameters
        except ImportError as exc:
            raise RuntimeError(
                "Install HarnessLab with the 'mcp' extra: pip install harnesslab[mcp]"
            ) from exc
        return StdioServerParameters(command=self.command, args=self.args, env=self.env or None)

    async def _list_tools(self):
        from mcp import ClientSession
        from mcp.client.stdio import stdio_client

        async with (
            stdio_client(self._params()) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.list_tools()
            return result.tools

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        from mcp import ClientSession
        from mcp.client.stdio import stdio_client

        async with (
            stdio_client(self._params()) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.call_tool(name, arguments)
            return [item.model_dump(mode="json") for item in result.content]
