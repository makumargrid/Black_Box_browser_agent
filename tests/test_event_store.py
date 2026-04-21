from __future__ import annotations

from blackbox_service.models import EventEnvelope
from blackbox_service.store import SQLiteEventStore


def test_event_store_persists_and_replays(tmp_path):
    store = SQLiteEventStore(db_path=tmp_path / "events.db")
    run = store.create_run(targets=["https://example.com"], options={})

    event = EventEnvelope(
        event_id="evt-1",
        run_id=run.run_id,
        type="thought",
        payload={"text": "checking page"},
    )
    store.append_event(event)

    rows = store.list_events(run.run_id)
    assert len(rows) == 1
    assert rows[0].event_id == "evt-1"
    assert rows[0].payload["text"] == "checking page"

    loaded_run = store.get_run(run.run_id)
    assert loaded_run is not None
    assert loaded_run.run_id == run.run_id
