from __future__ import annotations

from blackbox_service.demo import build_dashboard_url, run_demo_actions


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def create_run(self, targets, options):
        self.calls.append(("create_run", {"targets": targets, "options": options}))
        return {"run_id": "run-123", "active_tab_id": "tab-1", "status": "running"}

    def run_action(self, run_id, action_type, params):
        self.calls.append(
            (
                "run_action",
                {
                    "run_id": run_id,
                    "action_type": action_type,
                    "params": params,
                },
            )
        )
        return {"ok": True, "action_type": action_type, "result": {}}

    def list_memory(self, run_id):
        self.calls.append(("list_memory", {"run_id": run_id}))
        return {
            "run_id": run_id,
            "events": [
                {"type": "action.open_tab"},
                {"type": "action.eval_js"},
                {"type": "action.inject_html"},
                {"type": "observation.console"},
                {"type": "observation.network"},
                {"type": "artifact.screenshot"},
            ],
        }


def test_run_demo_actions_executes_expected_capabilities():
    client = _FakeClient()
    summary = run_demo_actions(client=client, target_url="http://127.0.0.1:3000/#/")

    assert summary["run_id"] == "run-123"
    assert summary["action_count"] >= 6
    assert "action.open_tab" in summary["event_types"]
    assert "observation.console" in summary["event_types"]
    assert "artifact.screenshot" in summary["event_types"]


def test_build_dashboard_url_supports_target_and_autorun_flags():
    out = build_dashboard_url(
        base_url="http://127.0.0.1:8080",
        target_url="https://example.com/app",
        autorun=True,
        autostart_agent=True,
    )
    assert out.startswith("http://127.0.0.1:8080/dashboard?")
    assert "target=https%3A%2F%2Fexample.com%2Fapp" in out
    assert "autorun=1" in out
    assert "autostart_agent=1" in out
