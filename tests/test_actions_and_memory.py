from __future__ import annotations

from blackbox_service.service import BlackboxService


def test_eval_js_and_inject_html_emit_events(tmp_path):
    service = BlackboxService(db_path=tmp_path / "events.db", use_playwright=False)
    run = service.start_run(targets=["https://example.com"], options={})
    tab = service.open_tab(run.run_id, url="https://example.com")

    eval_result = service.execute_action(
        run_id=run.run_id,
        action_type="eval_js",
        params={"tab_id": tab.tab_id, "script": "1 + 1"},
    )
    assert eval_result["ok"] is True
    assert eval_result["result"] == 2

    inject_result = service.execute_action(
        run_id=run.run_id,
        action_type="inject_html",
        params={"tab_id": tab.tab_id, "html": "<div id='poc'>ok</div>"},
    )
    assert inject_result["ok"] is True

    console_result = service.execute_action(
        run_id=run.run_id,
        action_type="read_console",
        params={"tab_id": tab.tab_id},
    )
    assert console_result["ok"] is True
    assert console_result["result"] == []

    network_result = service.execute_action(
        run_id=run.run_id,
        action_type="read_network",
        params={"tab_id": tab.tab_id},
    )
    assert network_result["ok"] is True
    assert network_result["result"] == []

    events = service.list_memory(run.run_id)
    event_types = [event.type for event in events]
    assert "action.eval_js" in event_types
    assert "action.inject_html" in event_types
    assert "observation.console" in event_types
    assert "observation.network" in event_types
