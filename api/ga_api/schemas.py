"""Request and response shapes of the HTTP API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from ga_core.models import Checks, Usage


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=1000)


class Decision(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class RunSummary(BaseModel):
    id: str
    question: str
    model: str
    status: str
    disposition: str | None
    finding_state: str | None
    pending_actions: int
    tool_calls: int
    created_at: datetime
    duration_ms: float | None


class StepOut(BaseModel):
    seq: int
    node: str
    started_ms: float
    duration_ms: float
    detail: dict[str, Any]


class ToolCallOut(BaseModel):
    call_id: str
    seq: int
    tool: str
    arguments: dict[str, Any]
    ok: bool
    result: dict[str, Any] | None
    error: str | None
    duration_ms: float


class FindingOut(BaseModel):
    status: str
    answer: str
    confidence: str
    numbers: list[dict[str, Any]]
    caveats: list[str]
    state: str
    decided_by: str | None
    decided_at: datetime | None
    decision_reason: str | None


class ActionOut(BaseModel):
    id: str
    run_id: str
    question: str
    proposal_id: str
    tool_call_id: str
    action_type: str
    arguments: dict[str, Any]
    reason: str
    risk: str
    state: str
    created_at: datetime
    decided_by: str | None
    decided_at: datetime | None
    decision_reason: str | None
    executed_at: datetime | None
    error: str | None
    outbox_state: str | None
    outbox_attempts: int | None
    outbox_error: str | None
    effect_id: int | None
    effect_applied_at: datetime | None


class AuditOut(BaseModel):
    id: int
    at: datetime
    actor: str
    event: str
    run_id: str | None
    subject: str | None
    detail: dict[str, Any]


class RunDetail(BaseModel):
    id: str
    question: str
    model: str
    status: str
    disposition: str | None
    stop_reason: str | None
    fallback: str | None
    error: str | None
    checks: Checks | None
    usage: Usage | None
    duration_ms: float | None
    created_at: datetime
    finished_at: datetime | None
    steps: list[StepOut]
    tool_calls: list[ToolCallOut]
    finding: FindingOut | None
    actions: list[ActionOut]
    audit: list[AuditOut]


class HeldFinding(FindingOut):
    run_id: str
    question: str
    checks: Checks | None
    created_at: datetime


class ReviewQueue(BaseModel):
    actions: list[ActionOut]
    findings: list[HeldFinding]


class DrainResult(BaseModel):
    executed: int
    duplicate: int
    retried: int
    failed: int
