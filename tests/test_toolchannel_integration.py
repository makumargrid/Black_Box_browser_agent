from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from blackbox_service.agents_v2.base import AgentBase, AgentContext
from blackbox_service.api import create_app
from blackbox_service.engagement_models import BudgetState, EngagementRecord


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


# ---------------------------------------------------------------------------
# Test 1: disabled HexStrike → engagement behaves identically to today
# ---------------------------------------------------------------------------

def test_hexstrike_disabled_engagement_identical_behavior(tmp_path):
    """With hexstrike_enabled=False the engagement lifecycle is byte-for-byte identical."""
    app = create_app(
        db_path=":memory:",
        use_playwright=False,
        artifacts_dir=tmp_path / "artifacts",
        hexstrike_enabled=False,
    )
    client = TestClient(app)

    caps = client.get("/health").json()["capabilities"]
    assert caps["toolchannel_enabled"] is False

    create_resp = client.post(
        "/engagements",
        json={"target_url": "https://example.com", "budget_usd": 50, "approval_mode": "none"},
    )
    assert create_resp.status_code == 201
    eid = create_resp.json()["engagement_id"]

    client.post(f"/engagements/{eid}/start", json={"max_steps_per_agent": 4, "step_delay_ms": 0})
    state = _wait_for_terminal(client, eid)
    assert state["status"] in {"completed", "budget_exhausted"}
    assert state["tool_invocations"] == []


# ---------------------------------------------------------------------------
# Test 2: AgentBase with gate=None returns clean negative from _invoke_tool
# ---------------------------------------------------------------------------

def test_agent_base_no_gate_returns_clean_negative():
    """_invoke_tool without a gate returns ok=False, no exception."""
    fake_bie = MagicMock()

    class DummyAgent(AgentBase):
        name = "dummy"
        _TOOL_ACTION_NAMES = frozenset({"test_tool"})

        def plan_next(self, ctx, local_state, observations):
            from blackbox_service.agents_v2.base import AgentStep
            return AgentStep(done=True)

    agent = DummyAgent(fake_bie, tool_gate=None)
    result = agent._invoke_tool("test_tool", {"target": "http://example.com"})
    assert result["ok"] is False
    assert result["error"] == "no_tool_gate"


# ---------------------------------------------------------------------------
# Test 3: AgentBase with fake gate records the call
# ---------------------------------------------------------------------------

def test_agent_base_with_gate_delegates_to_gate(tmp_path):
    """When tool_gate is set, _invoke_tool delegates to gate.invoke()."""
    fake_bie = MagicMock()
    fake_gate = MagicMock()
    fake_gate.invoke.return_value = {
        "ok": True, "raw": {}, "stdout": "done", "artifacts": [], "error": None
    }

    class DummyAgent(AgentBase):
        name = "dummy"
        _TOOL_ACTION_NAMES = frozenset({"nmap_scan"})

        def plan_next(self, ctx, local_state, observations):
            from blackbox_service.agents_v2.base import AgentStep
            return AgentStep(done=True)

    agent = DummyAgent(fake_bie, tool_gate=fake_gate)
    result = agent._invoke_tool("nmap_scan", {"target": "http://example.com"})
    assert result["ok"] is True
    fake_gate.invoke.assert_called_once_with("nmap_scan", {"target": "http://example.com"})


# ---------------------------------------------------------------------------
# Test 4: Tool-action observations are routed through gate, not BIE
# ---------------------------------------------------------------------------

def test_tool_action_routes_through_gate_not_bie(tmp_path):
    """When a plan_next returns a tool action, run() uses _invoke_tool not BIE.request."""
    fake_bie = MagicMock()
    fake_gate = MagicMock()
    fake_gate.invoke.return_value = {
        "ok": True, "raw": {"hosts": []}, "stdout": "nmap done", "artifacts": [], "error": None
    }
    call_count = [0]

    class SingleToolAgent(AgentBase):
        name = "test"
        _TOOL_ACTION_NAMES = frozenset({"nmap_scan"})

        def plan_next(self, ctx, local_state, observations):
            from blackbox_service.agents_v2.base import AgentStep
            if len(observations) == 0:
                return AgentStep(
                    done=False,
                    goal="scan",
                    action_type="nmap_scan",
                    params={"target": "http://example.com"},
                )
            return AgentStep(done=True)

    agent = SingleToolAgent(fake_bie, tool_gate=fake_gate)
    ctx = AgentContext(
        engagement_id="eng-x",
        run_id="run-x",
        target_url="http://example.com",
        max_steps=5,
    )
    result = agent.run(ctx)

    fake_gate.invoke.assert_called_once()
    fake_bie.request.assert_not_called()
    assert result["observations"][0]["action_type"] == "nmap_scan"
    assert result["observations"][0]["ok"] is True
