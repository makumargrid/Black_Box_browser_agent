from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from blackbox_service.agents_v2.base import AgentContext, AgentStep
from blackbox_service.agents_v2.confirm_evidence import ConfirmEvidenceAgent
from blackbox_service.engagement_models import BudgetState, EngagementRecord, SuspectedFinding
from blackbox_service.toolchannel.security_gate import SecurityToolGate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_record(approval_granted: bool = False) -> EngagementRecord:
    rec = EngagementRecord(engagement_id="eng-test", target_url="http://example.com")
    rec.budget = BudgetState(limit_usd=100.0, spent_usd=0.0)
    rec.approval_granted = approval_granted
    return rec


def _make_gate(rec: EngagementRecord, client_ok: bool = True) -> SecurityToolGate:
    fake_client = MagicMock()
    if client_ok:
        fake_client.invoke.return_value = {
            "ok": True,
            "raw": {"url": "http://example.com/login", "injectable": True},
            "stdout": "[CRITICAL] Parameter 'id' is injectable (boolean-based blind)\nDatabase: juice_shop",
            "artifacts": [],
            "error": None,
        }
    else:
        fake_client.invoke.return_value = {
            "ok": False,
            "raw": {},
            "stdout": "",
            "artifacts": [],
            "error": "no injection found",
        }
    artifacts_dir = Path(tempfile.mkdtemp())
    return SecurityToolGate(
        client=fake_client,
        engagement=rec,
        artifacts_dir=artifacts_dir,
        logger=logging.getLogger("test"),
        budget_hard_cap_usd=100.0,
    )


def _make_ctx(target_url: str = "http://example.com") -> AgentContext:
    return AgentContext(
        engagement_id="eng-test",
        run_id="run-test",
        target_url=target_url,
        max_steps=10,
    )


def _make_suspected_finding() -> SuspectedFinding:
    return SuspectedFinding(
        finding_id="sf-abc1234567",
        vuln_type="sql_injection",
        title="Possible SQLi in login form",
        endpoint="http://example.com/login",
        method="POST",
        severity="medium",
        confidence=6,
        evidence_snippet="differential response on login",
    )


# ---------------------------------------------------------------------------
# Test 1: sqlmap_probe pre-approval → tool.rejected event, no confirmation
# ---------------------------------------------------------------------------

def test_sqlmap_probe_rejected_before_approval():
    """sqlmap_probe requested before HITL approval → SecurityToolGate rejects it; tool.rejected event recorded."""
    rec = _make_record(approval_granted=False)
    gate = _make_gate(rec, client_ok=True)

    finding = _make_suspected_finding()
    fake_bie = MagicMock()
    agent = ConfirmEvidenceAgent(fake_bie, tool_gate=gate)
    ctx = _make_ctx()

    # Simulate plan_next returning sqlmap_probe (before approval)
    call_count = [0]

    def fake_plan_next(ctx_, local_state, observations):
        if call_count[0] == 0:
            call_count[0] += 1
            return AgentStep(
                done=False,
                goal="probe sqli",
                action_type="sqlmap_probe",
                params={"target": "http://example.com/login"},
                note="sqlmap_confirm:sf-abc1234567",
            )
        return AgentStep(done=True)

    agent.plan_next = fake_plan_next

    ctx.state = {"suspected_findings": [finding.model_dump(mode="json")]}
    result = agent.run(ctx)

    # Gate should have rejected sqlmap (approval not granted)
    rejected_events = [e for e in rec.events if e.type == "tool.rejected"]
    assert len(rejected_events) >= 1
    assert any("approval" in e.payload.get("reason", "") for e in rejected_events)

    # No HexStrike client call should have been made
    gate._client.invoke.assert_not_called()

    # No confirmed findings
    assert result["confirmed_findings"] == []


# ---------------------------------------------------------------------------
# Test 2: sqlmap_probe post-approval → passes, ConfirmedFinding created
# ---------------------------------------------------------------------------

def test_sqlmap_probe_allowed_after_approval():
    """sqlmap_probe after approval_granted=True → SecurityToolGate allows it; ConfirmedFinding created."""
    rec = _make_record(approval_granted=True)
    gate = _make_gate(rec, client_ok=True)

    finding = _make_suspected_finding()
    fake_bie = MagicMock()
    agent = ConfirmEvidenceAgent(fake_bie, tool_gate=gate)
    ctx = _make_ctx()

    call_count = [0]

    def fake_plan_next(ctx_, local_state, observations):
        if call_count[0] == 0:
            call_count[0] += 1
            return AgentStep(
                done=False,
                goal="probe sqli",
                action_type="sqlmap_probe",
                params={"target": "http://example.com/login"},
                note="sqlmap_confirm:sf-abc1234567",
            )
        return AgentStep(done=True)

    agent.plan_next = fake_plan_next

    ctx.state = {"suspected_findings": [finding.model_dump(mode="json")]}
    result = agent.run(ctx)

    # tool.invoked event should be present
    invoked_events = [e for e in rec.events if e.type == "tool.invoked"]
    assert len(invoked_events) >= 1

    # ToolInvocation recorded on the engagement
    assert len(rec.tool_invocations) >= 1
    assert rec.tool_invocations[0].tool_name == "sqlmap_probe"
    assert rec.tool_invocations[0].ok is True

    # Confirmed finding should be created
    assert len(result["confirmed_findings"]) == 1
    assert result["confirmed_findings"][0]["vuln_type"] == "sql_injection"


# ---------------------------------------------------------------------------
# Test 3: event sequence — rejected then approved
# ---------------------------------------------------------------------------

def test_sqlmap_event_sequence_rejected_then_approved():
    """Full sequence: rejected pre-approval, then approved, then passes."""
    # First run: no approval
    rec = _make_record(approval_granted=False)
    gate = _make_gate(rec, client_ok=True)

    # Direct gate invocation simulates what the agent would do
    result_before = gate.invoke("sqlmap_probe", {"target": "http://example.com/login"})
    assert result_before["ok"] is False
    assert result_before["error"] == "requires_hitl_approval"

    # Simulate HITL approval
    rec.approval_granted = True

    result_after = gate.invoke("sqlmap_probe", {"target": "http://example.com/login"})
    assert result_after["ok"] is True

    # Check event sequence
    event_types = [e.type for e in rec.events]
    rejected_idx = event_types.index("tool.rejected")
    invoked_idx = event_types.index("tool.invoked")
    assert rejected_idx < invoked_idx

    # tool_invocations should have exactly 1 entry (only the successful post-approval call)
    assert len(rec.tool_invocations) == 1
    assert rec.tool_invocations[0].ok is True


# ---------------------------------------------------------------------------
# Test 4: ConfirmEvidenceAgent plan_next includes sqlmap_probe when tools enabled
# ---------------------------------------------------------------------------

def test_confirm_agent_plan_next_includes_sqlmap_when_tools_enabled():
    fake_gate = MagicMock()
    agent = ConfirmEvidenceAgent(MagicMock(), tool_gate=fake_gate)
    ctx = _make_ctx()
    finding = _make_suspected_finding()
    local_state = agent.initialize_state(
        AgentContext(
            engagement_id="eng-x",
            run_id="run-x",
            target_url="http://example.com",
            max_steps=5,
            state={"suspected_findings": [finding.model_dump(mode="json")]},
        )
    )

    captured = {}

    def fake_call_llm(ctx_, system_prompt, user_context):
        captured["user_context"] = user_context
        return {"action_type": "none", "done": True, "thought": "", "hypothesis": "", "params": {}}

    agent._call_llm = fake_call_llm
    agent.plan_next(ctx, local_state, [])

    assert "sqlmap_probe" in captured["user_context"]["allowed_actions"]
    assert captured["user_context"]["tools_enabled"] is True
