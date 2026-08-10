from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Any

from harnesslab.models import TraceEvent


class EventBus:
    """Ordered per-run event log with lightweight live subscriptions."""

    def __init__(self) -> None:
        self._events: dict[str, list[TraceEvent]] = defaultdict(list)
        self._subscribers: dict[str, set[asyncio.Queue[TraceEvent]]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def publish(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        duration_ms: float | None = None,
    ) -> TraceEvent:
        async with self._lock:
            event = TraceEvent(
                run_id=run_id,
                sequence=len(self._events[run_id]) + 1,
                type=event_type,
                payload=payload or {},
                duration_ms=duration_ms,
            )
            self._events[run_id].append(event)
            subscribers = tuple(self._subscribers[run_id])
        for queue in subscribers:
            queue.put_nowait(event)
        return event

    def history(self, run_id: str) -> list[TraceEvent]:
        return list(self._events.get(run_id, ()))

    async def subscribe(self, run_id: str, after: int = 0) -> AsyncIterator[TraceEvent]:
        queue: asyncio.Queue[TraceEvent] = asyncio.Queue()
        async with self._lock:
            backlog = [event for event in self._events.get(run_id, ()) if event.sequence > after]
            self._subscribers[run_id].add(queue)
        for event in backlog:
            if event.sequence > after:
                yield event
        try:
            while True:
                yield await queue.get()
        finally:
            self._subscribers[run_id].discard(queue)
