from __future__ import annotations

import asyncio
import threading
from collections import defaultdict

from blackbox_service.models import EventEnvelope


class RunEventBus:
    """In-memory event bus for live run streaming."""

    def __init__(self) -> None:
        self._events: dict[str, list[EventEnvelope]] = defaultdict(list)
        self._lock = threading.Lock()

    def publish(self, event: EventEnvelope) -> None:
        with self._lock:
            self._events[event.run_id].append(event)

    def snapshot(self, run_id: str) -> list[EventEnvelope]:
        with self._lock:
            return list(self._events.get(run_id, []))

    async def subscribe(self, run_id: str):
        cursor = 0
        while True:
            batch = []
            with self._lock:
                run_events = self._events.get(run_id, [])
                if cursor < len(run_events):
                    batch = run_events[cursor:]
                    cursor = len(run_events)
            if batch:
                for event in batch:
                    yield event
            await asyncio.sleep(0.05)

