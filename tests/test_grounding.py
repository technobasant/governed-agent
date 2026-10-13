"""The grounding check: numbers trace to query results, or the finding is held."""

from __future__ import annotations

from typing import Any

import pytest

from ga_agent.grounding import (
    CalculationError,
    check_finding,
    evaluate,
    extract_numbers,
    mention_of,
)
from ga_core.models import Finding, NumberClaim, ToolCallRecord


def call(
    call_id: str, sql: str, columns: list[str], rows: list[list[Any]], ok: bool = True
) -> ToolCallRecord:
    return ToolCallRecord(
        id=call_id,
        tool="query_warehouse",
        arguments={"sql": sql},
        ok=ok,
        result={
            "ok": ok,
            "executed_sql": sql,
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": False,
        }
        if ok
        else None,
    )


REVENUE = call(
    "q1",
    "SELECT channel, sum(x) AS revenue FROM t GROUP BY 1",
    ["channel", "revenue"],
    [["web", 1521407.35], ["app", 870636.82]],
)
COUNT = call("q2", "SELECT count(*) AS n FROM orders WHERE status = 'returned'", ["n"], [[742]])
MONTHS = call(
    "q3",
    "SELECT month, orders FROM m WHERE month >= DATE '2025-01-01'",
    ["month", "orders"],
    [["2025-03-01", 410], ["2025-04-01", 388]],
)


def finding(answer: str, numbers: list[NumberClaim] | None = None) -> Finding:
    return Finding(status="answered", answer=answer, numbers=numbers or [], confidence="high")


def verdicts(
    f: Finding,
    question: str = "q?",
    calls: list[ToolCallRecord] | None = None,
) -> dict[str, bool]:
    checks = check_finding(f, question, calls if calls is not None else [REVENUE, COUNT, MONTHS])
    return {c.text: c.grounded for c in checks}


@pytest.mark.parametrize(
    ("text", "values"),
    [
        ("742 orders", [742.0]),
        ("$1,521,407.35 in revenue", [1521407.35]),
        ("about 1.5 million", [1_500_000.0]),
        ("up 12.5% on last year", [12.5]),
        ("3rd place", []),
        ("SKU LL-0017 sold 4", [4.0]),
        ("1. Oak Mug\n2. Ash Bowl", []),
        ("between 10-20 units", [10.0, 20.0]),
        ("q2 and Q1", []),
        ("5k", [5000.0]),
    ],
)
def test_extracts_numbers_as_written(text: str, values: list[float]) -> None:
    mentions, _ = extract_numbers(text)
    assert [m.value for m in mentions] == values


def test_extracts_dates_separately() -> None:
    mentions, dates = extract_numbers("orders on 2025-03-14 and in 2025-04")
    assert dates == ["2025-03-14", "2025-04"]
    assert mentions == []


def test_numbers_from_results_are_grounded_and_linked_to_their_cell() -> None:
    checks = check_finding(finding("Web brought in 1,521,407.35 USD."), "q?", [REVENUE])
    assert checks[0].grounded
    assert (checks[0].tool_call_id, checks[0].column, checks[0].row) == ("q1", "revenue", 0)


def test_rounding_is_accepted_at_the_precision_written() -> None:
    assert verdicts(finding("Web brought in about 1.5 million; app 870.6k.")) == {
        "1.5 million": True,
        "870.6k": True,
    }


def test_an_invented_number_is_not_grounded() -> None:
    v = verdicts(finding("Returns were 742, up 18% on last year."))
    assert v == {"742": True, "18%": False}


def test_too_much_precision_is_not_grounded() -> None:
    # 742 is in the results; 742.4 claims a precision the results don't have.
    assert verdicts(finding("Returns were 742.4."))["742.4"] is False


def test_question_numbers_row_counts_and_dates() -> None:
    v = verdicts(
        finding("Of the top 5, 2 months are listed; March 2025 had 410 orders on 2025-03-01."),
        question="Show the top 5 months",
    )
    assert v == {"5": True, "2": True, "2025": True, "410": True, "2025-03-01": True}


def test_a_date_only_in_the_sql_counts_as_a_query_parameter() -> None:
    checks = check_finding(
        finding("Since 2025-01-01 there were 742 returns."), "q?", [MONTHS, COUNT]
    )
    date = next(c for c in checks if c.text == "2025-01-01")
    assert date.grounded and date.how == "query parameter"
    assert verdicts(finding("Back in 2019-02-01."))["2019-02-01"] is False


def test_constant_columns_are_not_evidence() -> None:
    laundered = call(
        "q9", "SELECT 3125053 AS revenue FROM orders LIMIT 1", ["revenue"], [[3125053]]
    )
    assert verdicts(finding("Revenue was 3,125,053."), calls=[laundered]) == {"3,125,053": False}


def test_failed_queries_are_not_evidence() -> None:
    failed = call("q1", "SELECT 742", [], [], ok=False)
    assert verdicts(finding("742 orders."), calls=[failed]) == {"742": False}


def test_percent_matches_a_fraction_in_the_results() -> None:
    rate = call("q1", "SELECT avg(x) AS rate FROM t", ["rate"], [[0.0824]])
    assert verdicts(finding("The return rate is 8.2%."), calls=[rate]) == {"8.2%": True}


def test_a_declared_calculation_grounds_a_derived_number() -> None:
    claim = NumberClaim(
        label="web share",
        value=63.6,
        unit="%",
        calculation="1521407.35 / (1521407.35 + 870636.82) * 100",
    )
    v = verdicts(finding("Web is 63.6% of revenue.", [claim]))
    assert v == {"63.6%": True, "web share: 63.6": True}


def test_a_value_found_directly_needs_no_calculation() -> None:
    # Models often describe the SQL in `calculation`; the value is in the result anyway.
    claim = NumberClaim(label="returns", value=742, calculation="COUNT(*) of returned orders")
    checks = check_finding(finding("742 returns.", [claim]), "q?", [COUNT])
    assert all(c.grounded for c in checks)


def test_a_calculation_with_an_invented_operand_is_not_grounded() -> None:
    claim = NumberClaim(label="growth", value=12.0, calculation="742 / 662 * 100 - 100")
    checks = check_finding(finding("Returns grew 12%.", [claim]), "q?", [COUNT])
    assert not any(c.grounded for c in checks)
    assert "operand 662" in (checks[-1].how or "")


def test_a_calculation_that_does_not_produce_the_claimed_value() -> None:
    claim = NumberClaim(label="avg", value=400.0, calculation="(410 + 388) / 2")
    checks = check_finding(finding("Average 400 a month.", [claim]), "q?", [MONTHS])
    assert checks[-1].grounded is False
    assert "= 399" in (checks[-1].how or "")


def test_a_claim_found_in_a_different_query_than_cited_is_noted() -> None:
    claim = NumberClaim(label="returns", value=742, source_query="q1")
    checks = check_finding(finding("742 returns.", [claim]), "q?", [REVENUE, COUNT])
    assert checks[-1].grounded
    assert "cited q1" in (checks[-1].how or "")


@pytest.mark.parametrize(
    ("expression", "value"),
    [
        ("1 + 2 * 3", 7.0),
        ("(10 - 4) / 3", 2.0),
        ("-5 + 1", -4.0),
        ("round(2 / 3, 2)", 0.67),
        ("1,520.5 / 38", 40.01315789473684),
        ("6 × 7", 42.0),
    ],
)
def test_evaluates_plain_arithmetic(expression: str, value: float) -> None:
    assert evaluate(expression)[0] == pytest.approx(value)


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('true')",
        "2 ** 10",
        "x + 1",
        "1 / 0",
        "round(1, n)",
        "the sum of sales",
        "open('f')",
    ],
)
def test_refuses_anything_but_arithmetic(expression: str) -> None:
    with pytest.raises(CalculationError):
        evaluate(expression)


def test_mention_of_uses_the_shortest_form() -> None:
    assert mention_of(742.0).text == "742"
    assert mention_of(8.25).tolerance == pytest.approx(0.005)
    assert mention_of(1e-12).tolerance < 1e-12
