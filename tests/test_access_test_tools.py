from __future__ import annotations

from unittest.mock import MagicMock

from blackbox_service.agents_v2.access_test import AccessTestAgent, _cap_severity_pre_approval


def _make_agent(gate=None) -> AccessTestAgent:
    fake_bie = MagicMock()
    return AccessTestAgent(fake_bie, tool_gate=gate)


def _make_ctx():
    from blackbox_service.agents_v2.base import AgentContext
    return AgentContext(
        engagement_id="eng-x",
        run_id="run-x",
        target_url="http://example.com",
        max_steps=5,
    )


# ---------------------------------------------------------------------------
# Test _cap_severity_pre_approval
# ---------------------------------------------------------------------------

def test_cap_severity_passes_medium_and_below():
    assert _cap_severity_pre_approval("low") == "low"
    assert _cap_severity_pre_approval("medium") == "medium"


def test_cap_severity_downgrades_high_and_critical():
    assert _cap_severity_pre_approval("high") == "medium"
    assert _cap_severity_pre_approval("critical") == "medium"


def test_cap_severity_unknown_defaults_medium():
    assert _cap_severity_pre_approval("unknown") == "medium"
    assert _cap_severity_pre_approval("") == "medium"


# ---------------------------------------------------------------------------
# Test nuclei → SuspectedFinding conversion
# ---------------------------------------------------------------------------

def test_report_finding_creates_suspected_from_llm():
    """report_finding action records a SuspectedFinding from the LLM's own reasoning."""
    agent = _make_agent()
    ctx = _make_ctx()
    local_state = agent.initialize_state(ctx)

    obs = {
        "action_type": "report_finding",
        "ok": True,
        "result": {
            "vuln_type": "sql_injection",
            "title": "SQLi on login",
            "endpoint": "http://example.com/rest/user/login",
            "severity": "critical",  # should be capped to medium pre-approval
            "confidence": 9,
            "evidence_snippet": "500 on ' OR 1=1-- payload",
        },
        "cost_usd": 0.0,
    }
    agent._after_observation(local_state, obs)

    suspected = local_state["suspected"]
    assert len(suspected) == 1
    assert suspected[0].vuln_type == "sql_injection"
    assert suspected[0].endpoint == "http://example.com/rest/user/login"
    assert suspected[0].severity == "medium"  # critical capped pre-approval
    assert suspected[0].source_agent == "llm_reasoning"


def test_report_finding_ignored_without_vuln_type():
    """report_finding with no vuln_type is a no-op (avoids junk findings)."""
    agent = _make_agent()
    ctx = _make_ctx()
    local_state = agent.initialize_state(ctx)
    agent._after_observation(local_state, {"action_type": "report_finding", "result": {"title": "x"}})
    assert len(local_state["suspected"]) == 0


def test_nuclei_scan_converts_to_suspected_findings():
    """nuclei_scan results are converted to SuspectedFinding with correct fields."""
    agent = _make_agent()
    ctx = _make_ctx()
    local_state = agent.initialize_state(ctx)

    obs = {
        "action_type": "nuclei_scan",
        "ok": True,
        "tool_result": {
            "ok": True,
            "stdout": "",
            "raw": {
                "findings": [
                    {
                        "template_id": "cve-2021-44228",
                        "name": "Log4Shell RCE",
                        "severity": "critical",  # should be capped to medium
                        "matched_at": "http://example.com/login",
                        "matcher_status": "true",
                        "classification": "rce",
                    },
                    {
                        "template_id": "exposed-metrics",
                        "name": "Prometheus Metrics Exposed",
                        "severity": "medium",
                        "matched_at": "http://example.com/metrics",
                        "matcher_status": "true",
                        "classification": "exposure",
                    },
                ]
            },
            "artifacts": [],
            "error": None,
        },
        "cost_usd": 0.0,
    }
    agent._after_observation(local_state, obs)

    suspected = local_state["suspected"]
    assert len(suspected) == 2

    # First finding: critical should be capped to medium
    log4shell = next(f for f in suspected if "Log4Shell" in f.title)
    assert log4shell.severity == "medium"
    assert log4shell.confidence == 8
    assert log4shell.source_agent == "access_test:nuclei"
    assert "cve-2021-44228" in log4shell.evidence_snippet

    # Second finding: medium stays as medium
    metrics = next(f for f in suspected if "Metrics" in f.title)
    assert metrics.severity == "medium"
    assert metrics.endpoint == "http://example.com/metrics"


def test_nuclei_findings_deduplicated():
    """Same template_id+endpoint combo should not create duplicate SuspectedFindings."""
    agent = _make_agent()
    ctx = _make_ctx()
    local_state = agent.initialize_state(ctx)

    nuclei_obs = {
        "action_type": "nuclei_scan",
        "ok": True,
        "tool_result": {
            "ok": True,
            "stdout": "",
            "raw": {
                "findings": [
                    {
                        "template_id": "xss-simple",
                        "name": "Reflected XSS",
                        "severity": "medium",
                        "matched_at": "http://example.com/search",
                        "matcher_status": "true",
                        "classification": "xss",
                    }
                ]
            },
            "artifacts": [],
            "error": None,
        },
        "cost_usd": 0.0,
    }

    agent._after_observation(local_state, nuclei_obs)
    agent._after_observation(local_state, nuclei_obs)  # second call — same finding

    assert len(local_state["suspected"]) == 1


def test_nuclei_missing_template_id_or_endpoint_skipped():
    """Nuclei findings without template_id or endpoint are silently skipped."""
    agent = _make_agent()
    ctx = _make_ctx()
    local_state = agent.initialize_state(ctx)

    obs = {
        "action_type": "nuclei_scan",
        "ok": True,
        "tool_result": {
            "ok": True,
            "stdout": "",
            "raw": {
                "findings": [
                    {"template_id": "", "matched_at": "http://example.com/x", "severity": "low"},
                    {"template_id": "cve-1234", "matched_at": "", "severity": "low"},
                ]
            },
            "artifacts": [],
            "error": None,
        },
        "cost_usd": 0.0,
    }
    agent._after_observation(local_state, obs)
    assert len(local_state["suspected"]) == 0


# ---------------------------------------------------------------------------
# Test plan_next allowed_actions includes nuclei_scan when gate present
# ---------------------------------------------------------------------------

def test_plan_next_includes_nuclei_scan_when_tools_enabled():
    fake_gate = MagicMock()
    fake_gate.reachable = True
    agent = _make_agent(gate=fake_gate)
    ctx = _make_ctx()
    local_state = agent.initialize_state(ctx)

    captured = {}

    def fake_call_llm(ctx_, system_prompt, user_context):
        captured["user_context"] = user_context
        return {"action_type": "none", "done": True, "thought": "", "hypothesis": "", "params": {}}

    agent._call_llm = fake_call_llm
    agent.plan_next(ctx, local_state, [])

    assert "nuclei_scan" in captured["user_context"]["allowed_actions"]
    assert captured["user_context"]["tools_enabled"] is True


def test_plan_next_no_nuclei_scan_without_gate():
    agent = _make_agent(gate=None)
    ctx = _make_ctx()
    local_state = agent.initialize_state(ctx)

    captured = {}

    def fake_call_llm(ctx_, system_prompt, user_context):
        captured["user_context"] = user_context
        return {"action_type": "none", "done": True, "thought": "", "hypothesis": "", "params": {}}

    agent._call_llm = fake_call_llm
    agent.plan_next(ctx, local_state, [])

    assert "nuclei_scan" not in captured["user_context"]["allowed_actions"]
    assert captured["user_context"]["tools_enabled"] is False
