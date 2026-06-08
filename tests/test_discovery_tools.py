from __future__ import annotations

from unittest.mock import MagicMock

from blackbox_service.agents_v2.discovery import DiscoveryAgent


def _make_agent(gate=None) -> DiscoveryAgent:
    fake_bie = MagicMock()
    return DiscoveryAgent(fake_bie, tool_gate=gate)


def _make_ctx():
    from blackbox_service.agents_v2.base import AgentContext
    return AgentContext(
        engagement_id="eng-x",
        run_id="run-x",
        target_url="http://example.com",
        max_steps=5,
    )


# ---------------------------------------------------------------------------
# Tests for _process_nmap
# ---------------------------------------------------------------------------

def test_nmap_scan_populates_hosts_and_tech_stack():
    agent = _make_agent()
    ctx = _make_ctx()
    local_state = agent.initialize_state(ctx)

    obs = {
        "action_type": "nmap_scan",
        "ok": True,
        "tool_result": {
            "ok": True,
            "stdout": "80/tcp open http Apache httpd 2.4.51\n443/tcp open ssl/http nginx",
            "raw": {
                "hosts": [
                    {
                        "address": "93.184.216.34",
                        "ports": [
                            {"port": 80, "service": "Apache/2.4.51"},
                            {"port": 443, "service": "nginx"},
                        ],
                    }
                ]
            },
            "artifacts": [],
            "error": None,
        },
        "cost_usd": 0.0,
    }
    agent._after_observation(local_state, obs)

    assert "93.184.216.34" in local_state["hosts"]
    tech_stack = local_state.get("tech_stack", set())
    # Should pick up services from both raw struct and stdout
    assert any("Apache" in s for s in tech_stack) or any("apache" in s.lower() for s in tech_stack)


def test_nmap_scan_stdout_service_extraction():
    agent = _make_agent()
    ctx = _make_ctx()
    local_state = agent.initialize_state(ctx)

    obs = {
        "action_type": "nmap_scan",
        "ok": True,
        "tool_result": {
            "ok": True,
            "stdout": "22/tcp open ssh OpenSSH 8.9\n3306/tcp open mysql MySQL 8.0",
            "raw": {},
            "artifacts": [],
            "error": None,
        },
        "cost_usd": 0.0,
    }
    agent._after_observation(local_state, obs)

    tech = local_state.get("tech_stack", set())
    assert any("ssh" in s.lower() or "OpenSSH" in s for s in tech)
    assert any("mysql" in s.lower() or "MySQL" in s for s in tech)


# ---------------------------------------------------------------------------
# Tests for _process_katana
# ---------------------------------------------------------------------------

def test_katana_crawl_populates_endpoints():
    agent = _make_agent()
    ctx = _make_ctx()
    local_state = agent.initialize_state(ctx)

    obs = {
        "action_type": "katana_crawl",
        "ok": True,
        "tool_result": {
            "ok": True,
            "stdout": "http://example.com/api/users\nhttp://example.com/login\nhttp://example.com/admin",
            "raw": {},
            "artifacts": [],
            "error": None,
        },
        "cost_usd": 0.0,
    }
    agent._after_observation(local_state, obs)

    urls = [e["url"] for e in local_state["endpoints"]]
    assert "http://example.com/api/users" in urls
    assert "http://example.com/login" in urls
    assert all(e["source"] == "katana" for e in local_state["endpoints"])


def test_katana_crawl_raw_urls():
    agent = _make_agent()
    ctx = _make_ctx()
    local_state = agent.initialize_state(ctx)

    obs = {
        "action_type": "katana_crawl",
        "ok": True,
        "tool_result": {
            "ok": True,
            "stdout": "",
            "raw": {"urls": ["http://example.com/a", "http://example.com/b"]},
            "artifacts": [],
            "error": None,
        },
        "cost_usd": 0.0,
    }
    agent._after_observation(local_state, obs)

    urls = [e["url"] for e in local_state["endpoints"]]
    assert "http://example.com/a" in urls
    assert "http://example.com/b" in urls


# ---------------------------------------------------------------------------
# Tests for _process_subfinder
# ---------------------------------------------------------------------------

def test_subfinder_populates_hosts():
    agent = _make_agent()
    ctx = _make_ctx()
    local_state = agent.initialize_state(ctx)

    obs = {
        "action_type": "subfinder_enum",
        "ok": True,
        "tool_result": {
            "ok": True,
            "stdout": "api.example.com\nmail.example.com\ndev.example.com",
            "raw": {},
            "artifacts": [],
            "error": None,
        },
        "cost_usd": 0.0,
    }
    agent._after_observation(local_state, obs)

    assert "api.example.com" in local_state["hosts"]
    assert "mail.example.com" in local_state["hosts"]


# ---------------------------------------------------------------------------
# Tests for _process_nuclei (stored separately, not in endpoints)
# ---------------------------------------------------------------------------

def test_nuclei_findings_stored_not_in_endpoints():
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
                        "name": "Log4Shell",
                        "severity": "critical",
                        "matched_at": "http://example.com/login",
                    }
                ]
            },
            "artifacts": [],
            "error": None,
        },
        "cost_usd": 0.0,
    }
    agent._after_observation(local_state, obs)

    # nuclei findings go to nuclei_findings, NOT endpoints
    assert len(local_state.get("nuclei_findings", [])) == 1
    assert local_state["nuclei_findings"][0]["template_id"] == "cve-2021-44228"
    assert len(local_state["endpoints"]) == 0  # not mixed into endpoints


# ---------------------------------------------------------------------------
# Test summarize includes nuclei_findings
# ---------------------------------------------------------------------------

def test_summarize_includes_nuclei_findings():
    agent = _make_agent()
    ctx = _make_ctx()
    local_state = agent.initialize_state(ctx)
    local_state["nuclei_findings"] = [{"template_id": "some-cve", "severity": "medium"}]
    local_state["endpoints"] = [
        {"url": "http://example.com/x", "method": "GET", "status_code": 200, "source": "katana", "auth_required": False}
    ]

    result = agent.summarize(ctx, local_state, [])
    assert len(result["nuclei_findings"]) == 1
    assert len(result["endpoints"]) == 1


# ---------------------------------------------------------------------------
# Test plan_next passes tools in allowed_actions when gate is present
# ---------------------------------------------------------------------------

def test_plan_next_includes_tool_actions_when_gate_present():
    """When tool_gate is set, plan_next includes tool actions in allowed_actions."""
    fake_gate = MagicMock()
    fake_gate.reachable = True
    agent = _make_agent(gate=fake_gate)
    ctx = _make_ctx()
    local_state = agent.initialize_state(ctx)

    # We'll spy on _call_llm
    captured = {}

    def fake_call_llm(ctx_, system_prompt, user_context):
        captured["user_context"] = user_context
        return {"action_type": "none", "done": True, "thought": "", "hypothesis": "", "params": {}}

    agent._call_llm = fake_call_llm
    agent.plan_next(ctx, local_state, [])

    allowed = captured["user_context"]["allowed_actions"]
    assert "nmap_scan" in allowed
    assert "katana_crawl" in allowed
    assert "subfinder_enum" in allowed
    assert "nuclei_scan" in allowed
    assert captured["user_context"]["tools_enabled"] is True


def test_plan_next_no_tool_actions_when_gate_absent():
    """Without tool_gate, plan_next does NOT include tool actions in allowed_actions."""
    agent = _make_agent(gate=None)
    ctx = _make_ctx()
    local_state = agent.initialize_state(ctx)

    captured = {}

    def fake_call_llm(ctx_, system_prompt, user_context):
        captured["user_context"] = user_context
        return {"action_type": "none", "done": True, "thought": "", "hypothesis": "", "params": {}}

    agent._call_llm = fake_call_llm
    agent.plan_next(ctx, local_state, [])

    allowed = captured["user_context"]["allowed_actions"]
    assert "nmap_scan" not in allowed
    assert "katana_crawl" not in allowed
    assert captured["user_context"]["tools_enabled"] is False
