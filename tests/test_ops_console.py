from __future__ import annotations

import json
import time

from fastapi.testclient import TestClient

from blackbox_service.api import create_app


def _make_app(tmp_path):
    return create_app(db_path=":memory:", use_playwright=False, artifacts_dir=tmp_path / "artifacts")


def _run_to_terminal(client: TestClient, eid: str, timeout_s: float = 10.0) -> dict:
    """Start an engagement and wait for it to reach a terminal status."""
    client.post(f"/engagements/{eid}/start", json={"max_steps_per_agent": 3, "step_delay_ms": 0})
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        state = client.get(f"/engagements/{eid}").json()
        if state["status"] in {"completed", "failed", "budget_exhausted", "paused_for_approval"}:
            return state
        time.sleep(0.05)
    return client.get(f"/engagements/{eid}").json()


# ---------------------------------------------------------------------------
# /ops-console route
# ---------------------------------------------------------------------------

def test_ops_console_route_returns_html(tmp_path):
    """GET /ops-console → 200 with HTML content referencing static assets."""
    app = _make_app(tmp_path)
    client = TestClient(app)
    resp = client.get("/ops-console")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    body = resp.text
    assert "ops_console.css" in body
    assert "ops_console.js" in body
    assert "Operations Console" in body


def test_static_css_served(tmp_path):
    """GET /static/ops_console.css → 200 with Phase-B keyframes."""
    app = _make_app(tmp_path)
    client = TestClient(app)
    resp = client.get("/static/ops_console.css")
    assert resp.status_code == 200
    assert "text/css" in resp.headers["content-type"]
    assert "bb-think" in resp.text
    assert "bb-exploit" in resp.text
    assert "bb-success" in resp.text


def test_static_js_served(tmp_path):
    """GET /static/ops_console.js → 200 with EVENT_MAP covering key types."""
    app = _make_app(tmp_path)
    client = TestClient(app)
    resp = client.get("/static/ops_console.js")
    assert resp.status_code == 200
    assert "EVENT_MAP" in resp.text
    assert "tool.invoked" in resp.text
    assert "tool.rejected" in resp.text
    assert "engagement.completed" in resp.text


# ---------------------------------------------------------------------------
# /engagements/{id}/stream SSE endpoint
# ---------------------------------------------------------------------------

def test_engagement_stream_404_for_unknown(tmp_path):
    """Unknown engagement_id → 404."""
    app = _make_app(tmp_path)
    client = TestClient(app)
    resp = client.get("/engagements/does-not-exist/stream")
    assert resp.status_code == 404


def test_engagement_stream_replays_and_closes_for_completed(tmp_path):
    """For a completed engagement, stream replays all events and then closes cleanly."""
    app = _make_app(tmp_path)
    client = TestClient(app)

    create_resp = client.post(
        "/engagements",
        json={"target_url": "https://example.com", "budget_usd": 5, "approval_mode": "none"},
    )
    assert create_resp.status_code == 201
    eid = create_resp.json()["engagement_id"]

    # Run engagement to terminal state before connecting
    _run_to_terminal(client, eid)

    # Now stream — generator should replay events and return (closed stream)
    messages = []
    with client.stream("GET", f"/engagements/{eid}/stream") as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        for line in resp.iter_lines():
            if line.startswith("data: "):
                messages.append(json.loads(line[len("data: "):]))

    assert len(messages) > 0
    types = [m["type"] for m in messages]
    assert "engagement.created" in types


def test_engagement_stream_includes_enriched_snapshot(tmp_path):
    """Each streamed message includes phase, status, and budget fields."""
    app = _make_app(tmp_path)
    client = TestClient(app)

    create_resp = client.post(
        "/engagements",
        json={"target_url": "https://example.com", "budget_usd": 25, "approval_mode": "none"},
    )
    eid = create_resp.json()["engagement_id"]
    _run_to_terminal(client, eid)

    with client.stream("GET", f"/engagements/{eid}/stream") as resp:
        for line in resp.iter_lines():
            if line.startswith("data: "):
                msg = json.loads(line[len("data: "):])
                assert "phase"  in msg
                assert "status" in msg
                assert "budget" in msg
                assert "spent"  in msg["budget"]
                assert "limit"  in msg["budget"]
                assert float(msg["budget"]["limit"]) == 25.0
                break


def test_engagement_stream_content_type_fresh(tmp_path):
    """For a freshly created (not-yet-started) engagement in terminal state after completion,
    the stream returns text/event-stream content type."""
    app = _make_app(tmp_path)
    client = TestClient(app)

    create_resp = client.post(
        "/engagements",
        json={"target_url": "https://example.com", "budget_usd": 5, "approval_mode": "none"},
    )
    eid = create_resp.json()["engagement_id"]
    _run_to_terminal(client, eid)

    with client.stream("GET", f"/engagements/{eid}/stream") as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
