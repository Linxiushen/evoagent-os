from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import httpx

from echoweave.adapters.http import (
    ManagedAsyncClient,
    iter_bounded_lines,
    require_content_type,
)


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
        max_output_chars: int = 64_000,
        max_sse_line_chars: int = 1024 * 1024,
        max_response_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        if not api_key:
            raise ValueError("DEEPSEEK_API_KEY is required for the DeepSeek backend")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.thinking = thinking
        self.timeout_seconds = timeout_seconds
        self.max_output_chars = max_output_chars
        self.max_sse_line_chars = max_sse_line_chars
        self.max_response_bytes = max_response_bytes
        self._http = ManagedAsyncClient(
            httpx.Timeout(timeout_seconds, connect=15.0),
        )

    async def stream(
        self,
        messages: list[dict[str, str]],
        cancel_event: asyncio.Event,
    ) -> AsyncIterator[str]:
        if cancel_event.is_set():
            return
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
        client = await self._http.get()
        async with client.stream(
            "POST",
            f"{self.base_url}/chat/completions",
            headers=headers,
            json=body,
        ) as response:
            response.raise_for_status()
            require_content_type(
                response,
                {"text/event-stream"},
                source="DeepSeek",
            )
            output_chars = 0
            async for raw_line in iter_bounded_lines(
                response,
                max_line_bytes=self.max_sse_line_chars,
                max_total_bytes=self.max_response_bytes,
                source="DeepSeek",
            ):
                if cancel_event.is_set():
                    return
                try:
                    line = raw_line.decode("utf-8", errors="strict")
                except UnicodeDecodeError as exc:
                    raise RuntimeError("DeepSeek returned invalid SSE text") from exc
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    if data == "[DONE]":
                        return
                    continue
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError as exc:
                    raise RuntimeError("DeepSeek returned invalid SSE JSON") from exc
                if not isinstance(payload, dict):
                    raise TypeError("DeepSeek returned an invalid SSE event")
                choices = payload.get("choices") or []
                if not choices:
                    continue
                if not isinstance(choices, list) or not isinstance(choices[0], dict):
                    raise TypeError("DeepSeek returned invalid streamed choices")
                delta = choices[0].get("delta") or {}
                if not isinstance(delta, dict):
                    raise TypeError("DeepSeek returned invalid streamed delta")
                content = delta.get("content")
                if content:
                    if not isinstance(content, str):
                        raise RuntimeError("DeepSeek returned invalid streamed content")
                    output_chars += len(content)
                    if output_chars > self.max_output_chars:
                        raise RuntimeError("DeepSeek output exceeded the safety limit")
                    yield content

    async def aclose(self) -> None:
        await self._http.aclose()
