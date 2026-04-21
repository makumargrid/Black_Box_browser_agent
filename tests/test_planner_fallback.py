from __future__ import annotations

from blackbox_service.agent import AgentDecision, FailoverPlanner, build_planner


class _PrimaryPlanner:
    def __init__(self, *args, **kwargs):
        self.calls = 0

    def next_decision(self, context):
        self.calls += 1
        raise RuntimeError("anthropic auth failed")


class _FallbackPlanner:
    def __init__(self, *args, **kwargs):
        self.calls = 0

    def next_decision(self, context):
        self.calls += 1
        return AgentDecision(
            thought="fallback",
            hypothesis="gemini recovered",
            action_type="none",
            params={},
            done=True,
        )


def test_failover_planner_switches_to_fallback_after_primary_error():
    planner = FailoverPlanner(primary=_PrimaryPlanner(), fallback=_FallbackPlanner())

    first = planner.next_decision({"step_index": 0})
    second = planner.next_decision({"step_index": 1})

    assert first.thought == "fallback"
    assert second.thought == "fallback"


def test_build_planner_wraps_anthropic_with_gemini_fallback(monkeypatch):
    monkeypatch.setattr("blackbox_service.agent.AnthropicPlanner", _PrimaryPlanner)
    monkeypatch.setattr("blackbox_service.agent.GeminiPlanner", _FallbackPlanner)

    planner = build_planner(
        anthropic_api_key="ant-key",
        anthropic_model="claude-opus-4-7",
        gemini_api_key="gem-key",
        gemini_model="gemini-2.5-flash",
    )

    result = planner.next_decision({"step_index": 0})
    assert result.hypothesis == "gemini recovered"
