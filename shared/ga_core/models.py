"""The records that cross a boundary: what the model may claim, and what a run produced.

A model's reply is never state. It becomes a `Finding` only if it parses against these
types, and a `Finding` is published only after the policy and grounding checks agree.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class NumberClaim(BaseModel):
    """One number the answer relies on, with where it came from."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=120, description="What the number measures.")
    value: float = Field(description="The number exactly as stated in the answer.")
    unit: str | None = Field(default=None, max_length=24, description="e.g. USD, orders, %.")
    source_query: str | None = Field(
        default=None,
        description="Id of the query_warehouse call whose result contains this number.",
    )
    calculation: str | None = Field(
        default=None,
        max_length=200,
        description="Arithmetic over numbers taken from query results, e.g. '1520.5 / 38'.",
    )


class Finding(BaseModel):
    """What the agent is allowed to hand back. Anything else is rejected."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["answered", "unanswerable"]
    answer: str = Field(min_length=1, max_length=2000)
    numbers: list[NumberClaim] = Field(default_factory=list, max_length=20)
    confidence: Literal["high", "medium", "low"]
    caveats: list[str] = Field(default_factory=list, max_length=10)


class Disposition(StrEnum):
    """Where a run's finding ended up."""

    PUBLISHED = "published"
    HELD_FOR_REVIEW = "held_for_review"
    UNANSWERABLE = "unanswerable"
    REJECTED = "rejected"


class ToolCallRecord(BaseModel):
    """One tool call, exactly as made and as answered."""

    id: str
    tool: str
    arguments: dict[str, Any]
    ok: bool
    result: dict[str, Any] | None = None
    error: str | None = None
    duration_ms: float = 0.0


class ProposedActionSpec(BaseModel):
    """A side effect the agent asked for. Recorded by the tool; executed only after review."""

    proposal_id: str
    action_type: str
    arguments: dict[str, Any]
    reason: str
    risk: Literal["low", "medium", "high"]
    tool_call_id: str


class NumberCheck(BaseModel):
    """The grounding verdict for one number."""

    text: str
    value: float
    grounded: bool
    how: str | None = None
    tool_call_id: str | None = None
    column: str | None = None
    row: int | None = None


class Checks(BaseModel):
    schema_errors: list[str] = Field(default_factory=list)
    policy_violations: list[str] = Field(default_factory=list)
    numbers: list[NumberCheck] = Field(default_factory=list)

    @property
    def ungrounded(self) -> list[NumberCheck]:
        return [n for n in self.numbers if not n.grounded]


class Step(BaseModel):
    """One node in the run timeline."""

    node: str
    started_ms: float
    duration_ms: float
    detail: dict[str, Any] = Field(default_factory=dict)


class Usage(BaseModel):
    model_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0


class AgentResult(BaseModel):
    """Everything a run produced. Persisted by the API; rendered by the review UI."""

    run_id: str
    question: str
    model: str
    disposition: Disposition
    finding: Finding | None = None
    proposals: list[ProposedActionSpec] = Field(default_factory=list)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    steps: list[Step] = Field(default_factory=list)
    checks: Checks = Field(default_factory=Checks)
    fallback: str | None = None
    stop_reason: str | None = None
    usage: Usage = Field(default_factory=Usage)
    duration_ms: float = 0.0
