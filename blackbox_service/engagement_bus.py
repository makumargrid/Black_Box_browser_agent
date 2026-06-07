from __future__ import annotations

import logging
import queue
import threading
from collections import defaultdict
from typing import Any

logger = logging.getLogger(__name__)


class EngagementEventBus:
    """Thread-safe fan-out bus for per-engagement SSE consumers.

    The EngagementOrchestrator runs engagement flows in background threads and
    calls ``publish()`` from those threads. SSE generator coroutines run in the
    asyncio event loop; they subscribe to a per-consumer ``queue.Queue``, drain
    it with ``q.get_nowait()`` inside an ``asyncio.sleep`` polling loop, and
    unsubscribe in a ``finally`` block when the client disconnects.

    Design invariant: ``rec.events`` list is never touched here. This bus is a
    purely additive second consumer — the existing polling dashboard and
    ``/events`` endpoint keep working unchanged.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[queue.Queue[dict[str, Any]]]] = defaultdict(list)
        self._lock = threading.Lock()

    def publish(self, engagement_id: str, msg: dict[str, Any]) -> None:
        """Publish an enriched event snapshot to all current subscribers.

        Called from the orchestrator background thread — must be thread-safe.
        Drops stale messages on full queues to avoid unbounded memory growth
        from slow / disconnected consumers.
        """
        with self._lock:
            queues = list(self._subscribers.get(engagement_id, []))

        for q in queues:
            try:
                q.put_nowait(msg)
            except queue.Full:
                logger.debug(
                    "EngagementEventBus: dropped event for %s (consumer queue full)",
                    engagement_id,
                )

    def subscribe(self, engagement_id: str, maxsize: int = 512) -> queue.Queue[dict[str, Any]]:
        """Register a new consumer queue for *engagement_id*.

        Returns a ``queue.Queue`` that will receive every event published after
        this call. The caller is responsible for calling ``unsubscribe()`` when
        the consumer is done (typically in a ``finally`` block).
        """
        q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=maxsize)
        with self._lock:
            self._subscribers[engagement_id].append(q)
        logger.debug("EngagementEventBus: subscribed to %s (%d total)",
                     engagement_id, len(self._subscribers[engagement_id]))
        return q

    def unsubscribe(self, engagement_id: str, q: queue.Queue[dict[str, Any]]) -> None:
        """Remove *q* from the subscriber list for *engagement_id*.

        Safe to call even if *q* was already removed or never registered.
        """
        with self._lock:
            subs = self._subscribers.get(engagement_id, [])
            try:
                subs.remove(q)
            except ValueError:
                pass
        logger.debug("EngagementEventBus: unsubscribed from %s", engagement_id)
