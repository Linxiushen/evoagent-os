from __future__ import annotations

from typing import Protocol

from harnesslab.models import AdapterTurn, Message, ToolSpec


class HarnessAdapter(Protocol):
    name: str

    async def complete(self, messages: list[Message], tools: list[ToolSpec]) -> AdapterTurn:
        """Produce the next harness turn without mutating caller-owned state."""
        ...

