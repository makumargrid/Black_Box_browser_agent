from __future__ import annotations

from blackbox_service.runtime import InMemoryRuntime


def test_multi_tab_correlation_and_active_tab_switch():
    runtime = InMemoryRuntime()
    run_id = "run-1"
    runtime.start_run(run_id, targets=["https://a.example"])

    tab_a = runtime.open_tab(run_id=run_id, url="https://a.example", correlation_id="corr-1")
    tab_b = runtime.open_tab(
        run_id=run_id,
        url="https://b.example",
        correlation_id="corr-1",
        parent_tab_id=tab_a.tab_id,
    )
    runtime.switch_tab(run_id=run_id, tab_id=tab_a.tab_id)

    tabs = runtime.list_tabs(run_id)
    assert len(tabs) == 3
    assert any(t.url == "https://a.example" and t.parent_tab_id is None for t in tabs)
    assert any(t.tab_id == tab_b.tab_id and t.parent_tab_id == tab_a.tab_id for t in tabs)
    assert runtime.get_active_tab(run_id) == tab_a.tab_id
