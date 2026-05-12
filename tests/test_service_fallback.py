from __future__ import annotations

from blackbox_service.service import BlackboxService


def test_service_falls_back_when_playwright_runtime_fails(tmp_path, monkeypatch):
    class _ExplodingRuntime:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("playwright missing")

    monkeypatch.setattr("blackbox_service.service.ThreadedPlaywrightRuntime", _ExplodingRuntime)

    service = BlackboxService(db_path=tmp_path / "events.db", use_playwright=True)
    run = service.start_run(targets=["https://example.com"], options={})

    assert run.status == "running"
    assert run.active_tab_id is not None


def test_service_strict_runtime_raises_when_playwright_missing(tmp_path, monkeypatch):
    class _ExplodingRuntime:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("playwright missing")

    monkeypatch.setattr("blackbox_service.service.ThreadedPlaywrightRuntime", _ExplodingRuntime)

    try:
        BlackboxService(
            db_path=tmp_path / "events.db",
            use_playwright=True,
            strict_playwright_runtime=True,
        )
        raised = False
    except RuntimeError as exc:
        raised = True
        assert "Playwright runtime unavailable" in str(exc)

    assert raised
