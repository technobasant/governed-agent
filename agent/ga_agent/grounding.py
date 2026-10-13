"""The grounding check: every number in a finding must trace back to a query result.

A number is grounded when, at the precision it is written, it matches

- a numeric cell in a successful `query_warehouse` result the model was shown,
- the row count of such a result,
- a year, month or day inside a date in such a result (or, for dates and years only,
  a literal in the executed SQL — those scope a query, they don't measure anything),
- a number in the question,

or when the finding declares a calculation whose operands are grounded and whose result
matches. A column the query computes from no table (`SELECT 42 AS revenue`) is not
evidence. Nothing here asks a model whether the answer looks right.

What this does not catch: a real number attached to the wrong label. The check proves a
number came from the data, not that it came from the right query; each verdict links to
the cell it matched so the reviewer can see which.
"""

from __future__ import annotations

import ast
import math
import operator
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp

from ga_core.models import Finding, NumberCheck, NumberClaim, ToolCallRecord

_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})(?:-(\d{2}))?\b")
_LIST_MARKER_RE = re.compile(r"(?m)^\s*\d{1,2}[.)]\s")
_NUMBER_RE = re.compile(
    r"(?<![\w.])"
    r"(?P<int>\d{1,3}(?:,\d{3})+|\d+)"
    r"(?:\.(?P<frac>\d+))?"
    r"(?:\s?(?P<suffix>%|percent|pct|thousand|million|billion|bn|mn|k|K|m|M)(?![A-Za-z])"
    r"|(?![A-Za-z0-9_]))"
)
_SCALE = {
    "k": 1e3,
    "K": 1e3,
    "thousand": 1e3,
    "m": 1e6,
    "M": 1e6,
    "mn": 1e6,
    "million": 1e6,
    "bn": 1e9,
    "billion": 1e9,
}
_PERCENT = {"%", "percent", "pct"}
_CALC_CONSTANTS = {100.0}
"""Numbers a calculation may use without a source: converting a fraction to a percent."""


@dataclass(frozen=True)
class Mention:
    """A number as written: its value, and how precisely it was written."""

    text: str
    value: float
    tolerance: float
    percent: bool = False

    def matches(self, candidate: float) -> bool:
        slack = self.tolerance + 1e-9 * max(1.0, abs(self.value))
        if abs(abs(candidate) - abs(self.value)) <= slack:
            return True
        return self.percent and abs(abs(candidate) * 100 - abs(self.value)) <= slack


def _mention(text: str, int_part: str, frac: str | None, suffix: str | None) -> Mention:
    decimals = len(frac or "")
    scale = _SCALE.get(suffix or "", 1.0)
    value = float(int_part.replace(",", "") + (f".{frac}" if frac else "")) * scale
    return Mention(
        text=text,
        value=value,
        tolerance=0.5 * 10**-decimals * scale,
        percent=(suffix or "") in _PERCENT,
    )


def extract_numbers(text: str) -> tuple[list[Mention], list[str]]:
    """Numbers and ISO dates written in `text`. List markers and identifiers are skipped."""
    dates = [m.group(0) for m in _DATE_RE.finditer(text)]
    masked = _DATE_RE.sub(" ", text)
    masked = _LIST_MARKER_RE.sub(" ", masked)
    mentions = []
    for m in _NUMBER_RE.finditer(masked):
        start = m.start()
        # `LL-0017` is an identifier, not a number; `10-20` is a range of two numbers.
        if start >= 2 and masked[start - 1] == "-" and masked[start - 2].isalpha():
            continue
        mentions.append(_mention(m.group(0).strip(), m["int"], m["frac"], m["suffix"]))
    return mentions, dates


def mention_of(value: float) -> Mention:
    """A claimed value, held to the precision its shortest form shows."""
    text = repr(float(value))
    if "e" in text or "E" in text:
        return Mention(text=text, value=value, tolerance=abs(value) * 1e-9)
    whole, _, frac = text.partition(".")
    frac = "" if frac == "0" else frac
    return _mention(f"{whole}.{frac}" if frac else whole, whole.lstrip("-"), frac or None, None)


@dataclass(frozen=True)
class Cell:
    value: float
    call_id: str
    column: str
    row: int


@dataclass
class Evidence:
    cells: list[Cell] = field(default_factory=list)
    date_parts: list[Cell] = field(default_factory=list)
    date_strings: list[tuple[str, Cell]] = field(default_factory=list)
    row_counts: list[Cell] = field(default_factory=list)
    sql_dates: list[tuple[str, str]] = field(default_factory=list)
    question: list[Mention] = field(default_factory=list)
    question_dates: list[str] = field(default_factory=list)


def _constant_columns(sql: str) -> set[int]:
    """Positions of output columns computed from no table, e.g. `SELECT 42 AS revenue`."""
    try:
        root = sqlglot.parse_one(sql, read="duckdb")
    except sqlglot.errors.ParseError:
        return set()
    if not isinstance(root, exp.Select):
        return set()
    constant = set()
    for i, projection in enumerate(root.expressions):
        if projection.find(exp.Column, exp.Star, exp.AggFunc) is None:
            constant.add(i)
    return constant


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float) and math.isfinite(value):
        return float(value)
    return None


def collect_evidence(question: str, tool_calls: Iterable[ToolCallRecord]) -> Evidence:
    ev = Evidence()
    ev.question, ev.question_dates = extract_numbers(question)
    for call in tool_calls:
        if call.tool != "query_warehouse" or not call.ok or not call.result:
            continue
        result = call.result
        sql = str(result.get("executed_sql", ""))
        ev.sql_dates += [(m.group(0), call.id) for m in _DATE_RE.finditer(sql)]
        ev.row_counts.append(Cell(float(result.get("row_count", 0)), call.id, "(row count)", -1))
        columns: list[str] = list(result.get("columns", []))
        constant = _constant_columns(sql)
        for r, row in enumerate(result.get("rows", [])):
            for c, value in enumerate(row):
                column = columns[c] if c < len(columns) else str(c)
                number = _as_number(value)
                if number is not None:
                    if c not in constant:
                        ev.cells.append(Cell(number, call.id, column, r))
                elif isinstance(value, str) and (d := _DATE_RE.fullmatch(value.strip()[:10])):
                    ev.date_strings.append((value.strip()[:10], Cell(0.0, call.id, column, r)))
                    for part in d.groups():
                        if part is not None:
                            ev.date_parts.append(Cell(float(part), call.id, column, r))
    return ev


def _find(mention: Mention, cells: list[Cell]) -> Cell | None:
    """The closest matching cell, so a reviewer is pointed at the likeliest source."""
    matches = [cell for cell in cells if mention.matches(cell.value)]
    return min(matches, key=lambda c: abs(abs(c.value) - abs(mention.value)), default=None)


def _is_year(m: Mention) -> bool:
    return m.tolerance == 0.5 and 1900 <= m.value <= 2100 and not m.percent


def ground_value(mention: Mention, ev: Evidence) -> NumberCheck:
    """Match one number against the evidence, strongest source first."""
    check = NumberCheck(text=mention.text, value=mention.value, grounded=True)
    if cell := _find(mention, ev.cells):
        return check.model_copy(
            update={
                "how": "query result",
                "tool_call_id": cell.call_id,
                "column": cell.column,
                "row": cell.row,
            }
        )
    if cell := _find(mention, ev.row_counts):
        return check.model_copy(update={"how": "row count", "tool_call_id": cell.call_id})
    if any(mention.matches(q.value) for q in ev.question):
        return check.model_copy(update={"how": "question"})
    if (cell := _find(mention, ev.date_parts)) and mention.tolerance <= 0.5:
        return check.model_copy(
            update={
                "how": "date in result",
                "tool_call_id": cell.call_id,
                "column": cell.column,
                "row": cell.row,
            }
        )
    if _is_year(mention):
        for date, call_id in ev.sql_dates:
            if date.startswith(str(int(mention.value))):
                return check.model_copy(update={"how": "query parameter", "tool_call_id": call_id})
    return check.model_copy(update={"grounded": False, "how": "not found in any query result"})


def ground_date(text: str, ev: Evidence) -> NumberCheck:
    check = NumberCheck(text=text, value=float(text[:4]), grounded=True)
    for date, cell in ev.date_strings:
        if date.startswith(text):
            return check.model_copy(
                update={
                    "how": "date in result",
                    "tool_call_id": cell.call_id,
                    "column": cell.column,
                    "row": cell.row,
                }
            )
    if any(q.startswith(text) or text.startswith(q) for q in ev.question_dates):
        return check.model_copy(update={"how": "question"})
    for date, call_id in ev.sql_dates:
        if date.startswith(text) or text.startswith(date):
            return check.model_copy(update={"how": "query parameter", "tool_call_id": call_id})
    return check.model_copy(update={"grounded": False, "how": "date not found in any query result"})


class CalculationError(ValueError):
    pass


_BINARY: dict[type[ast.operator], Callable[[float, float], float]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}


def evaluate(expression: str) -> tuple[float, list[float]]:
    """Evaluate plain arithmetic. Returns the result and every literal operand used."""
    cleaned = re.sub(r"(?<=\d),(?=\d{3})", "", expression)
    cleaned = cleaned.replace("×", "*").replace("÷", "/")
    try:
        tree = ast.parse(cleaned, mode="eval")
    except SyntaxError as exc:
        raise CalculationError(f"not arithmetic: {expression!r}") from exc
    operands: list[float] = []

    def walk(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant) and (number := _as_number(node.value)) is not None:
            operands.append(number)
            return number
        if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
            left, right = walk(node.left), walk(node.right)
            if isinstance(node.op, ast.Div) and right == 0:
                raise CalculationError("division by zero")
            return _BINARY[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub | ast.UAdd):
            value = walk(node.operand)
            return -value if isinstance(node.op, ast.USub) else value
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "round"
            and 1 <= len(node.args) <= 2
            and not node.keywords
        ):
            value = walk(node.args[0])
            digits = 0
            if len(node.args) == 2:
                if not isinstance(node.args[1], ast.Constant) or not isinstance(
                    node.args[1].value, int
                ):
                    raise CalculationError("round() takes a literal number of digits")
                digits = node.args[1].value
            return round(value, digits)
        raise CalculationError(f"only + - * / and round() are allowed: {expression!r}")

    return walk(tree), operands


def ground_claim(claim: NumberClaim, ev: Evidence) -> NumberCheck:
    """A claimed number is grounded if it is in the evidence as it stands; failing that,
    if its declared calculation checks out. Models often put a description of the SQL in
    `calculation` ("AVG(amount)"); that is not a derivation, and a value found directly
    does not need one."""
    target = mention_of(claim.value)
    label = f"{claim.label}: {target.text}"
    found = ground_value(target, ev)
    if found.grounded or not claim.calculation:
        result = found.model_copy(update={"text": label})
        if (
            found.grounded
            and claim.source_query
            and found.tool_call_id not in (None, claim.source_query)
        ):
            where = f"found in {found.tool_call_id}, cited {claim.source_query}"
            result = result.model_copy(update={"how": f"{found.how} ({where})"})
        return result
    check = NumberCheck(text=label, value=claim.value, grounded=False)
    try:
        value, operands = evaluate(claim.calculation)
    except CalculationError as exc:
        return check.model_copy(update={"how": str(exc)})
    for operand in operands:
        if operand in _CALC_CONSTANTS:
            continue
        if not ground_value(mention_of(operand), ev).grounded:
            missing = f"operand {operand:g} in '{claim.calculation}' is not in any query result"
            return check.model_copy(update={"how": missing})
    if not target.matches(value):
        return check.model_copy(
            update={"how": f"'{claim.calculation}' = {value:.6g}, not {claim.value:g}"}
        )
    return check.model_copy(update={"grounded": True, "how": f"calculation: {claim.calculation}"})


def check_finding(
    finding: Finding, question: str, tool_calls: list[ToolCallRecord]
) -> list[NumberCheck]:
    """Ground every number in the answer and every declared number claim."""
    ev = collect_evidence(question, tool_calls)
    claims = [ground_claim(c, ev) for c in finding.numbers]
    derived = [
        mention_of(c.value)
        for c, chk in zip(finding.numbers, claims, strict=True)
        if chk.grounded and c.calculation
    ]
    mentions, dates = extract_numbers(finding.answer)
    checks: list[NumberCheck] = []
    for m in mentions[:40]:
        verdict = ground_value(m, ev)
        if not verdict.grounded:
            calc = next(
                (
                    c
                    for c, d in zip(finding.numbers, claims, strict=True)
                    if d.grounded and c.calculation and m.matches(c.value)
                ),
                None,
            )
            if calc is not None or any(m.matches(d.value) for d in derived):
                verdict = verdict.model_copy(
                    update={
                        "grounded": True,
                        "how": f"calculation: {calc.calculation}"
                        if calc
                        else "declared calculation",
                    }
                )
        checks.append(verdict)
    checks += [ground_date(d, ev) for d in dates[:20]]
    return checks + claims
