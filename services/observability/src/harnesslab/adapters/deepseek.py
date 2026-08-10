from __future__ import annotations

import httpx

from harnesslab.adapters.openai_compatible import OpenAICompatibleAdapter
from harnesslab.models import CapabilityDocument


class DeepSeekAPIAdapter(OpenAICompatibleAdapter):
    """Adapter for today's public DeepSeek API, not the unreleased Harness protocol."""

    def __init__(self, api_key: str, model: str = "deepseek-chat") -> None:
        super().__init__(
            base_url="https://api.deepseek.com",
            api_key=api_key,
            model=model,
            name="deepseek-api",
        )


class DeepSeekHarnessProbe:
    """A quarantined discovery boundary for the future DeepSeek Harness protocol.

    No official protocol is assumed. Once a preview specification is available, only this
    discovery boundary and a new adapter need to change; the runtime and conformance suite stay
    stable.
    """

    WELL_KNOWN_PATH = "/.well-known/agent-harness"

    def __init__(self, base_url: str, token: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    async def discover(self) -> CapabilityDocument:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f"{self.base_url}{self.WELL_KNOWN_PATH}", headers=headers)
            response.raise_for_status()
        data = response.json()
        return CapabilityDocument(
            protocol=data.get("protocol", "unknown"),
            protocol_version=str(data.get("protocol_version", "unknown")),
            features=list(data.get("features", [])),
            transport=list(data.get("transport", [])),
            auth=list(data.get("auth", [])),
            raw=data,
        )

