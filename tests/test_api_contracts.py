from __future__ import annotations

from fastapi.testclient import TestClient

from blackbox_service.api import create_app


def test_create_run_and_fetch_status():
    app = create_app(db_path=":memory:", use_playwright=False)
    client = TestClient(app)

    create_resp = client.post(
        "/runs",
        json={
            "targets": ["https://example.com"],
            "options": {"screenshot_policy": "on-change"},
        },
    )
    assert create_resp.status_code == 201
    created = create_resp.json()
    assert created["run_id"]
    assert created["status"] == "running"

    run_id = created["run_id"]
    get_resp = client.get(f"/runs/{run_id}")
    assert get_resp.status_code == 200
    payload = get_resp.json()
    assert payload["run_id"] == run_id
    assert payload["status"] == "running"
    assert payload["targets"] == ["https://example.com"]


def test_post_action_unknown_run_returns_404():
    app = create_app(db_path=":memory:", use_playwright=False)
    client = TestClient(app)

    action_resp = client.post(
        "/runs/missing/actions",
        json={"action_type": "open_tab", "params": {"url": "https://example.com"}},
    )
    assert action_resp.status_code == 404


def test_stream_unknown_run_returns_404():
    app = create_app(db_path=":memory:", use_playwright=False)
    client = TestClient(app)

    stream_resp = client.get("/runs/missing/stream")
    assert stream_resp.status_code == 404


def test_agent_start_and_state_endpoints():
    app = create_app(db_path=":memory:", use_playwright=False)
    client = TestClient(app)

    create_resp = client.post(
        "/runs",
        json={"targets": ["https://example.com"], "options": {}},
    )
    assert create_resp.status_code == 201
    run_id = create_resp.json()["run_id"]

    start_resp = client.post(
        f"/runs/{run_id}/agent/start",
        json={"max_steps": 2, "step_delay_ms": 0},
    )
    assert start_resp.status_code == 202
    assert start_resp.json()["status"] in {"running", "completed"}

    state_resp = client.get(f"/runs/{run_id}/agent/state")
    assert state_resp.status_code == 200
    payload = state_resp.json()
    assert payload["run_id"] == run_id
    assert payload["status"] in {"idle", "running", "completed", "failed"}
