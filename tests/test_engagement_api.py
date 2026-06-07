from __future__ import annotations

import time

from fastapi.testclient import TestClient

from blackbox_service.api import create_app


def _wait_for_terminal(client: TestClient, engagement_id: str, timeout_s: float = 5.0) -> dict:
    deadline = time.time() + timeout_s
    last = {}
    while time.time() < deadline:
        resp = client.get(f"/engagements/{engagement_id}")
        assert resp.status_code == 200
        last = resp.json()
        if last["status"] in {"completed", "failed", "paused_for_approval", "budget_exhausted"}:
            return last
        time.sleep(0.05)
    return last


def test_engagement_lifecycle_optional_approval(tmp_path):
    app = create_app(db_path=":memory:", use_playwright=False, artifacts_dir=tmp_path / "artifacts")
    client = TestClient(app)

    create_resp = client.post(
        "/engagements",
        json={"target_url": "https://example.com", "budget_usd": 50, "approval_mode": "optional"},
    )
    assert create_resp.status_code == 201
    engagement_id = create_resp.json()["engagement_id"]

    start_resp = client.post(
        f"/engagements/{engagement_id}/start",
        json={"max_steps_per_agent": 6, "step_delay_ms": 0},
    )
    assert start_resp.status_code == 200

    state = _wait_for_terminal(client, engagement_id)
    assert state["status"] in {"paused_for_approval", "completed"}

    if state["status"] == "paused_for_approval":
        approve_resp = client.post(
            f"/engagements/{engagement_id}/approval",
            json={"approved": True, "note": "approve for test"},
        )
        assert approve_resp.status_code == 200
        state = _wait_for_terminal(client, engagement_id)

    assert state["status"] in {"completed", "budget_exhausted"}

    report_resp = client.get(f"/engagements/{engagement_id}/report")
    assert report_resp.status_code == 200
    payload = report_resp.json()
    assert payload["engagement_id"] == engagement_id


def test_engagement_events_and_findings_endpoints(tmp_path):
    app = create_app(db_path=":memory:", use_playwright=False, artifacts_dir=tmp_path / "artifacts")
    client = TestClient(app)

    create_resp = client.post(
        "/engagements",
        json={"target_url": "https://example.com", "budget_usd": 10, "approval_mode": "none"},
    )
    engagement_id = create_resp.json()["engagement_id"]

    client.post(
        f"/engagements/{engagement_id}/start",
        json={"max_steps_per_agent": 4, "step_delay_ms": 0},
    )
    _ = _wait_for_terminal(client, engagement_id)

    ev_resp = client.get(f"/engagements/{engagement_id}/events")
    assert ev_resp.status_code == 200
    assert isinstance(ev_resp.json().get("events"), list)

    findings_resp = client.get(f"/engagements/{engagement_id}/findings")
    assert findings_resp.status_code == 200
    out = findings_resp.json()
    assert out["engagement_id"] == engagement_id
    assert "suspected_findings" in out
    assert "confirmed_findings" in out


def test_engagement_mandatory_approval_pause_and_reject(tmp_path):
    app = create_app(db_path=":memory:", use_playwright=False, artifacts_dir=tmp_path / "artifacts")
    client = TestClient(app)

    create_resp = client.post(
        "/engagements",
        json={"target_url": "https://example.com", "budget_usd": 10, "approval_mode": "mandatory"},
    )
    engagement_id = create_resp.json()["engagement_id"]

    client.post(
        f"/engagements/{engagement_id}/start",
        json={"max_steps_per_agent": 4, "step_delay_ms": 0},
    )

    state = _wait_for_terminal(client, engagement_id)
    assert state["status"] == "paused_for_approval"

    reject_resp = client.post(
        f"/engagements/{engagement_id}/approval",
        json={"approved": False, "note": "reject for test"},
    )
    assert reject_resp.status_code == 200

    final = client.get(f"/engagements/{engagement_id}").json()
    assert final["status"] == "completed"
    assert final["report"] is not None


def test_tool_invocations_endpoint_returns_empty_for_fresh_engagement(tmp_path):
    """GET /engagements/{id}/tool-invocations returns 200 + empty list for a new engagement."""
    app = create_app(db_path=":memory:", use_playwright=False, artifacts_dir=tmp_path / "artifacts")
    client = TestClient(app)

    create_resp = client.post(
        "/engagements",
        json={"target_url": "https://example.com", "budget_usd": 10, "approval_mode": "none"},
    )
    assert create_resp.status_code == 201
    engagement_id = create_resp.json()["engagement_id"]

    ti_resp = client.get(f"/engagements/{engagement_id}/tool-invocations")
    assert ti_resp.status_code == 200
    data = ti_resp.json()
    assert data["engagement_id"] == engagement_id
    assert data["tool_invocations"] == []


def test_tool_invocations_endpoint_404_for_unknown():
    """GET /engagements/unknown-id/tool-invocations returns 404."""
    app = create_app(db_path=":memory:", use_playwright=False)
    client = TestClient(app)
    resp = client.get("/engagements/does-not-exist/tool-invocations")
    assert resp.status_code == 404
