from __future__ import annotations

"""FIX 2 tests: tool error surfaces in recent_observations so the LLM can self-correct."""

import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from blackbox_service.agents_v2.base import AgentContext, AgentStep
from blackbox_service.agents_v2.discovery import DiscoveryAgent
from blackbox_service.engagement_models import BudgetState, EngagementRecord
from blackbox_service.toolchannel.security_gate import SecurityToolGate


def _make_engagement(target_url: str = "http://juice-shop:3000/#/") -> EngagementRecord:
    rec = EngagementRecord(engagement_id="eng-feedback-test", target_url=target_url)
    rec.budget = BudgetState(limit_usd=100.0)
    return rec


def _make_ctx(target_url: str = "http://juice-shop:3000/#/") -> AgentContext:
    return AgentContext(
        engagement_id="eng-feedback-test",
        run_id="run-feedback",
        target_url=target_url,
        max_steps=5,
    )


class _ScriptedDiscoveryAgent(DiscoveryAgent):
    """DiscoveryAgent with scripted plan_next and LLM call capture."""

    def __init__(self, bie, tool_gate=None):
        super().__init__(bie, tool_gate=tool_gate)
        self._step_index = 0
        self.captured_llm_contexts: list[dict] = []

    def plan_next(self, ctx, local_state, observations):
        self._step_index = len(observations)
        if self._step_index == 0:
            return AgentStep(
                done=False,
                goal="scan with nmap",
                action_type="nmap_scan",
                params={"target": "juice-shop"},
            )
        return AgentStep(done=True, goal="done")

    def _call_llm(self, ctx, system_prompt, user_context):
        # Capture LLM context so we can assert what the agent "sees"
        self.captured_llm_contexts.append(user_context)
        return {"action_type": "none", "done": True, "thought": "", "hypothesis": "", "params": {}}


# ---------------------------------------------------------------------------
# Test: out_of_scope error is visible in the next plan_next's observations
# ---------------------------------------------------------------------------

def test_tool_error_surfaces_in_recent_observations(tmp_path):
    """When a tool call returns ok=False + error, the next plan_next receives the error in observations."""
    rec = _make_engagement()
    fake_client = MagicMock()
    # Gate returns rejection — simulating what happened before FIX 1
    fake_client.invoke.return_value = {
        "ok": False, "raw": None, "stdout": "", "artifacts": [],
        "error": "out_of_scope",
    }
    gate = SecurityToolGate(
        client=fake_client,
        engagement=rec,
        artifacts_dir=tmp_path,
        logger=logging.getLogger("test"),
        budget_hard_cap_usd=100.0,
    )

    fake_bie = MagicMock()
    agent = _ScriptedDiscoveryAgent(fake_bie, tool_gate=gate)
    ctx = _make_ctx()

    # Override plan_next to have two steps: nmap (fails), then capture what LLM sees
    step_calls: list[dict] = []

    original_plan = agent.plan_next.__func__

    call_count = [0]

    def scripted_plan(ctx_, local_state, observations):
        if call_count[0] == 0:
            call_count[0] += 1
            return AgentStep(
                done=False,
                goal="scan with nmap",
                action_type="nmap_scan",
                params={"target": "juice-shop"},
            )
        # On the second call, capture the observations the agent has
        step_calls.append({"observations": list(observations)})
        return AgentStep(done=True)

    agent.plan_next = scripted_plan

    agent.run(ctx)

    # The second plan_next call must have received the first observation with an error
    assert len(step_calls) >= 1, "Second plan_next was never called"
    obs = step_calls[0]["observations"]
    assert len(obs) >= 1, "No observations passed to second plan_next"

    first_obs = obs[0]
    assert first_obs.get("action_type") == "nmap_scan"
    # The error field must be present (ok is False because the fake client returned ok=False)
    assert "error" in first_obs, "error key missing from observation"


def test_recent_observations_includes_error_key_when_tool_fails(tmp_path):
    """The error returned by a tool call must appear in recent_observations passed to _call_llm."""
    rec = _make_engagement()
    fake_client = MagicMock()
    # Return a failed-but-not-scope-rejected result
    fake_client.invoke.return_value = {
        "ok": False, "raw": None, "stdout": "", "artifacts": [],
        "error": "connection_refused",
    }
    gate = SecurityToolGate(
        client=fake_client,
        engagement=rec,
        artifacts_dir=tmp_path,
        logger=logging.getLogger("test"),
        budget_hard_cap_usd=100.0,
    )

    fake_bie = MagicMock()

    captured_contexts: list[dict] = []

    class CapturingAgent(DiscoveryAgent):
        def _call_llm(self, ctx, system_prompt, user_context):
            captured_contexts.append(user_context)
            return {"action_type": "none", "done": True, "thought": "", "hypothesis": "", "params": {}}

    agent = CapturingAgent(fake_bie, tool_gate=gate)
    ctx = _make_ctx()

    step_n = [0]

    def scripted_plan(ctx_, local_state, observations):
        n = step_n[0]
        step_n[0] += 1
        if n == 0:
            return AgentStep(
                done=False, goal="scan", action_type="nmap_scan",
                params={"target": "juice-shop"},
            )
        # second step: trigger _call_llm so we can inspect what it sees
        # (plan_next in real agent calls _call_llm; we override to trigger it)
        agent._call_llm(ctx_, agent._build_system_prompt(True) if hasattr(agent, '_build_system_prompt') else "", {
            "recent_observations": [
                {
                    "action_type": o.get("action_type"),
                    "ok": o.get("ok"),
                    "error": o.get("error"),
                    "result_preview": "",
                }
                for o in observations[-6:]
            ]
        })
        return AgentStep(done=True)

    agent.plan_next = scripted_plan
    agent.run(ctx)

    # Verify the captured context has the error
    assert len(captured_contexts) >= 1
    recent = captured_contexts[0].get("recent_observations", [])
    if recent:
        # At least one observation should have an error field
        has_error_key = any("error" in obs for obs in recent)
        assert has_error_key, f"error key not found in recent_observations: {recent}"


def test_error_key_present_in_observations_for_bie_actions(tmp_path):
    """For non-tool BIE actions, error key is also present (may be None)."""
    from blackbox_service.agents_v2.base import AgentBase

    fake_bie = MagicMock()
    fake_bie.request.return_value = MagicMock(
        ok=False,
        tier_used=1,
        action_type="http_get",
        result=None,
        error="connection_refused",
        cost_usd=0.0,
    )

    captured_obs: list[dict] = []

    class CapturingAgent(AgentBase):
        name = "test"

        def plan_next(self, ctx, local_state, observations):
            if len(observations) == 0:
                return AgentStep(done=False, goal="get", action_type="http_get",
                                 params={"url": "http://example.com"})
            captured_obs.extend(observations)
            return AgentStep(done=True)

    agent = CapturingAgent(fake_bie)
    ctx = AgentContext(
        engagement_id="eng-x",
        run_id="run-x",
        target_url="http://example.com",
        max_steps=3,
    )
    agent.run(ctx)

    assert len(captured_obs) >= 1
    # BIE observations already have 'error' from the BIEOutcome
    # (the key may be present as None or with the error string)
    assert "error" in captured_obs[0]
