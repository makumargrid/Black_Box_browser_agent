from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class EventEnvelope(BaseModel):
    event_id: str
    run_id: str
    ts: datetime = Field(default_factory=utc_now)
    type: str
    tab_id: str | None = None
    step_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    token_cost: float | None = None


class TabState(BaseModel):
    run_id: str
    tab_id: str
    url: str
    title: str = ""
    parent_tab_id: str | None = None
    correlation_id: str | None = None
    is_active: bool = False
    opened_at: datetime = Field(default_factory=utc_now)


class RunRecord(BaseModel):
    run_id: str
    status: str
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    targets: list[str] = Field(default_factory=list)
    options: dict[str, Any] = Field(default_factory=dict)
    active_tab_id: str | None = None
    error: str | None = None


class StartRunRequest(BaseModel):
    targets: list[str] = Field(default_factory=list)
    options: dict[str, Any] = Field(default_factory=dict)


class StartRunResponse(BaseModel):
    run_id: str
    status: str
    targets: list[str] = Field(default_factory=list)
    active_tab_id: str | None = None


class ActionRequest(BaseModel):
    action_type: str
    params: dict[str, Any] = Field(default_factory=dict)


class ActionResponse(BaseModel):
    ok: bool
    action_type: str
    result: Any = None


class AgentStartRequest(BaseModel):
    max_steps: int = 8
    step_delay_ms: int = 400
    model: str = ""
