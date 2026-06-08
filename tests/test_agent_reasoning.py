from __future__ import annotations

from blackbox_service.agent import ScriptedPlanner
from blackbox_service.service import BlackboxService


def test_agent_step_emits_thought_and_hypothesis(tmp_path):
    planner = ScriptedPlanner(
        script=[
            {
                "thought": "The page loaded, I should inspect script-side behavior.",
                "hypothesis": "Console may reveal framework/runtime hints.",
                "action_type": "read_console",
                "params": {},
                "done": False,
            },
            {
                "thought": "I captured first observations.",
                "hypothesis": "Stop now; this is enough for this smoke test.",
                "action_type": "snapshot",
                "params": {},
                "done": True,
            },
        ]
    )
    service = BlackboxService(db_path=tmp_path / "events.db", use_playwright=False, planner=planner)
    run = service.start_run(targets=["https://example.com"], options={})

    summary = service.run_agent_steps(run.run_id, max_steps=5, step_delay_ms=0)

    assert summary["status"] == "completed"
    events = service.list_memory(run.run_id)
    event_types = [event.type for event in events]
    assert "agent.thought" in event_types
    assert "agent.hypothesis" in event_types
    assert "observation.console" in event_types
    assert "artifact.screenshot" in event_types
