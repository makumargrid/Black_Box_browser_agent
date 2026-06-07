from __future__ import annotations

import time

from fastapi.testclient import TestClient

from blackbox_service.api import create_app
from blackbox_service.engagement_models import SuspectedFinding


def _make_app(tmp_path):
    return create_app(db_path=":memory:", use_playwright=False, artifacts_dir=tmp_path / "artifacts")


def _wait_for_terminal(client: TestClient, engagement_id: str, timeout_s: float = 10.0) -> dict:
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


# ---------------------------------------------------------------------------
# C1 tests
# ---------------------------------------------------------------------------

def test_mandatory_mode_pauses_once_and_reaches_done(tmp_path):
    """mandatory mode: engagement pauses exactly once; after approval it reaches 'done', not a second pause."""
    app = _make_app(tmp_path)
    client = TestClient(app)

    create_resp = client.post(
        "/engagements",
        json={"target_url": "https://example.com", "budget_usd": 10, "approval_mode": "mandatory"},
    )
    assert create_resp.status_code == 201
    eid = create_resp.json()["engagement_id"]

    client.post(f"/engagements/{eid}/start", json={"max_steps_per_agent": 3, "step_delay_ms": 0})
    state = _wait_for_terminal(client, eid)

    # First pause must happen
    assert state["status"] == "paused_for_approval", f"Expected first pause, got {state['status']}"

    # Approve — flow must proceed to confirm_evidence then completed, NOT pause again
    approve_resp = client.post(
        f"/engagements/{eid}/approval",
        json={"approved": True, "note": "test approval"},
    )
    assert approve_resp.status_code == 200
    assert approve_resp.json()["approval_granted"] is True

    state = _wait_for_terminal(client, eid)
    assert state["status"] in {"completed", "budget_exhausted"}, (
        f"After approval expected completed/budget_exhausted, got {state['status']}"
    )
    # Must NOT be paused a second time
    assert state["status"] != "paused_for_approval"
    # current_phase must have moved past "approval"
    assert state["current_phase"] not in {"approval", "init"}, (
        f"Phase stuck at {state['current_phase']}"
    )

    # Verify paused_for_approval event appears exactly once
    events_resp = client.get(f"/engagements/{eid}/events")
    event_types = [e["type"] for e in events_resp.json()["events"]]
    pause_count = event_types.count("engagement.paused_for_approval")
    assert pause_count == 1, f"Expected exactly 1 pause event, got {pause_count}"


def test_optional_mode_with_injected_findings_pauses_once(tmp_path):
    """optional mode with suspected findings: pauses once; approving leads to terminal state."""
    app = _make_app(tmp_path)
    client = TestClient(app)

    create_resp = client.post(
        "/engagements",
        json={"target_url": "https://example.com", "budget_usd": 10, "approval_mode": "optional"},
    )
    eid = create_resp.json()["engagement_id"]
    client.post(f"/engagements/{eid}/start", json={"max_steps_per_agent": 3, "step_delay_ms": 0})

    state = _wait_for_terminal(client, eid)

    if state["status"] == "paused_for_approval":
        approve_resp = client.post(
            f"/engagements/{eid}/approval",
            json={"approved": True, "note": "test"},
        )
        assert approve_resp.status_code == 200
        state = _wait_for_terminal(client, eid)
        assert state["status"] != "paused_for_approval", "Double-pause detected in optional mode"

    assert state["status"] in {"completed", "budget_exhausted"}


def test_optional_mode_no_approval_needed_completes(tmp_path):
    """approval_mode=none: engagement must reach completed without ever pausing."""
    app = _make_app(tmp_path)
    client = TestClient(app)

    create_resp = client.post(
        "/engagements",
        json={"target_url": "https://example.com", "budget_usd": 10, "approval_mode": "none"},
    )
    eid = create_resp.json()["engagement_id"]
    client.post(f"/engagements/{eid}/start", json={"max_steps_per_agent": 3, "step_delay_ms": 0})

    state = _wait_for_terminal(client, eid)
    assert state["status"] in {"completed", "budget_exhausted"}, f"Got {state['status']}"
    assert state["status"] != "paused_for_approval"

    events_resp = client.get(f"/engagements/{eid}/events")
    event_types = [e["type"] for e in events_resp.json()["events"]]
    assert "engagement.paused_for_approval" not in event_types


def test_approval_granted_flag_prevents_repause(tmp_path):
    """Direct test of the fix: approval_granted=True must short-circuit needs_approval."""
    app = _make_app(tmp_path)
    client = TestClient(app)

    create_resp = client.post(
        "/engagements",
        json={"target_url": "https://example.com", "budget_usd": 10, "approval_mode": "mandatory"},
    )
    eid = create_resp.json()["engagement_id"]

    client.post(f"/engagements/{eid}/start", json={"max_steps_per_agent": 2, "step_delay_ms": 0})
    state = _wait_for_terminal(client, eid)
    assert state["status"] == "paused_for_approval"

    # Approve
    client.post(f"/engagements/{eid}/approval", json={"approved": True, "note": "ok"})
    state2 = _wait_for_terminal(client, eid)

    # The engagement must never set paused_for_approval again
    assert state2["status"] != "paused_for_approval", (
        "BUG C1 not fixed: mandatory mode re-paused after approval"
    )
