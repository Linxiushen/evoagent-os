from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from harnesslab.models import ToolSpec

ToolHandler = Callable[[dict[str, Any]], Awaitable[Any] | Any]


class ToolNotFoundError(LookupError):
    pass


class ToolRegistry:
    """Registry for local and MCP-discovered tools using JSON Schema contracts."""

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._handlers: dict[str, ToolHandler] = {}

    def register(self, spec: ToolSpec, handler: ToolHandler) -> None:
        if spec.name in self._specs:
            raise ValueError(f"Tool already registered: {spec.name}")
        self._specs[spec.name] = spec
        self._handlers[spec.name] = handler

    def specs(self) -> list[ToolSpec]:
        return list(self._specs.values())

    def get(self, name: str) -> ToolSpec:
        try:
            return self._specs[name]
        except KeyError as exc:
            raise ToolNotFoundError(name) from exc

    async def call(self, name: str, arguments: dict[str, Any]) -> Any:
        self.get(name)
        result = self._handlers[name](arguments)
        if inspect.isawaitable(result):
            return await result
        return result


def build_demo_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="search_repository",
            description="Search a small repository fixture for implementation evidence.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Symbol or behavior to locate"}
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        ),
        lambda args: {
            "matches": [
                {"path": "src/checkout/policy.py", "line": 42, "score": 0.96},
                {"path": "tests/test_checkout_policy.py", "line": 18, "score": 0.89},
            ],
            "query": args["query"],
        },
    )
    registry.register(
        ToolSpec(
            name="inspect_change",
            description="Inspect a proposed code change and return deterministic risk signals.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "focus": {"type": "string"},
                },
                "required": ["path", "focus"],
                "additionalProperties": False,
            },
        ),
        lambda args: {
            "path": args["path"],
            "risk": "high",
            "signals": [
                "authorization check moved after state mutation",
                "rollback path is not covered by a test",
            ],
            "focus": args["focus"],
        },
    )
    return registry

