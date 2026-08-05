from __future__ import annotations

import json
from typing import Any

import httpx

from harnesslab.models import AdapterTurn, Message, ToolCall, ToolSpec


class OpenAICompatibleAdapter:
    """Small, inspectable adapter for OpenAI-compatible chat completion endpoints."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 60,
        name: str = "openai-compatible",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.name = name

    async def complete(self, messages: list[Message], tools: list[ToolSpec]) -> AdapterTurn:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [message.model_dump(exclude_none=True) for message in messages],
            "tools": [tool.as_openai_tool() for tool in tools],
            "tool_choice": "auto",
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            )
            response.raise_for_status()
        data = response.json()
        choice = data["choices"][0]
        message = choice["message"]
        calls = []
        for call in message.get("tool_calls", []):
            function = call["function"]
            arguments = function.get("arguments", {})
            if isinstance(arguments, str):
                arguments = json.loads(arguments)
            calls.append(ToolCall(id=call["id"], name=function["name"], arguments=arguments))
        usage = {
            key: int(value)
            for key, value in data.get("usage", {}).items()
            if isinstance(value, int)
        }
        return AdapterTurn(
            text=message.get("content") or "",
            tool_calls=calls,
            finish_reason=choice.get("finish_reason") or "stop",
            usage=usage,
            raw={"id": data.get("id"), "model": data.get("model")},
        )
