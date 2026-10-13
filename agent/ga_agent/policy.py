"""Policy checks on a finding, and the routing decision that follows from all checks.

Policy is deterministic and small on purpose. Each rule either blocks the finding outright
or sends it to a person; none of them rewrites what the model said.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from ga_core.models import Checks, Disposition, Finding, ProposedActionSpec, ToolCallRecord

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE_RE = re.compile(r"\+\d{1,3}[\s-]?\d{3}[\s-]?\d{4,}")
_CLAIMED_EFFECT_RE = re.compile(
    r"\b(?:I(?:'ve| have)?|we(?:'ve| have)?|has been|have been|was|were)\s+"
    r"(?:already\s+)?(?:issued|refunded|processed|approved|executed|applied|flagged|updated|changed)\b",
    re.IGNORECASE,
)


# Even a "can't answer" goes to a person if it claims something was done or comes with a
# proposal: the text would be false, or the proposal needs a decision either way.
_SEEN_BY_A_PERSON = frozenset({"claims_side_effect", "proposes_action"})


@dataclass(frozen=True)
class Violation:
    code: str
    message: str
    severity: Literal["block", "review"]

    def render(self) -> str:
        return f"{self.severity}:{self.code}: {self.message}"


def check_policy(
    finding: Finding,
    tool_calls: list[ToolCallRecord],
    proposals: list[ProposedActionSpec],
    stop_reason: str | None,
) -> list[Violation]:
    text = " ".join([finding.answer, *finding.caveats, *(n.label for n in finding.numbers)])
    out: list[Violation] = []
    if _EMAIL_RE.search(text) or _PHONE_RE.search(text):
        out.append(Violation("personal_data", "the finding contains contact details", "block"))
    if _CLAIMED_EFFECT_RE.search(finding.answer):
        out.append(
            Violation(
                "claims_side_effect",
                "the answer says something was done; the agent can only propose actions",
                "review",
            )
        )
    queried = [c for c in tool_calls if c.tool == "query_warehouse" and c.ok]
    if finding.status == "answered" and not queried:
        out.append(
            Violation("unbacked_answer", "answered without a successful warehouse query", "review")
        )
    if any((c.result or {}).get("truncated") for c in queried):
        out.append(
            Violation(
                "partial_results",
                "at least one query result was cut off at the row limit",
                "review",
            )
        )
    if stop_reason:
        out.append(Violation("budget_exhausted", f"the run stopped early: {stop_reason}", "review"))
    if proposals:
        out.append(
            Violation(
                "proposes_action",
                f"{len(proposals)} proposed action(s) need a decision; "
                "the finding is reviewed with them",
                "review",
            )
        )
    if finding.status == "answered" and finding.confidence == "low":
        out.append(Violation("low_confidence", "the model rated its own answer low", "review"))
    return out


def route(finding: Finding | None, checks: Checks, violations: list[Violation]) -> Disposition:
    """Publish only what passed everything; hold what a person can still rescue."""
    if finding is None:
        # No finding at all: the model wrote something unparseable (rejected), or the run
        # ran out of time before it wrote anything (an honest "can't answer").
        return Disposition.REJECTED if checks.schema_errors else Disposition.UNANSWERABLE
    if any(v.severity == "block" for v in violations):
        return Disposition.REJECTED
    if finding.status == "unanswerable" and not any(
        v.code in _SEEN_BY_A_PERSON for v in violations
    ):
        return Disposition.UNANSWERABLE
    if checks.ungrounded or violations:
        return Disposition.HELD_FOR_REVIEW
    return Disposition.PUBLISHED
