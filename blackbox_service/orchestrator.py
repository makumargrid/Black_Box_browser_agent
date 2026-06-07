from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from blackbox_service.agents_v2 import AccessTestAgent, ConfirmEvidenceAgent, DiscoveryAgent
from blackbox_service.agents_v2.base import AgentContext
from blackbox_service.bie import BrowserInteractionEngine
from blackbox_service.engagement_bus import EngagementEventBus
from blackbox_service.engagement_models import (
    AgentState,
    ApprovalRequest,
    BudgetState,
    ConfirmedFinding,
    CreateEngagementRequest,
    EngagementEvent,
    EngagementRecord,
    ExecutiveReport,
    SuspectedFinding,
)

logger = logging.getLogger(__name__)


class EngagementNotFoundError(KeyError):
    pass


class EngagementOrchestrator:
    def __init__(
        self,
        service: Any,
        fail_fast_llm: bool = True,
        anthropic_api_key: str = "",
        anthropic_model: str = "claude-sonnet-4-6",
        tier4_headless: bool = True,
        hexstrike_enabled: bool = False,
        hexstrike_url: str = "http://localhost:8888",
        hexstrike_timeout_s: float = 300.0,
        tool_budget_hard_cap_usd: float = 5.0,
        artifacts_dir: str | Path = "artifacts",
    ) -> None:
        self._service = service
        self._anthropic_api_key = anthropic_api_key
        self._anthropic_model = anthropic_model
        self._artifacts_dir = Path(artifacts_dir)
        self._tool_budget_hard_cap_usd = tool_budget_hard_cap_usd
        self._bie = BrowserInteractionEngine(
            action_executor=service.execute_action,
            fail_fast_llm=fail_fast_llm,
            anthropic_api_key=anthropic_api_key,
            anthropic_model=anthropic_model,
            tier4_headless=tier4_headless,
        )
        self._engagements: dict[str, EngagementRecord] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()
        self._bus = EngagementEventBus()

        # Build a single shared HexStrikeClient if the ToolChannel is enabled.
        # HexStrike is intentionally optional and off by default.
        self._hexstrike_client = None
        if hexstrike_enabled:
            from blackbox_service.toolchannel.hexstrike_client import HexStrikeClient
            self._hexstrike_client = HexStrikeClient(
                base_url=hexstrike_url,
                timeout_s=hexstrike_timeout_s,
            )
            if self._hexstrike_client.health():
                logger.info("ToolChannel enabled — HexStrike reachable at %s", hexstrike_url)
            else:
                logger.warning("ToolChannel enabled but HexStrike unreachable at %s", hexstrike_url)

    def create_engagement(self, body: CreateEngagementRequest) -> EngagementRecord:
        eid = f"eng-{uuid.uuid4().hex[:12]}"
        rec = EngagementRecord(
            engagement_id=eid,
            target_url=body.target_url,
            approval_mode=body.approval_mode,
            budget=BudgetState(limit_usd=max(1.0, float(body.budget_usd))),
            agent_states={
                "discovery": AgentState(name="discovery"),
                "access_test": AgentState(name="access_test"),
                "confirm_evidence": AgentState(name="confirm_evidence"),
            },
        )
        rec.events.append(EngagementEvent(type="engagement.created", payload={"target_url": body.target_url}))
        with self._lock:
            self._engagements[eid] = rec
        return rec

    def get_engagement(self, engagement_id: str) -> EngagementRecord:
        with self._lock:
            rec = self._engagements.get(engagement_id)
            if rec is None:
                raise EngagementNotFoundError(engagement_id)
            return rec

    def list_events(self, engagement_id: str) -> list[dict[str, Any]]:
        rec = self.get_engagement(engagement_id)
        return [e.model_dump(mode="json") for e in rec.events]

    def runtime_capabilities(self) -> dict[str, Any]:
        return {
            "tier4_enabled": bool(getattr(self._bie, "_anthropic_api_key", "")),
            "tier4_fail_fast": bool(getattr(self._bie, "_fail_fast_llm", True)),
            "toolchannel_enabled": self._hexstrike_client is not None,
        }

    def start_engagement(self, engagement_id: str, max_steps_per_agent: int, step_delay_ms: int) -> EngagementRecord:
        rec = self.get_engagement(engagement_id)
        with self._lock:
            current = self._threads.get(engagement_id)
            if current is not None and current.is_alive():
                return rec
            thread = threading.Thread(
                target=self._run_flow,
                args=(engagement_id, max_steps_per_agent, step_delay_ms),
                daemon=True,
                name=f"engagement-{engagement_id}",
            )
            self._threads[engagement_id] = thread
            thread.start()
        return rec

    def approve(self, engagement_id: str, body: ApprovalRequest) -> EngagementRecord:
        rec = self.get_engagement(engagement_id)
        rec.approval_granted = bool(body.approved)
        rec.approval_required = False
        rec.current_phase = "confirm_evidence" if body.approved else "done"
        rec.status = "running" if body.approved else "completed"
        rec.updated_at = datetime.now(timezone.utc)
        rec.events.append(
            EngagementEvent(
                type="engagement.approval.updated",
                payload={"approved": body.approved, "note": body.note},
            )
        )
        if body.approved:
            # Continue asynchronously from confirmation stage.
            self.start_engagement(engagement_id, max_steps_per_agent=8, step_delay_ms=100)
        else:
            rec.report = self._build_report(rec)
        return rec

    def _run_flow(self, engagement_id: str, max_steps_per_agent: int, step_delay_ms: int) -> None:
        rec = self.get_engagement(engagement_id)
        gate = None
        try:
            rec.status = "running"
            rec.updated_at = datetime.now(timezone.utc)

            # Build a SecurityToolGate bound to this engagement's live record.
            if self._hexstrike_client is not None:
                from blackbox_service.toolchannel.security_gate import SecurityToolGate
                gate = SecurityToolGate(
                    client=self._hexstrike_client,
                    engagement=rec,
                    artifacts_dir=self._artifacts_dir,
                    logger=logger,
                    budget_hard_cap_usd=self._tool_budget_hard_cap_usd,
                )

            if rec.run_id is None:
                run = self._service.start_run(targets=[rec.target_url], options={"mode": "engagement"})
                rec.run_id = run.run_id
                self._event(rec, "engagement.run.created", {"run_id": rec.run_id})

            if rec.current_phase in {"init", "discovery"}:
                rec.current_phase = "discovery"
                self._event(rec, "phase.start", {"phase": "discovery"})
                disc_out = self._run_discovery(rec, max_steps_per_agent, step_delay_ms, gate)
                rec.attack_surface.hosts = list(disc_out.get("hosts", []))
                rec.attack_surface.endpoints = list(disc_out.get("endpoints", []))
                rec.attack_surface.tech_stack = list(disc_out.get("tech_stack", []))
                self._spend(rec, float(disc_out.get("cost_usd", 0.0)))
                self._event(rec, "phase.end", {"phase": "discovery", "endpoints": len(rec.attack_surface.endpoints)})

            if rec.current_phase in {"discovery", "access_test"}:
                rec.current_phase = "access_test"
                self._event(rec, "phase.start", {"phase": "access_test"})
                access_out = self._run_access_test(rec, max_steps_per_agent, step_delay_ms, gate)
                rec.auth_state.status = "success" if access_out.get("auth_status") == "success" else "failed"
                rec.suspected_findings = []
                for item in access_out.get("suspected_findings", []):
                    rec.suspected_findings.append(SuspectedFinding(**item))
                self._spend(rec, float(access_out.get("cost_usd", 0.0)))
                self._event(rec, "phase.end", {"phase": "access_test", "suspected": len(rec.suspected_findings)})

            needs_approval = rec.approval_mode == "mandatory" or (
                rec.approval_mode == "optional" and len(rec.suspected_findings) > 0 and not rec.approval_granted
            )
            if needs_approval:
                rec.current_phase = "approval"
                rec.status = "paused_for_approval"
                rec.approval_required = True
                self._event(
                    rec,
                    "engagement.paused_for_approval",
                    {
                        "suspected_findings": len(rec.suspected_findings),
                        "approval_mode": rec.approval_mode,
                    },
                )
                return

            if rec.current_phase in {"access_test", "approval", "confirm_evidence"}:
                rec.current_phase = "confirm_evidence"
                self._event(rec, "phase.start", {"phase": "confirm_evidence"})
                confirm_out = self._run_confirm_evidence(rec, max_steps_per_agent, step_delay_ms, gate)
                rec.confirmed_findings = [ConfirmedFinding(**x) for x in confirm_out.get("confirmed_findings", [])]
                self._spend(rec, float(confirm_out.get("cost_usd", 0.0)))
                self._event(rec, "phase.end", {"phase": "confirm_evidence", "confirmed": len(rec.confirmed_findings)})

            rec.current_phase = "report"
            rec.report = self._build_report(rec)
            rec.current_phase = "done"
            rec.status = "completed"
            self._event(rec, "engagement.completed", {"confirmed": len(rec.confirmed_findings)})

        except Exception as exc:
            rec.status = "failed"
            rec.last_error = str(exc)
            self._event(rec, "engagement.failed", {"error": str(exc)})
        finally:
            gate = None  # release the gate reference for this run
            rec.updated_at = datetime.now(timezone.utc)

    def _run_discovery(self, rec: EngagementRecord, max_steps: int, step_delay_ms: int, gate: Any = None) -> dict[str, Any]:
        rec.agent_states["discovery"].status = "running"
        ctx = AgentContext(
            engagement_id=rec.engagement_id,
            run_id=str(rec.run_id),
            target_url=rec.target_url,
            max_steps=max_steps,
            step_delay_ms=step_delay_ms,
            anthropic_api_key=self._anthropic_api_key,
            anthropic_model=self._anthropic_model,
        )
        out = DiscoveryAgent(self._bie, tool_gate=gate).run(ctx)
        rec.agent_states["discovery"].status = "completed"
        rec.agent_states["discovery"].steps_completed = int(out.get("observation_count", 0))
        return out

    def _run_access_test(self, rec: EngagementRecord, max_steps: int, step_delay_ms: int, gate: Any = None) -> dict[str, Any]:
        rec.agent_states["access_test"].status = "running"
        ctx = AgentContext(
            engagement_id=rec.engagement_id,
            run_id=str(rec.run_id),
            target_url=rec.target_url,
            max_steps=max_steps,
            step_delay_ms=step_delay_ms,
            state={"discovery_endpoints": rec.attack_surface.endpoints},
            anthropic_api_key=self._anthropic_api_key,
            anthropic_model=self._anthropic_model,
        )
        out = AccessTestAgent(self._bie, tool_gate=gate).run(ctx)
        for obs in out.get("observations", []):
            if not isinstance(obs, dict):
                continue
            if obs.get("action_type") != "ai_navigate":
                continue
            self._event(
                rec,
                "tier4.navigation.result",
                {
                    "ok": bool(obs.get("ok")),
                    "tier": obs.get("tier"),
                    "error": obs.get("error"),
                    "result_preview": str(obs.get("result", ""))[:400],
                },
            )
        rec.agent_states["access_test"].status = "completed"
        rec.agent_states["access_test"].steps_completed = int(out.get("observation_count", 0))
        return out

    def _run_confirm_evidence(self, rec: EngagementRecord, max_steps: int, step_delay_ms: int, gate: Any = None) -> dict[str, Any]:
        rec.agent_states["confirm_evidence"].status = "running"
        ctx = AgentContext(
            engagement_id=rec.engagement_id,
            run_id=str(rec.run_id),
            target_url=rec.target_url,
            max_steps=max_steps,
            step_delay_ms=step_delay_ms,
            state={"suspected_findings": [x.model_dump(mode="json") for x in rec.suspected_findings]},
            anthropic_api_key=self._anthropic_api_key,
            anthropic_model=self._anthropic_model,
        )
        out = ConfirmEvidenceAgent(self._bie, tool_gate=gate).run(ctx)
        rec.agent_states["confirm_evidence"].status = "completed"
        rec.agent_states["confirm_evidence"].steps_completed = int(out.get("observation_count", 0))
        return out

    def _spend(self, rec: EngagementRecord, amount: float) -> None:
        rec.budget.spent_usd += max(0.0, amount)
        ratio = rec.budget.ratio
        if ratio >= 1.0:
            rec.status = "budget_exhausted"
            self._event(rec, "budget.exhausted", {"spent": rec.budget.spent_usd, "limit": rec.budget.limit_usd})
        elif ratio >= rec.budget.pause_threshold:
            self._event(rec, "budget.pause_threshold", {"ratio": ratio})
        elif ratio >= rec.budget.warn_threshold:
            self._event(rec, "budget.warn_threshold", {"ratio": ratio})

    def _build_report(self, rec: EngagementRecord) -> ExecutiveReport:
        by_severity = {"critical": 0, "high": 0, "medium": 0, "low": 0}
        key_risks: list[str] = []
        for finding in rec.confirmed_findings:
            sev = str(finding.severity)
            if sev in by_severity:
                by_severity[sev] += 1
            key_risks.append(f"{finding.title} ({sev})")

        if not key_risks:
            key_risks.append("No confirmed exploitable findings in this run.")

        summary = (
            f"Engagement scanned {len(rec.attack_surface.endpoints)} endpoints across "
            f"{len(rec.attack_surface.hosts)} host(s). "
            f"{len(rec.suspected_findings)} findings were suspected and "
            f"{len(rec.confirmed_findings)} were confirmed."
        )

        recommendations = [
            "Enforce authentication and authorization checks on every API endpoint.",
            "Add server-side validation for identifier-based resource access.",
            "Enable centralized security logging for abnormal route access patterns.",
            "Run recurring blackbox assessments on each release candidate.",
        ]

        return ExecutiveReport(
            title="Automated Security Engagement Report",
            target=rec.target_url,
            engagement_id=rec.engagement_id,
            summary=summary,
            findings_overview=by_severity,
            key_risks=key_risks[:8],
            recommendations=recommendations,
        )

    def _event(self, rec: EngagementRecord, event_type: str, payload: dict[str, Any]) -> None:
        event = EngagementEvent(type=event_type, payload=payload)
        rec.events.append(event)
        rec.updated_at = datetime.now(timezone.utc)
        self._bus.publish(
            rec.engagement_id,
            {
                "type": event_type,
                "ts": event.ts.isoformat(),
                "payload": payload,
                "phase": rec.current_phase,
                "status": rec.status,
                "budget": {
                    "spent": rec.budget.spent_usd,
                    "limit": rec.budget.limit_usd,
                },
            },
        )
