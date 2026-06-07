from __future__ import annotations

"""Tests for per-step agent reasoning events (FIX 2) and agent.step stream."""

import logging
from unittest.mock import MagicMock

from blackbox_service.agents_v2.base import AgentBase, AgentContext, AgentStep
from blackbox_service.engagement_models import BudgetState, EngagementRecord


def _make_ctx():
    return AgentContext(
        engagement_id="eng-step-test",
        run_id="run-step",
        target_url="http://example.com",
        max_steps=5,
    )


class _ScriptedAgent(AgentBase):
    """Agent that takes one BIE action then stops."""
    name = "test"
    _TOOL_ACTION_NAMES = frozenset()

    def plan_next(self, ctx, local_state, observations):
        if len(observations) == 0:
            return AgentStep(
                done=False,
                goal="testing step sink",
                action_type="http_get",
                params={"url": "http://example.com"},
            )
        return AgentStep(done=True)


def test_step_sink_receives_agent_step_event():
    """When step_sink is set, each agent step emits an agent.step event to the sink."""
    fake_bie = MagicMock()
    fake_bie.request.return_value = MagicMock(
        ok=True, tier_used=1, action_type="http_get",
        result={"status_code": 200, "body_preview": "ok"}, error=None, cost_usd=0.001
    )

    step_events: list[tuple[str, dict]] = []
    def sink(event_type: str, payload: dict) -> None:
        step_events.append((event_type, payload))

    agent = _ScriptedAgent(fake_bie, step_sink=sink)
    agent.run(_make_ctx())

    assert len(step_events) == 1
    etype, payload = step_events[0]
    assert etype == "agent.step"
    assert payload["agent"] == "test"
    assert payload["step"] == 1
    assert payload["action"] == "http_get"
    assert payload["ok"] is True


def test_step_sink_none_does_not_raise():
    """When step_sink is None, no event is emitted and the agent runs normally."""
    fake_bie = MagicMock()
    fake_bie.request.return_value = MagicMock(
        ok=True, tier_used=1, action_type="http_get",
        result={}, error=None, cost_usd=0.0
    )
    agent = _ScriptedAgent(fake_bie, step_sink=None)
    result = agent.run(_make_ctx())
    assert len(result.get("observations", [])) == 1


def test_step_sink_captures_error_on_failed_action():
    """Failed BIE action: step_sink receives ok=False and error string."""
    fake_bie = MagicMock()
    fake_bie.request.return_value = MagicMock(
        ok=False, tier_used=1, action_type="http_get",
        result=None, error="connection_refused", cost_usd=0.0
    )

    step_events: list[tuple[str, dict]] = []
    agent = _ScriptedAgent(fake_bie, step_sink=lambda t, p: step_events.append((t, p)))
    agent.run(_make_ctx())

    assert len(step_events) == 1
    _, payload = step_events[0]
    assert payload["ok"] is False
    assert payload["error"] == "connection_refused"
