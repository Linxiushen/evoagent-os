from __future__ import annotations

import asyncio
import time
from contextlib import suppress

from .models import Envelope, new_id, utc_now
from .runtime import AgentRuntime
from .store import Store


class Scheduler:
    def __init__(self, store: Store, runtime: AgentRuntime) -> None:
        self.store = store
        self.runtime = runtime
        self._task: asyncio.Task[None] | None = None

    def create(self, name: str, interval_seconds: int, message: str) -> str:
        if interval_seconds < 5:
            raise ValueError("Minimum interval is 5 seconds")
        job_id = new_id("job")
        self.store.execute(
            """INSERT INTO jobs(job_id,name,interval_seconds,message,next_run_at,created_at)
               VALUES(?,?,?,?,?,?)""",
            (job_id, name, interval_seconds, message, time.time() + interval_seconds, utc_now()),
        )
        return job_id

    def list(self) -> list[dict[str, object]]:
        return self.store.query("SELECT * FROM jobs ORDER BY created_at")

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while True:
            now = time.time()
            due = self.store.query("SELECT * FROM jobs WHERE enabled=1 AND next_run_at<=?", (now,))
            for job in due:
                await self.runtime.submit(
                    Envelope(
                        channel="scheduler",
                        peer_id=str(job["job_id"]),
                        text=str(job["message"]),
                    )
                )
                self.store.execute(
                    "UPDATE jobs SET next_run_at=? WHERE job_id=?",
                    (now + int(job["interval_seconds"]), job["job_id"]),
                )
            await asyncio.sleep(1)
