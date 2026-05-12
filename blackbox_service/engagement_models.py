from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


EngagementStatus = Literal[
    "created",
    "running",
    "paused_for_approval",
    "completed",
    "failed",
    "budget_exhausted",
]


class BudgetState(BaseModel):
    limit_usd: float = 50.0
    spent_usd: float = 0.0
    warn_threshold: float = 0.80
    pause_threshold: float = 0.95

    @property
    def ratio(self) -> float:
        if self.limit_usd <= 0:
            return 1.0
        return self.spent_usd / self.limit_usd


class EngagementEvent(BaseModel):
    ts: datetime = Field(default_factory=utc_now)
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class FindingEvidence(BaseModel):
    kind: str
    detail: str
    artifact_path: str | None = None


class SuspectedFinding(BaseModel):
    finding_id: str
    vuln_type: str
    title: str
    endpoint: str
    method: str = "GET"
    severity: Literal["critical", "high", "medium", "low"] = "medium"
    confidence: int = 5
    evidence_snippet: str = ""
    source_agent: str = "access_test"


class ConfirmedFinding(BaseModel):
    finding_id: str
    vuln_type: str
    title: str
    endpoint: str
    method: str = "GET"
    severity: Literal["critical", "high", "medium", "low"] = "medium"
    confidence: int = 8
    impact: str = ""
    status: Literal["confirmed", "false_positive"] = "confirmed"
    evidence: list[FindingEvidence] = Field(default_factory=list)


class AgentState(BaseModel):
    name: str
    status: Literal["idle", "running", "completed", "failed", "paused"] = "idle"
    steps_completed: int = 0
    last_error: str | None = None


class AttackSurface(BaseModel):
    hosts: list[str] = Field(default_factory=list)
    endpoints: list[dict[str, Any]] = Field(default_factory=list)
    tech_stack: list[str] = Field(default_factory=list)


class AuthState(BaseModel):
    mode: Literal["none", "form", "oauth", "unknown"] = "none"
    status: Literal["not_attempted", "success", "failed"] = "not_attempted"
    notes: str = ""
    storage_state: dict[str, Any] = Field(default_factory=dict)


class ExecutiveReport(BaseModel):
    title: str
    generated_at: datetime = Field(default_factory=utc_now)
    target: str
    engagement_id: str
    summary: str
    findings_overview: dict[str, int]
    key_risks: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)


class EngagementRecord(BaseModel):
    engagement_id: str
    target_url: str
    status: EngagementStatus = "created"
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    approval_mode: Literal["optional", "mandatory", "none"] = "optional"
    approval_required: bool = False
    approval_granted: bool = False
    budget: BudgetState = Field(default_factory=BudgetState)
    run_id: str | None = None
    current_phase: Literal["init", "discovery", "access_test", "approval", "confirm_evidence", "report", "done"] = "init"
    agent_states: dict[str, AgentState] = Field(default_factory=dict)
    attack_surface: AttackSurface = Field(default_factory=AttackSurface)
    auth_state: AuthState = Field(default_factory=AuthState)
    suspected_findings: list[SuspectedFinding] = Field(default_factory=list)
    confirmed_findings: list[ConfirmedFinding] = Field(default_factory=list)
    events: list[EngagementEvent] = Field(default_factory=list)
    report: ExecutiveReport | None = None
    last_error: str | None = None


class CreateEngagementRequest(BaseModel):
    target_url: str
    budget_usd: float = 50.0
    approval_mode: Literal["optional", "mandatory", "none"] = "optional"


class StartEngagementRequest(BaseModel):
    max_steps_per_agent: int = 12
    step_delay_ms: int = 200


class ApprovalRequest(BaseModel):
    approved: bool
    note: str = ""
