from __future__ import annotations

import asyncio

from blackbox_service.models import EventEnvelope
from blackbox_service.stream import RunEventBus


def test_event_bus_snapshot_contains_published_events():
    bus = RunEventBus()
    event = EventEnvelope(event_id="evt-1", run_id="run-1", type="action.eval_js", payload={"result": 4})
    bus.publish(event)

    snapshot = bus.snapshot("run-1")
    assert len(snapshot) == 1
    assert snapshot[0].type == "action.eval_js"


def test_event_bus_async_subscribe_replays_published_event():
    bus = RunEventBus()
    event = EventEnvelope(event_id="evt-1", run_id="run-1", type="thought", payload={"text": "hi"})
    bus.publish(event)

    async def _collect_one():
        stream = bus.subscribe("run-1")
        return await asyncio.wait_for(stream.__anext__(), timeout=1.0)

    out = asyncio.run(_collect_one())
    assert out.event_id == "evt-1"
    assert out.payload["text"] == "hi"

