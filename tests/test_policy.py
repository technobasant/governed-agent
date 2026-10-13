from __future__ import annotations

import pytest

from ga_agent.policy import Violation, check_policy, route
from ga_core.models import (
    Checks,
    Disposition,
    Finding,
    NumberCheck,
    ProposedActionSpec,
    ToolCallRecord,
)

QUERY = ToolCallRecord(
    id="q1",
    tool="query_warehouse",
    arguments={"sql": "SELECT 1"},
    ok=True,
    result={"ok": True, "rows": [[1]], "row_count": 1, "truncated": False},
)
PROPOSAL = ProposedActionSpec(
    proposal_id="prop_x",
    action_type="issue_refund",
    arguments={"order_id": 1},
    reason="asked",
    risk="high",
    tool_call_id="q2",
)


def answered(answer: str = "There were 742.", **kw: object) -> Finding:
    return Finding.model_validate(
        {"status": "answered", "answer": answer, "confidence": "high", **kw}
    )


def codes(violations: list[Violation]) -> list[str]:
    return [v.code for v in violations]


def test_a_clean_finding_has_no_violations() -> None:
    assert check_policy(answered(), [QUERY], [], None) == []


@pytest.mark.parametrize(
    "answer",
    ["The top customer is ada.archer12@example.test.", "Call them on +00 555 000123."],
)
def test_contact_details_block_the_finding(answer: str) -> None:
    violations = check_policy(answered(answer), [QUERY], [], None)
    assert codes(violations) == ["personal_data"]
    assert violations[0].severity == "block"


@pytest.mark.parametrize(
    "answer",
    [
        "I have issued a refund of 50 USD.",
        "The refund was processed.",
        "We've flagged supplier 3.",
        "The metric has been updated.",
    ],
)
def test_claiming_a_side_effect_is_held(answer: str) -> None:
    assert "claims_side_effect" in codes(check_policy(answered(answer), [QUERY], [], None))


def test_other_review_rules() -> None:
    truncated = QUERY.model_copy(update={"result": {**(QUERY.result or {}), "truncated": True}})
    found = codes(check_policy(answered(confidence="low"), [truncated], [PROPOSAL], "timeout"))
    assert found == ["partial_results", "budget_exhausted", "proposes_action", "low_confidence"]
    assert codes(check_policy(answered(), [], [], None)) == ["unbacked_answer"]


def test_routing() -> None:
    ok = answered()
    grounded = Checks(numbers=[NumberCheck(text="742", value=742, grounded=True)])
    ungrounded = Checks(numbers=[NumberCheck(text="18%", value=18, grounded=False)])
    review = [Violation("x", "y", "review")]
    block = [Violation("x", "y", "block")]
    assert route(ok, grounded, []) is Disposition.PUBLISHED
    assert route(ok, ungrounded, []) is Disposition.HELD_FOR_REVIEW
    assert route(ok, grounded, review) is Disposition.HELD_FOR_REVIEW
    assert route(ok, grounded, block) is Disposition.REJECTED
    assert route(None, Checks(schema_errors=["bad"]), []) is Disposition.REJECTED
    assert route(None, Checks(), []) is Disposition.UNANSWERABLE
    unanswerable = Finding(status="unanswerable", answer="No data.", confidence="low")
    assert route(unanswerable, Checks(), []) is Disposition.UNANSWERABLE
    assert route(unanswerable, Checks(), [Violation("budget_exhausted", "y", "review")]) is (
        Disposition.UNANSWERABLE
    )
    claimed = [Violation("claims_side_effect", "y", "review")]
    assert route(unanswerable, Checks(), claimed) is Disposition.HELD_FOR_REVIEW
    assert Violation("a", "b", "review").render() == "review:a: b"
