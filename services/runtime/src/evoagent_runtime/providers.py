from __future__ import annotations

import json
import os
from typing import Protocol

import httpx

from .models import ChatMessage, ModelTurn, ToolCall


class Provider(Protocol):
    async def complete(
        self, messages: list[ChatMessage], tools: list[dict[str, object]]
    ) -> ModelTurn: ...


class OfflineProvider:
    """Deterministic provider for onboarding, tests, and air-gapped demos."""

    async def complete(
        self, messages: list[ChatMessage], tools: list[dict[str, object]]
    ) -> ModelTurn:
        latest_index = max(
            (index for index, message in enumerate(messages) if message.role == "user"),
            default=-1,
        )
        latest = messages[latest_index].content if latest_index >= 0 else ""
        lowered = latest.lower().strip()
        tool_results = [
            message for message in messages[latest_index + 1 :] if message.role == "tool"
        ]
        if tool_results:
            result = tool_results[-1]
            return ModelTurn(
                text=f"Tool `{result.name}` completed: {result.content}",
                input_tokens=max(1, sum(len(message.content) for message in messages) // 4),
                output_tokens=max(1, len(result.content) // 4),
            )
        if lowered.startswith("remember:"):
            return ModelTurn(
                tool_calls=[
                    ToolCall(
                        name="memory.remember", arguments={"text": latest.split(":", 1)[1].strip()}
                    )
                ]
            )
        if lowered.startswith("recall:"):
            return ModelTurn(
                tool_calls=[
                    ToolCall(
                        name="memory.search", arguments={"query": latest.split(":", 1)[1].strip()}
                    )
                ]
            )
        if lowered.startswith("read:"):
            return ModelTurn(
                tool_calls=[
                    ToolCall(
                        name="workspace.read", arguments={"path": latest.split(":", 1)[1].strip()}
                    )
                ]
            )
        if lowered.startswith("write:"):
            payload = latest.split(":", 1)[1].strip()
            path, _, content = payload.partition("=")
            return ModelTurn(
                tool_calls=[
                    ToolCall(
                        name="workspace.write", arguments={"path": path.strip(), "content": content}
                    )
                ]
            )
        return ModelTurn(
            text=f"Offline agent received: {latest}",
            input_tokens=max(1, sum(len(message.content) for message in messages) // 4),
            output_tokens=max(1, len(latest) // 4),
        )


class OpenAICompatibleProvider:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.base_url = (
            base_url or os.getenv("EVOAGENT_BASE_URL", "https://api.openai.com/v1")
        ).rstrip("/")
        self.api_key = api_key or os.getenv("EVOAGENT_API_KEY") or os.getenv("OPENAI_API_KEY")
        self.model = model or os.getenv("EVOAGENT_MODEL", "gpt-4o-mini")
        self.timeout = timeout
        if not self.api_key:
            raise ValueError("Set EVOAGENT_API_KEY or OPENAI_API_KEY")

    async def complete(
        self, messages: list[ChatMessage], tools: list[dict[str, object]]
    ) -> ModelTurn:
        wire_messages = []
        for message in messages:
            if message.role == "tool":
                wire_messages.append(
                    {"role": "user", "content": f"Tool result ({message.name}): {message.content}"}
                )
            else:
                wire_messages.append({"role": message.role, "content": message.content})
        payload: dict[str, object] = {
            "model": self.model,
            "messages": wire_messages,
            "temperature": 0.2,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            )
        if response.is_error:
            raise RuntimeError(
                f"Model API returned HTTP {response.status_code}: {response.text[:500]}"
            )
        data = response.json()
        message = data["choices"][0]["message"]
        calls = []
        for item in message.get("tool_calls") or []:
            try:
                arguments = json.loads(item["function"].get("arguments") or "{}")
            except json.JSONDecodeError as exc:
                raise RuntimeError("Model returned invalid tool arguments") from exc
            calls.append(
                ToolCall(id=item["id"], name=item["function"]["name"], arguments=arguments)
            )
        usage = data.get("usage") or {}
        return ModelTurn(
            text=message.get("content") or "",
            tool_calls=calls,
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
            model=str(data.get("model", self.model)),
        )
