from __future__ import annotations

"""Fix 2 tests: when ANTHROPIC_API_KEY is absent, agents run 0 steps.
The orchestrator must emit phase.warning events and set rec.last_error
so the operator can see WHY nothing happened instead of a silent success."""

import time

from fastapi.testclient import TestClient

from blackbox_service.api import create_app


def _make_app(tmp_path, api_key: str = ""):
    return create_app(
        db_path=":memory:",
        use_playwright=False,
        artifacts_dir=tmp_path / "artifacts",
        anthropic_api_key=api_key,
    )


def _wait_for_terminal(client: TestClient, eid: str, timeout_s: float = 10.0) -> dict:
    deadline = time.time() + timeout_s
    last = {}
    while time.time() < deadline:
        resp = client.get(f"/engagements/{eid}")
        assert resp.status_code == 200
        last = resp.json()
        if last["status"] in {"completed", "failed", "paused_for_approval", "budget_exhausted"}:
            return last
        time.sleep(0.05)
    return last


def test_phase_warning_emitted_when_no_api_key(tmp_path):
    """Without ANTHROPIC_API_KEY, each agent emits phase.warning(reason=no_llm_key)."""
    app = _make_app(tmp_path, api_key="")
    client = TestClient(app)

    create_resp = client.post(
        "/engagements",
        json={"target_url": "https://example.com", "budget_usd": 10, "approval_mode": "none"},
    )
    assert create_resp.status_code == 201
    eid = create_resp.json()["engagement_id"]

    client.post(f"/engagements/{eid}/start", json={"max_steps_per_agent": 3, "step_delay_ms": 0})
    state = _wait_for_terminal(client, eid)

    assert state["status"] in {"completed", "budget_exhausted"}, f"Unexpected status: {state['status']}"

    events_resp = client.get(f"/engagements/{eid}/events")
    events = events_resp.json()["events"]
    event_types = [e["type"] for e in events]

    # At least one phase.warning event must be present
    warning_events = [e for e in events if e["type"] == "phase.warning"]
    assert len(warning_events) >= 1, (
        f"No phase.warning events emitted. Event types seen: {event_types}"
    )

    # Each warning must have reason=no_llm_key
    for w in warning_events:
        assert w["payload"].get("reason") == "no_llm_key", (
            f"phase.warning has unexpected reason: {w['payload']}"
        )
        assert "ANTHROPIC_API_KEY" in w["payload"].get("message", "")


def test_last_error_set_when_no_api_key(tmp_path):
    """Without ANTHROPIC_API_KEY, rec.last_error must explain the issue clearly."""
    app = _make_app(tmp_path, api_key="")
    client = TestClient(app)

    create_resp = client.post(
        "/engagements",
        json={"target_url": "https://example.com", "budget_usd": 10, "approval_mode": "none"},
    )
    eid = create_resp.json()["engagement_id"]
    client.post(f"/engagements/{eid}/start", json={"max_steps_per_agent": 3, "step_delay_ms": 0})
    state = _wait_for_terminal(client, eid)

    last_error = state.get("last_error")
    assert last_error is not None, "last_error should be set when API key is missing"
    assert "ANTHROPIC_API_KEY" in last_error, (
        f"last_error does not mention ANTHROPIC_API_KEY: {last_error!r}"
    )


def test_no_phase_warning_when_api_key_present(tmp_path):
    """With ANTHROPIC_API_KEY set, no phase.warning(no_llm_key) should be emitted."""
    app = _make_app(tmp_path, api_key="sk-ant-fake-but-present")
    client = TestClient(app)

    create_resp = client.post(
        "/engagements",
        json={"target_url": "https://example.com", "budget_usd": 10, "approval_mode": "none"},
    )
    eid = create_resp.json()["engagement_id"]
    client.post(f"/engagements/{eid}/start", json={"max_steps_per_agent": 3, "step_delay_ms": 0})
    state = _wait_for_terminal(client, eid)

    events_resp = client.get(f"/engagements/{eid}/events")
    events = events_resp.json()["events"]
    no_llm_key_warnings = [
        e for e in events
        if e["type"] == "phase.warning" and e["payload"].get("reason") == "no_llm_key"
    ]
    assert len(no_llm_key_warnings) == 0, (
        "phase.warning(no_llm_key) emitted even though API key was present"
    )
