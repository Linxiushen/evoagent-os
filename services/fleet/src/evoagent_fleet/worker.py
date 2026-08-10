from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from .models import Completion, WorkerRegistration
from .orchestrator import Orchestrator

Handler = Callable[[dict[str, Any]], Awaitable[Completion]]


class Worker:
    def __init__(
        self, orchestrator: Orchestrator, registration: WorkerRegistration, handler: Handler
    ) -> None:
        self.orchestrator = orchestrator
        self.registration = registration
        self.handler = handler

    async def run_once(self) -> bool:
        self.orchestrator.register(self.registration)
        lease = self.orchestrator.claim(self.registration.worker_id)
        if lease is None:
            return False
        try:
            result = await self.handler(lease)
            self.orchestrator.complete(self.registration.worker_id, lease["lease_token"], result)
        except Exception as exc:  # noqa: BLE001 - handlers are an isolation boundary.
            self.orchestrator.fail(self.registration.worker_id, lease["lease_token"], str(exc))
        return True

    async def run_forever(self, poll_seconds: float = 1.0) -> None:
        while True:
            if not await self.run_once():
                await asyncio.sleep(poll_seconds)
