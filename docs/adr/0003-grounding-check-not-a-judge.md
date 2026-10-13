# 3. A deterministic grounding check, not a model judging the answer

Status: accepted · 2026-10

## Context

The failure that matters most in an analytics answer is a number that was never in the
data: invented, mis-remembered, or computed in the model's head. A second model asked
"is this answer supported?" is itself a classifier with error rates, and it has to be
calibrated before its verdict means anything.

## Decision

* Every number written in the answer, and every number the finding declares, must match
  — at the precision it is written — a numeric cell in a successful query result the model
  was shown, that result's row count, a part of a date in it, or a number in the question.
* A derived number is accepted only with a declared calculation (`1520.5 / 38`). The
  calculation is evaluated with a small arithmetic evaluator (`+ - * /` and `round`), its
  operands must themselves be grounded, and its result must match the claimed value.
* A column a query computes from no table (`SELECT 42 AS revenue`) is not evidence.
* Each verdict records the call, column and row it matched, so the UI can link a number to
  its cell.
* A finding with any ungrounded number is held for review, not published.

## Consequences

* The check proves a number came from the data, not that it came from the right query. A
  correct number under the wrong label passes. The link to the source cell is there so a
  reviewer can see which query a figure came from.
* Small integers match easily, so the check is strongest for the figures that matter most:
  totals, averages and rates.
* No model call, no calibration, and the same verdict on every run.
