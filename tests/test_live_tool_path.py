from __future__ import annotations

"""FIX 5: Live-path regression test.

Drives DiscoveryAgent.run() with a scripted planner, fake HexStrikeClient, and
a real SecurityToolGate against a simulated juice-shop engagement.  Locks:
  - bare-host nmap target not rejected (Fix 1)
  - tool.invoked events emitted (H1 from prior C1-H4 fixes)
  - nmap output folded into hosts + tech_stack
  - error field present in the second plan_next's recent_observations (Fix 2)
"""

import logging
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from blackbox_service.agents_v2.base import AgentContext, AgentStep
from blackbox_service.agents_v2.discovery import DiscoveryAgent
from blackbox_service.engagement_models import BudgetState, EngagementRecord
from blackbox_service.toolchannel.security_gate import SecurityToolGate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_engagement(
    target_url: str = "http://juice-shop:3000/#/",
    budget: float = 100.0,
) -> EngagementRecord:
    rec = EngagementRecord(
        engagement_id="eng-regression",
        target_url=target_url,
    )
    rec.budget = BudgetState(limit_usd=budget)
    return rec


def _make_ctx(target_url: str = "http://juice-shop:3000/#/") -> AgentContext:
    return AgentContext(
        engagement_id="eng-regression",
        run_id="run-regression",
        target_url=target_url,
        max_steps=5,
    )


_NMAP_OUTPUT = {
    "stdout": (
        "80/tcp open http Apache httpd 2.4.51\n"
        "3000/tcp open http Node.js Express"
    ),
    "raw": {
        "hosts": [
            {
                "address": "172.28.0.3",
                "ports": [
                    {"port": 80,   "service": "Apache/2.4.51"},
                    {"port": 3000, "service": "Node.js Express"},
                ],
            }
        ]
    },
    "artifacts": [],
    "error": None,
}

_NUCLEI_OUTPUT = {
    "stdout": "",
    "raw": {
        "findings": [
            {
                "template_id": "xss-reflected",
                "name": "Reflected XSS",
                "severity": "medium",
                "matched_at": "http://juice-shop:3000/search?q=test",
                "matcher_status": "true",
                "classification": "xss",
            }
        ]
    },
    "artifacts": [],
    "error": None,
}


# ---------------------------------------------------------------------------
# Scripted agent: step 0 = nmap (bare host), step 1 = nuclei (full URL), done
# ---------------------------------------------------------------------------

class _ScriptedDiscoveryAgent(DiscoveryAgent):
    """Discovery agent with scripted plan_next; captures observations for assertions."""

    def __init__(self, bie, tool_gate=None):
        super().__init__(bie, tool_gate=tool_gate)
        self.plan_calls: list[dict[str, Any]] = []

    def plan_next(self, ctx, local_state, observations):
        step = len(observations)
        self.plan_calls.append({"step": step, "observations": list(observations)})

        if step == 0:
            return AgentStep(
                done=False,
                goal="nmap scan bare host",
                action_type="nmap_scan",
                params={"target": "juice-shop"},  # bare host — the proven-failing case
            )
        if step == 1:
            return AgentStep(
                done=False,
                goal="nuclei scan full url",
                action_type="nuclei_scan",
                params={"target": "http://juice-shop:3000"},
            )
        return AgentStep(done=True, goal="done")


# ---------------------------------------------------------------------------
# Main regression test
# ---------------------------------------------------------------------------

def test_live_tool_path_nmap_bare_host_and_nuclei(tmp_path):
    """
    Full live-path harness:
    1. nmap_scan{target:"juice-shop"} must NOT be rejected (Fix 1).
    2. tool.invoked events emitted, tool.rejected absent (H1 + Fix 1).
    3. nmap output folded into hosts + tech_stack.
    4. Second plan_next receives observations with "error" key (Fix 2).
    """
    rec = _make_engagement()

    # Fake HexStrike client returns canned outputs
    fake_client = MagicMock()
    call_count = [0]

    def fake_invoke(tool, params):
        n = call_count[0]
        call_count[0] += 1
        if tool == "nmap_scan":
            return {"ok": True, **_NMAP_OUTPUT}
        if tool == "nuclei_scan":
            return {"ok": True, **_NUCLEI_OUTPUT}
        return {"ok": False, "raw": None, "stdout": "", "artifacts": [], "error": "unknown_tool"}

    fake_client.invoke.side_effect = fake_invoke

    gate = SecurityToolGate(
        client=fake_client,
        engagement=rec,
        artifacts_dir=tmp_path,
        logger=logging.getLogger("regression"),
        budget_hard_cap_usd=100.0,
    )

    fake_bie = MagicMock()
    agent = _ScriptedDiscoveryAgent(fake_bie, tool_gate=gate)
    ctx = _make_ctx()

    result = agent.run(ctx)

    # ── Assertion 1: nmap_scan was NOT rejected ──────────────────────────
    rejected_events = [e for e in rec.events if e.type == "tool.rejected"]
    assert len(rejected_events) == 0, (
        f"tool.rejected events found — scope check is still blocking: {rejected_events}"
    )

    # ── Assertion 2: tool.invoked events were emitted ────────────────────
    invoked_events = [e for e in rec.events if e.type == "tool.invoked"]
    assert len(invoked_events) >= 2, (
        f"Expected 2 tool.invoked events, got {len(invoked_events)}"
    )
    invoked_types = [e.payload.get("tool") for e in invoked_events]
    assert "nmap_scan" in invoked_types, "nmap_scan not in tool.invoked events"
    assert "nuclei_scan" in invoked_types, "nuclei_scan not in tool.invoked events"

    # ── Assertion 3: nmap output folded into hosts + tech_stack ──────────
    hosts = result.get("hosts", [])
    tech_stack = result.get("tech_stack", [])
    assert "172.28.0.3" in hosts, (
        f"nmap host IP not in summarize() hosts: {hosts}"
    )
    assert any("Apache" in t or "Node" in t for t in tech_stack), (
        f"nmap service banners not in tech_stack: {tech_stack}"
    )

    # ── Assertion 4: second plan_next call has error in observations ──────
    assert len(agent.plan_calls) >= 2, "Agent stopped before second plan_next call"
    second_call_obs = agent.plan_calls[1]["observations"]
    assert len(second_call_obs) >= 1, "Second plan_next received no observations"
    first_obs = second_call_obs[0]
    assert "error" in first_obs, (
        f"'error' key missing from observations passed to second plan_next: {first_obs}"
    )


def test_live_path_nmap_scope_gate_end_to_end(tmp_path):
    """Verify that the full gate.invoke call chain accepts bare-host nmap against juice-shop:3000."""
    rec = _make_engagement(target_url="http://juice-shop:3000/#/")

    fake_client = MagicMock()
    fake_client.invoke.return_value = {
        "ok": True, "raw": {"hosts": []}, "stdout": "nmap done",
        "artifacts": [], "error": None,
    }
    gate = SecurityToolGate(
        client=fake_client,
        engagement=rec,
        artifacts_dir=tmp_path,
        logger=logging.getLogger("test"),
        budget_hard_cap_usd=100.0,
    )

    # The proven-failing target format
    result = gate.invoke("nmap_scan", {"target": "juice-shop"})
    assert result["ok"] is True, f"Bare-host nmap still rejected: {result.get('error')}"
    fake_client.invoke.assert_called_once()

    # Localhost alias equivalence
    rec2 = _make_engagement(target_url="http://localhost:3000")
    gate2 = SecurityToolGate(
        client=fake_client,
        engagement=rec2,
        artifacts_dir=tmp_path,
        logger=logging.getLogger("test"),
        budget_hard_cap_usd=100.0,
    )
    result2 = gate2.invoke("nmap_scan", {"target": "127.0.0.1"})
    assert result2["ok"] is True, f"localhost<->127.0.0.1 alias not working: {result2.get('error')}"
