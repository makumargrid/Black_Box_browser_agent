from __future__ import annotations

from blackbox_service.agents_v2.access_test import AccessTestAgent
from blackbox_service.agents_v2.base import AgentContext
from blackbox_service.bie import BIEOutcome


class _FakeBIE:
    def __init__(self) -> None:
        self.actions: list[str] = []

    def request(self, req):
        self.actions.append(req.action_type)
        if req.action_type == "ai_navigate":
            return BIEOutcome(ok=True, tier_used=4, action_type=req.action_type, result={"route_memory": []}, cost_usd=0.02)
        return BIEOutcome(ok=True, tier_used=2, action_type=req.action_type, result={}, cost_usd=0.001)


def test_access_agent_runs_and_returns_valid_summary():
    """Agent is now LLM-driven: without an API key it terminates gracefully after plan_next returns done=True."""
    bie = _FakeBIE()
    agent = AccessTestAgent(bie)  # type: ignore[arg-type]
    ctx = AgentContext(
        engagement_id="eng-1",
        run_id="run-1",
        target_url="https://example.com",
        max_steps=3,
        step_delay_ms=0,
        state={"discovery_endpoints": []},
        # No anthropic_api_key → _call_llm returns done=True immediately
    )

    out = agent.run(ctx)

    # Agent must return a valid summary regardless of LLM availability
    assert "auth_status" in out
    assert "suspected_findings" in out
    assert isinstance(out["suspected_findings"], list)
    assert "observation_count" in out
