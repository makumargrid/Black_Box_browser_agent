from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse

from blackbox_service.engagement_models import SuspectedFinding

from .base import AgentBase, AgentContext, AgentStep


_ID_RE = re.compile(r"(\d+)")


class AccessTestAgent(AgentBase):
    name = "access_test"

    def initialize_state(self, ctx: AgentContext) -> dict[str, object]:
        endpoints = list(ctx.state.get("discovery_endpoints", []))
        login_candidates = [e for e in endpoints if "login" in str(e.get("url", "")).lower()]
        api_candidates = [e for e in endpoints if "/api" in str(e.get("url", ""))]
        return {
            "login_candidates": login_candidates,
            "api_candidates": api_candidates,
            "probe_index": 0,
            "stage": "auth",
            "tier4_attempted": False,
            "suspected": [],
            "total_cost_usd": 0.0,
            "auth_status": "not_attempted",
        }

    _SYSTEM_PROMPT = """\
You are a blackbox security access-testing agent. Phase: AUTHENTICATION AND ACCESS CONTROL TESTING.

The Discovery phase mapped the attack surface — use the endpoints in your context.

Your goals (work through all of these):
1. TEST LOGIN: Find the login form. Try credentials: admin/admin, test/test, admin/password.
   Try SQL injection: username = ' OR 1=1-- with any password. Try: admin'--
2. TEST API WITHOUT AUTH: Probe /api/* endpoints with http_get. Look for 200 responses that expose data.
3. TEST ADMIN ROUTES: Try /admin, /management, /internal, /dashboard — check if accessible unauthenticated.
4. IDOR TESTING: When you see URLs with numeric IDs (e.g. /users/1), probe adjacent IDs (/users/2, /users/0).
5. For complex login flows you cannot handle with navigate/fill: use ai_navigate with a clear instruction.

Record every suspected vulnerability in your thought/hypothesis so the next phase can confirm it.

Available actions:
- http_get: {"url": "full_url"} — HTTP probe without browser
- navigate: {"url": "full_url"} — navigate browser to URL
- get_page_content: {} — read current page content/forms
- ai_navigate: {"instruction": "...", "target_url": "...", "max_steps": N} — AI browser agent for complex flows
- snapshot: {} — screenshot for evidence

Return ONLY valid JSON:
{"thought": "...", "hypothesis": "...", "action_type": "...", "params": {...}, "done": false}

Set done=true only when you have thoroughly tested auth and access control.\
"""

    def plan_next(self, ctx: AgentContext, local_state: dict[str, object], observations: list[dict[str, object]]) -> AgentStep:
        discovery_endpoints = local_state.get("api_candidates", []) + local_state.get("login_candidates", [])
        decision = self._call_llm(ctx, self._SYSTEM_PROMPT, {
            "target_url": ctx.target_url,
            "step": len(observations),
            "max_steps": ctx.max_steps,
            "discovery_endpoints": [str(e.get("url", "")) for e in discovery_endpoints][:20],
            "suspected_so_far": len(local_state.get("suspected", [])),
            "recent_observations": [
                {
                    "action_type": o.get("action_type"),
                    "ok": o.get("ok"),
                    "result_preview": str(o.get("result", ""))[:300],
                }
                for o in observations[-6:]
            ],
            "allowed_actions": ["http_get", "navigate", "get_page_content", "ai_navigate", "snapshot"],
        })
        return AgentStep(
            done=bool(decision.get("done", False)),
            goal=str(decision.get("thought", "")),
            action_type=str(decision.get("action_type", "none")),
            params=dict(decision.get("params") or {}),
            note=str(decision.get("hypothesis", "")),
        )

    def _after_observation(self, local_state: dict[str, object], obs: dict[str, object]) -> None:
        super()._after_observation(local_state, obs)

        if str(obs.get("action_type")) == "navigate":
            if bool(obs.get("ok")):
                local_state["auth_status"] = "success"
            else:
                local_state["auth_status"] = "failed"
        if str(obs.get("action_type")) == "ai_navigate":
            local_state["auth_status"] = "success" if bool(obs.get("ok")) else "failed"

        if str(obs.get("action_type")) != "http_get":
            return

        result = obs.get("result") or {}
        if not isinstance(result, dict):
            return

        status_code = int(result.get("status_code", 0))
        url = str(result.get("url", ""))
        body_preview = str(result.get("body_preview", ""))

        if "/admin" in url and status_code == 200:
            self._add_suspected(
                local_state,
                vuln_type="broken_access_control",
                title="Admin route reachable without strict controls",
                endpoint=url,
                severity="high",
                confidence=7,
                evidence_snippet=f"status={status_code}",
            )

        if "/api" in url and status_code == 200:
            self._add_suspected(
                local_state,
                vuln_type="missing_auth_api",
                title="API endpoint accessible",
                endpoint=url,
                severity="medium",
                confidence=5,
                evidence_snippet=f"status={status_code} body={body_preview[:120]}",
            )

        id_match = _ID_RE.search(url)
        if id_match and status_code == 200:
            value = id_match.group(1)
            next_id = str(int(value) + 1)
            alt_url = url.replace(value, next_id, 1)
            self._add_suspected(
                local_state,
                vuln_type="idor",
                title="Potential IDOR via numeric identifier",
                endpoint=alt_url,
                severity="high",
                confidence=6,
                evidence_snippet="numeric ID path detected with accessible record",
            )

    def _add_suspected(
        self,
        local_state: dict[str, object],
        vuln_type: str,
        title: str,
        endpoint: str,
        severity: str,
        confidence: int,
        evidence_snippet: str,
    ) -> None:
        key = f"{vuln_type}|{endpoint}".encode("utf-8")
        finding_id = f"sf-{hashlib.sha1(key).hexdigest()[:10]}"
        findings: list[SuspectedFinding] = local_state["suspected"]
        if any(x.finding_id == finding_id for x in findings):
            return
        findings.append(
            SuspectedFinding(
                finding_id=finding_id,
                vuln_type=vuln_type,
                title=title,
                endpoint=endpoint,
                method="GET",
                severity=severity,  # type: ignore[arg-type]
                confidence=confidence,
                evidence_snippet=evidence_snippet,
            )
        )

    def summarize(self, ctx: AgentContext, local_state: dict[str, object], observations: list[dict[str, object]]) -> dict[str, object]:
        suspected: list[SuspectedFinding] = local_state.get("suspected", [])  # type: ignore[assignment]
        return {
            "auth_status": str(local_state.get("auth_status", "not_attempted")),
            "suspected_findings": [x.model_dump(mode="json") for x in suspected],
            "observation_count": len(observations),
            "cost_usd": float(local_state.get("total_cost_usd", 0.0)),
            "observations": observations,
        }
