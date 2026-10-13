"""The only side effects the agent may propose, with typed arguments.

Proposing is all the agent can do. Every action here is queued for a human, and the
outbox executes an approved action at most once.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class IssueRefund(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: int = Field(gt=0)
    amount: float = Field(gt=0, le=10_000)
    reason: Literal["damaged", "late", "wrong_item", "changed_mind", "goodwill"]


class FlagSupplier(BaseModel):
    model_config = ConfigDict(extra="forbid")
    supplier_id: int = Field(gt=0)
    issue: str = Field(min_length=10, max_length=300)


class UpdateMetricDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metric: str = Field(pattern=r"^[a-z][a-z0-9_]{2,40}$")
    definition_sql: str = Field(min_length=10, max_length=1000)


@dataclass(frozen=True)
class ActionSpec:
    name: str
    model: type[BaseModel]
    risk: Literal["low", "medium", "high"]
    summary: str


CATALOG: dict[str, ActionSpec] = {
    spec.name: spec
    for spec in (
        ActionSpec("issue_refund", IssueRefund, "high", "Refund part or all of an order."),
        ActionSpec("flag_supplier", FlagSupplier, "medium", "Open a quality case on a supplier."),
        ActionSpec(
            "update_metric_definition",
            UpdateMetricDefinition,
            "high",
            "Change the SQL behind a published metric.",
        ),
    )
}


class InvalidAction(ValueError):
    """The proposal does not match the catalog."""


def validate_action(action_type: str, arguments: dict[str, Any]) -> tuple[ActionSpec, BaseModel]:
    spec = CATALOG.get(action_type)
    if spec is None:
        raise InvalidAction(
            f"unknown action_type {action_type!r}; allowed: {', '.join(sorted(CATALOG))}"
        )
    try:
        return spec, spec.model.model_validate(arguments)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'arguments'}: {e['msg']}" for e in exc.errors()
        )
        raise InvalidAction(f"invalid arguments for {action_type}: {problems}") from exc


def proposal_id(action_type: str, arguments: dict[str, Any]) -> str:
    """Deterministic, so the same request proposed twice is recognisably the same."""
    payload = json.dumps({"t": action_type, "a": arguments}, sort_keys=True, default=str)
    return "prop_" + hashlib.sha256(payload.encode()).hexdigest()[:16]
