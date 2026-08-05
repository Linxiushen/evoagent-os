from __future__ import annotations

import ipaddress
import json
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .models import RiskLevel, ToolCall
from .store import Store

ToolHandler = Callable[[dict[str, Any], "ToolContext"], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    risk: RiskLevel
    handler: ToolHandler

    def wire_schema(self) -> dict[str, object]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


@dataclass
class ToolContext:
    store: Store
    session_id: str
    run_id: str
    workspace: Path
    allowed_hosts: set[str]


@dataclass(frozen=True)
class ToolDecision:
    action: str
    reason: str


class ToolPolicy:
    def __init__(
        self,
        denied: set[str] | None = None,
        require_approval: set[RiskLevel] | None = None,
    ) -> None:
        self.denied = denied or set()
        self.require_approval = require_approval or {RiskLevel.MEDIUM, RiskLevel.HIGH}

    def decide(self, tool: Tool) -> ToolDecision:
        if tool.name in self.denied:
            return ToolDecision("deny", f"Tool {tool.name} is denied by operator policy")
        if tool.risk in self.require_approval:
            return ToolDecision(
                "approve", f"{tool.risk.value}-risk tool requires operator approval"
            )
        return ToolDecision("execute", "low-risk tool allowed")


class ToolRegistry:
    def __init__(self, policy: ToolPolicy | None = None) -> None:
        self.policy = policy or ToolPolicy()
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Duplicate tool: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ValueError(f"Unknown tool: {name}") from exc

    def schemas(self) -> list[dict[str, object]]:
        return [tool.wire_schema() for tool in self._tools.values()]

    async def execute(self, call: ToolCall, context: ToolContext) -> dict[str, Any]:
        return await self.get(call.name).handler(call.arguments, context)


def _safe_workspace_path(workspace: Path, relative: str) -> Path:
    candidate = (workspace / relative).resolve()
    root = workspace.resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("Path escapes workspace")
    return candidate


async def clock_now(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    del arguments, context
    return {"utc": datetime.now(UTC).isoformat()}


async def memory_remember(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    text = str(arguments.get("text", "")).strip()
    if not text:
        raise ValueError("text is required")
    return {"memory_id": context.store.remember(context.session_id, text)}


async def memory_search(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    query = str(arguments.get("query", "")).strip()
    return {"matches": context.store.search_memory(query, limit=5)}


async def workspace_read(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    path = _safe_workspace_path(context.workspace, str(arguments.get("path", "")))
    if not path.is_file():
        raise ValueError("File does not exist")
    if path.stat().st_size > 1_000_000:
        raise ValueError("File exceeds 1 MB read limit")
    return {
        "path": str(path.relative_to(context.workspace)),
        "content": path.read_text(encoding="utf-8"),
    }


async def workspace_write(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    path = _safe_workspace_path(context.workspace, str(arguments.get("path", "")))
    content = str(arguments.get("content", ""))
    if len(content.encode("utf-8")) > 1_000_000:
        raise ValueError("Content exceeds 1 MB write limit")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return {"path": str(path.relative_to(context.workspace)), "bytes": len(content.encode("utf-8"))}


def _validate_host(host: str, allowed_hosts: set[str]) -> None:
    if host not in allowed_hosts:
        raise ValueError("Host is not in EVOAGENT_HTTP_ALLOWLIST")
    for result in socket.getaddrinfo(host, None):
        address = ipaddress.ip_address(result[4][0])
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
        ):
            raise ValueError("Resolved host points to a non-public address")


async def http_get(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    url = str(arguments.get("url", ""))
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Only HTTPS URLs are allowed")
    _validate_host(parsed.hostname, context.allowed_hosts)
    async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
        response = await client.get(url, headers={"User-Agent": "EvoAgent-Runtime/0.1"})
    body = response.content[:1_000_000]
    return {
        "status": response.status_code,
        "content_type": response.headers.get("content-type", ""),
        "body": body.decode("utf-8", errors="replace"),
        "truncated": len(response.content) > len(body),
    }


def builtins(policy: ToolPolicy | None = None) -> ToolRegistry:
    registry = ToolRegistry(policy)
    registry.register(
        Tool(
            "clock.now",
            "Return current UTC time",
            {"type": "object", "properties": {}},
            RiskLevel.LOW,
            clock_now,
        )
    )
    registry.register(
        Tool(
            "memory.remember",
            "Persist a useful fact or preference",
            {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            RiskLevel.LOW,
            memory_remember,
        )
    )
    registry.register(
        Tool(
            "memory.search",
            "Search persistent memory",
            {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
            RiskLevel.LOW,
            memory_search,
        )
    )
    registry.register(
        Tool(
            "workspace.read",
            "Read a UTF-8 file inside the configured workspace",
            {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            RiskLevel.MEDIUM,
            workspace_read,
        )
    )
    registry.register(
        Tool(
            "workspace.write",
            "Write a UTF-8 file inside the configured workspace",
            {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
            RiskLevel.HIGH,
            workspace_write,
        )
    )
    registry.register(
        Tool(
            "http.get",
            "Fetch an operator-allowlisted HTTPS URL",
            {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
            RiskLevel.HIGH,
            http_get,
        )
    )
    return registry


def serialize_result(result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))
