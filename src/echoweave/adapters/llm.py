from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import httpx


class DemoLLM:
    async def stream(
        self,
        messages: list[dict[str, str]],
        cancel_event: asyncio.Event,
    ) -> AsyncIterator[str]:
        user_text = next(
            (item["content"] for item in reversed(messages) if item["role"] == "user"),
            "",
        )
        reply = (
            "你好，我是 EchoWeave-RTC 的安全演示数字分身。"
            f"我收到了你的内容：“{user_text}”"
            "。当前是本地演示后端；配置 DeepSeek、VoxCPM2 和 SoulX 后，"
            "同一条实时链路会切换到完整模型。"
        )
        for index in range(0, len(reply), 5):
            if cancel_event.is_set():
                return
            await asyncio.sleep(0.015)
            yield reply[index : index + 5]


class DeepSeekV4Flash:
    """Minimal dependency OpenAI-compatible SSE client for DeepSeek V4 Flash."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-v4-flash",
        thinking: str = "disabled",
        timeout_seconds: float = 120.0,
    ) -> None:
        if not api_key:
            raise ValueError("DEEPSEEK_API_KEY is required for the DeepSeek backend")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.thinking = thinking
        self.timeout_seconds = timeout_seconds

    async def stream(
        self,
        messages: list[dict[str, str]],
        cancel_event: asyncio.Event,
    ) -> AsyncIterator[str]:
        body = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "thinking": {"type": self.thinking},
            "max_tokens": 800,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        timeout = httpx.Timeout(self.timeout_seconds, connect=15.0)
        async with (
            httpx.AsyncClient(timeout=timeout) as client,
            client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=body,
            ) as response,
        ):
            response.raise_for_status()
            async for line in response.aiter_lines():
                if cancel_event.is_set():
                    return
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    if data == "[DONE]":
                        return
                    continue
                payload = json.loads(data)
                choices = payload.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                content = delta.get("content")
                if content:
                    yield str(content)
