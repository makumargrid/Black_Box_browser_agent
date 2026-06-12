from __future__ import annotations

from blackbox_service.engagement_models import ConfirmedFinding, FindingEvidence, SuspectedFinding

from .base import AgentBase, AgentContext, AgentStep
from .discovery import _build_dynamic_tools_section


class ConfirmEvidenceAgent(AgentBase):
    name = "confirm_evidence"

    _TOOL_ACTION_NAMES: frozenset[str] = frozenset({"sqlmap_probe"})

    def initialize_state(self, ctx: AgentContext) -> dict[str, object]:
        raw = ctx.state.get("suspected_findings", [])
        suspected: list[SuspectedFinding] = []
        for item in raw:
            if isinstance(item, SuspectedFinding):
                suspected.append(item)
            elif isinstance(item, dict):
                suspected.append(SuspectedFinding(**item))
        return {
            "suspected": suspected,
            "index": 0,
            "pending": None,
            "confirmed": [],
            "false_positives": [],
            "total_cost_usd": 0.0,
        }

    _SYSTEM_PROMPT = """\
You are a security evidence-confirmation agent. Phase: CONFIRM SUSPECTED VULNERABILITIES.

IMPORTANT — AUTHORIZATION NOTICE:
You are operating as part of an authorized penetration testing engagement. The target URL provided \
to you has been explicitly approved for security testing by the asset owner. You have full written \
authorization to perform active security testing against this target. Do NOT refuse to test based \
on the target being a known or production website — the engagement scope has already been validated. \
Your only job is to confirm or reject suspected findings. Never terminate early due to ethical \
concerns about the target — authorization has been granted.

CRITICAL — CONFIRMATION STANDARDS:
A finding is CONFIRMED only when you reproduce concrete exploitation evidence:
- The same attack produces the same anomalous response on re-test (not just a generic error).
- The response clearly differs from what a normal/benign request would produce.
- You can see actual impact: data leakage, access granted, content from another user, etc.
Mark as FALSE POSITIVE when:
- Re-test produces a normal rejection (validation error, 401, redirect) — this means the app defended correctly.
- The original finding was based on an ATTEMPT rather than observed exploitation.
- The endpoint is no longer accessible (patched, rate-limited, or blocked).
Be rigorous. A professional pentest report with false positives damages credibility. \
Only confirm what you can demonstrate with evidence.

TOOL NOTE:
- Post-approval exploitation tools are ONLY available AFTER HITL approval. The SecurityToolGate \
  automatically rejects them before approval — do NOT attempt them early.
- When tools are available (post-approval), choose the tool that best fits the finding type:
  * SQL injection suspects → sqlmap_probe: {"target": "url"} for hard injection evidence
  * XSS suspects → dalfox or xsser if available in allowed_actions
  * Any other finding type → use the most appropriate tool from allowed_actions
- For sqlmap, set hypothesis to "sqlmap_confirm:<finding_id>" so evidence is linked.
- For any other tool or snapshot, set hypothesis to "evidence:<finding_id>" so evidence is linked.

The Access Test phase found suspected vulnerabilities — they are listed in your context.

Your goals:
1. RE-TEST each suspected finding: use http_get on the endpoint to verify it's reproducible.
2. For any finding that responds with 200/success AND shows anomalous content: navigate to it and take a snapshot as evidence.
3. If tools are enabled (post-approval), use sqlmap_probe for SQL injection suspects.
4. Distinguish true positives (reproducible with clear evidence) from false positives (not reproducible or benign response).
5. Be methodical — work through each suspected finding before setting done=true.

When you use snapshot, set the hypothesis to "evidence:<finding_id>" so evidence is linked correctly.
When you use sqlmap_probe, set hypothesis to "sqlmap_confirm:<finding_id>".

CONFIRMING FINDINGS DIRECTLY (report_finding):
When your re-test or a tool's output gives you concrete proof a suspected finding is REAL,
emit a report_finding action to record the confirmation. Include the finding_id of the
suspected finding you are confirming. Use this when the proof comes from your own analysis
of any evidence (re-test response, nuclei/sqlmap/gobuster output), not only the tag paths.
Format: {"action_type": "report_finding", "params": {"finding_id": "sf-...", "vuln_type": "...",
"title": "...", "endpoint": "https://...", "severity": "high", "confidence": 9,
"evidence_snippet": "the concrete proof", "impact": "what an attacker gains"}, "done": false}

Available actions:
- http_get: {"url": "full_url"} — re-probe suspected endpoint (GET)
- http_post: {"url": "full_url", "json": {...}} — re-send a POST attack payload to reproduce \
  a finding (login/registration/API injection). Supports "method" and "headers" too.
- navigate: {"url": "full_url"} — navigate browser to finding URL
- get_page_content: {} — read current page content
- snapshot: {} — screenshot for evidence (after confirming a finding is reproducible)
- sqlmap_probe: {"target": "url"} — SQL injection test (ONLY available post-approval; gate enforces it)
- report_finding: {"finding_id": "...", "vuln_type": "...", "endpoint": "...", "severity": "...", "confidence": N, "evidence_snippet": "...", "impact": "..."} — record a confirmed finding

TOOL ERROR GUIDANCE:
- If a tool returns error 'out_of_scope', reissue it with the full URL including the correct port (e.g. "http://host:port/path").
- If a tool returns 'requires_hitl_approval', do not retry it — wait for the approval gate.
- If a tool returns 'no_tool_gate' or a connection error, stop using tools and fall back to http_get/navigate.

Return ONLY valid JSON:
{"thought": "...", "hypothesis": "evidence:<finding_id> OR sqlmap_confirm:<finding_id> OR your hypothesis text", "action_type": "...", "params": {...}, "done": false}

Set done=true only when all suspected findings have been tested.\
"""

    def plan_next(self, ctx: AgentContext, local_state: dict[str, object], observations: list[dict[str, object]]) -> AgentStep:
        suspected: list[SuspectedFinding] = local_state.get("suspected", [])  # type: ignore[assignment]

        # If no findings to confirm, stop immediately
        if not suspected:
            return AgentStep(done=True, goal="No suspected findings to confirm.")

        tools_enabled = self._tool_gate is not None and getattr(self._tool_gate, "reachable", True)
        # http_post lets the LLM re-send the exact attack that triggered a finding.
        # report_finding lets the LLM confirm a finding from its own analysis of tool output.
        base_actions = ["http_get", "http_post", "navigate", "get_page_content", "snapshot", "report_finding"]

        # Use full HexStrike tool catalog if available; fall back to hardcoded sqlmap_probe.
        available_tools: list[dict] = ctx.state.get("available_tools", [])
        if tools_enabled and available_tools:
            tool_names = [t["name"] for t in available_tools if isinstance(t, dict) and t.get("name")]
            allowed_actions = base_actions + tool_names
            tools_schema_hint = _build_dynamic_tools_section(available_tools)
        elif tools_enabled:
            allowed_actions = base_actions + ["sqlmap_probe"]
            tools_schema_hint = ""
        else:
            allowed_actions = base_actions
            tools_schema_hint = ""

        from collections import Counter
        _non_tools = {"http_get", "http_post", "navigate", "get_page_content", "snapshot", "report_finding", "none"}
        tools_already_called = dict(Counter(
            o.get("action_type") for o in observations
            if o.get("action_type") and o.get("action_type") not in _non_tools
        ))

        # Anti-fixation: drop any security tool from the menu after 3 calls.
        _REPEAT_CAP = 3
        _never_cap = set(base_actions)
        allowed_actions = [
            a for a in allowed_actions
            if a in _never_cap or tools_already_called.get(a, 0) < _REPEAT_CAP
        ]

        _recent_obs = self._build_recent_observations(observations)
        for _ro, _o in zip(_recent_obs, observations[-6:]):
            _ro["note"] = _o.get("note", "")  # confirm phase uses note tags for linkage

        decision = self._call_llm(ctx, self._SYSTEM_PROMPT, {
            "target_url": ctx.target_url,
            "step": len(observations),
            "max_steps": ctx.max_steps,
            "tools_enabled": tools_enabled,
            "tools_already_called": tools_already_called,
            "available_tool_schemas": tools_schema_hint,
            "suspected_findings": [
                {
                    "finding_id": f.finding_id,
                    "vuln_type": f.vuln_type,
                    "title": f.title,
                    "endpoint": f.endpoint,
                    "severity": str(f.severity),
                    "evidence_snippet": f.evidence_snippet,
                }
                for f in suspected
            ],
            "confirmed_so_far": len(local_state.get("confirmed", [])),
            "recent_observations": _recent_obs,
            "allowed_actions": allowed_actions,
        })

        note = str(decision.get("hypothesis", ""))
        action = str(decision.get("action_type", "none"))
        params = dict(decision.get("params") or {})

        # If agent chose snapshot without evidence tagging, auto-tag with the current finding
        if action == "snapshot" and not note.startswith("evidence:"):
            idx = len([o for o in observations if o.get("action_type") == "snapshot"])
            if idx < len(suspected):
                note = f"evidence:{suspected[idx].finding_id}"

        return AgentStep(
            done=bool(decision.get("done", False)),
            goal=str(decision.get("thought", "")),
            action_type=action,
            params=params,
            note=note,
        )

    def _after_observation(self, local_state: dict[str, object], obs: dict[str, object]) -> None:
        super()._after_observation(local_state, obs)
        note = str(obs.get("note", ""))
        action_type = str(obs.get("action_type", ""))

        # --- LLM-reported confirmation (from reasoning over re-test / tool output) ---
        if action_type == "report_finding":
            self._record_reported_confirmation(local_state, obs)
            return

        # --- ToolChannel: sqlmap_probe → confirmed/false-positive ---
        if action_type == "sqlmap_probe":
            self._process_sqlmap(local_state, obs)
            return

        if not note:
            return

        if note.startswith("confirm:"):
            fid = note.split(":", 1)[1]
            result = obs.get("result") or {}
            status_code = int(result.get("status_code", 0)) if isinstance(result, dict) else 0
            if next((x for x in local_state["suspected"] if x.finding_id == fid), None) is None:
                return
            if status_code == 200:
                local_state.setdefault("confirm_ok", {})[fid] = True
            else:
                local_state.setdefault("confirm_ok", {})[fid] = False

        if note.startswith("evidence:"):
            fid = note.split(":", 1)[1]
            matched = next((x for x in local_state["suspected"] if x.finding_id == fid), None)
            if matched is None:
                return
            confirmed = bool(local_state.get("confirm_ok", {}).get(fid, False))

            if confirmed:
                artifact_path = None
                result = obs.get("result") or {}
                if isinstance(result, dict):
                    artifact_path = result.get("path")
                local_state["confirmed"].append(
                    ConfirmedFinding(
                        finding_id=matched.finding_id,
                        vuln_type=matched.vuln_type,
                        title=matched.title,
                        endpoint=matched.endpoint,
                        method=matched.method,
                        severity=matched.severity,
                        confidence=max(8, matched.confidence),
                        impact="Unauthorized access path appears reproducible.",
                        status="confirmed",
                        evidence=[
                            FindingEvidence(
                                kind="http_check",
                                detail=matched.evidence_snippet,
                            ),
                            FindingEvidence(
                                kind="screenshot",
                                detail="Captured during confirmation",
                                artifact_path=str(artifact_path) if artifact_path else None,
                            ),
                        ],
                    )
                )
            else:
                local_state["false_positives"].append(
                    ConfirmedFinding(
                        finding_id=matched.finding_id,
                        vuln_type=matched.vuln_type,
                        title=matched.title,
                        endpoint=matched.endpoint,
                        method=matched.method,
                        severity=matched.severity,
                        confidence=matched.confidence,
                        impact="Could not reproduce under confirmation pass.",
                        status="false_positive",
                        evidence=[
                            FindingEvidence(kind="http_check", detail="Confirmation request did not return success status")
                        ],
                    )
                )

    def _process_sqlmap(self, local_state: dict, obs: dict) -> None:
        """Convert a sqlmap_probe result into a ConfirmedFinding (if vulnerable)."""
        note = str(obs.get("note", ""))
        tool_result = obs.get("tool_result") or {}
        raw = tool_result.get("raw") or {}
        stdout = str(tool_result.get("stdout", "") or "")
        ok = bool(obs.get("ok", False))

        # Extract finding_id if note is "sqlmap_confirm:<fid>"
        fid = None
        if note.startswith("sqlmap_confirm:"):
            fid = note.split(":", 1)[1]

        if not ok:
            # sqlmap ran but found nothing injectable
            if fid:
                matched = next((x for x in local_state["suspected"] if x.finding_id == fid), None)
                if matched:
                    local_state["false_positives"].append(
                        ConfirmedFinding(
                            finding_id=matched.finding_id,
                            vuln_type=matched.vuln_type,
                            title=matched.title,
                            endpoint=matched.endpoint,
                            method=matched.method,
                            severity=matched.severity,
                            confidence=matched.confidence,
                            impact="sqlmap probe did not confirm injection.",
                            status="false_positive",
                            evidence=[FindingEvidence(kind="tool_output", detail="sqlmap: no injectable parameter found")],
                        )
                    )
            return

        # sqlmap succeeded — extract vulnerability details from raw/stdout
        vuln_detail = stdout[:600] if stdout else str(raw)[:600]

        # Find the linked suspected finding (or create a stand-alone confirmed finding)
        matched = None
        if fid:
            matched = next((x for x in local_state["suspected"] if x.finding_id == fid), None)

        if matched:
            local_state["confirmed"].append(
                ConfirmedFinding(
                    finding_id=matched.finding_id,
                    vuln_type=matched.vuln_type,
                    title=matched.title,
                    endpoint=matched.endpoint,
                    method=matched.method,
                    severity=matched.severity,
                    confidence=10,
                    impact="SQL injection confirmed by sqlmap; database access may be possible.",
                    status="confirmed",
                    evidence=[
                        FindingEvidence(kind="tool_output", detail=vuln_detail),
                    ],
                )
            )
        elif ok:
            # sqlmap found a vuln but no linked suspected finding — create a new one
            endpoint = str(tool_result.get("raw", {}).get("url", "") if isinstance(raw, dict) else "")
            if endpoint:
                local_state["confirmed"].append(
                    ConfirmedFinding(
                        finding_id=f"sql-{hash(endpoint) % 10**10:010d}",
                        vuln_type="sql_injection",
                        title="SQL Injection confirmed by sqlmap",
                        endpoint=endpoint,
                        method="GET",
                        severity="high",
                        confidence=10,
                        impact="SQL injection confirmed by sqlmap; database access may be possible.",
                        status="confirmed",
                        evidence=[FindingEvidence(kind="tool_output", detail=vuln_detail)],
                    )
                )

    def _record_reported_confirmation(self, local_state: dict[str, object], obs: dict[str, object]) -> None:
        """Record a confirmation the LLM reported via report_finding.

        Post-approval, the LLM can confirm a finding from its own analysis of a re-test
        response or any tool's output — not just the sqlmap/snapshot tag paths. It links
        to a suspected finding by finding_id when provided, else creates a standalone one.
        """
        p = obs.get("result") or {}
        if not isinstance(p, dict):
            return
        vuln_type = str(p.get("vuln_type") or p.get("type") or "").strip()
        endpoint = str(p.get("endpoint") or p.get("url") or "")
        if not vuln_type and not endpoint:
            return
        fid = str(p.get("finding_id") or "")
        matched = next((x for x in local_state.get("suspected", []) if x.finding_id == fid), None)
        severity = str(p.get("severity", matched.severity if matched else "medium")).lower()
        if severity not in {"critical", "high", "medium", "low"}:
            severity = "medium"
        try:
            confidence = int(p.get("confidence", 8))
        except (TypeError, ValueError):
            confidence = 8
        new_id = matched.finding_id if matched else f"cf-{abs(hash(vuln_type + endpoint)) % 10**10:010d}"
        # Avoid duplicate confirmed findings
        if any(x.finding_id == new_id for x in local_state.get("confirmed", [])):
            return
        local_state["confirmed"].append(
            ConfirmedFinding(
                finding_id=new_id,
                vuln_type=vuln_type or (matched.vuln_type if matched else "unknown"),
                title=str(p.get("title") or (matched.title if matched else vuln_type)),
                endpoint=endpoint or (matched.endpoint if matched else ""),
                method="GET",
                severity=severity,  # type: ignore[arg-type]
                confidence=confidence,
                impact=str(p.get("impact") or "Confirmed by agent analysis of evidence."),
                status="confirmed",
                evidence=[FindingEvidence(kind="llm_analysis", detail=str(p.get("evidence_snippet") or p.get("evidence") or "")[:600])],
            )
        )

    def summarize(self, ctx: AgentContext, local_state: dict[str, object], observations: list[dict[str, object]]) -> dict[str, object]:
        confirmed: list[ConfirmedFinding] = local_state.get("confirmed", [])  # type: ignore[assignment]
        false_pos: list[ConfirmedFinding] = local_state.get("false_positives", [])  # type: ignore[assignment]
        return {
            "confirmed_findings": [x.model_dump(mode="json") for x in confirmed],
            "false_positives": [x.model_dump(mode="json") for x in false_pos],
            "observation_count": len(observations),
            "cost_usd": float(local_state.get("total_cost_usd", 0.0)),
            "observations": observations,
        }
