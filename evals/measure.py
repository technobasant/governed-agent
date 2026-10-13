"""Summarise an evalproof run of the golden set into the numbers MEASUREMENTS.md reports.

    uv run python evals/measure.py evals/runs/qwen3-8b-live.json

Reads the run (outputs, scores, latencies) and the golden set (categories), and prints a
Markdown summary. Writes the same figures as JSON next to the run.
"""

from __future__ import annotations

import json
import math
import re
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent


def wilson(passed: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = passed / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (max(0.0, centre - half), min(1.0, centre + half))


def field(output: str, name: str) -> str:
    match = re.search(rf"^{name}: (.*)$", output, re.MULTILINE)
    return match.group(1) if match else ""


def pct(part: int, whole: int) -> str:
    return f"{100 * part / whole:.0f}%" if whole else "—"


def rate(passed: int, n: int) -> str:
    low, high = wilson(passed, n)
    return f"{passed}/{n} ({100 * passed / n:.0f}%, 95% CI {100 * low:.0f}–{100 * high:.0f}%)"


def held_for(row: dict[str, Any]) -> str:
    if row["proposals"]:
        return "a proposal to decide"
    if row["ungrounded"]:
        return "an ungrounded number"
    return "a policy rule"


def summarise(run: dict[str, Any], golden: dict[str, dict[str, Any]]) -> dict[str, Any]:
    rows = []
    for r in run["results"]:
        out = r["output"]["output"]
        case = golden[r["case_id"]]
        call_count = re.match(r"(\d+)", field(out, "tool_calls"))
        rows.append(
            {
                "id": r["case_id"],
                "category": case["tags"][0],
                "tags": case["tags"],
                "outcome": bool(r["scores"]["outcome"]["passed"]),
                "safety": bool(r["scores"]["safety"]["passed"]),
                "key_value": r["scores"].get("key_value", {}).get("passed"),
                "disposition": field(out, "disposition") or "error",
                "status": field(out, "status"),
                "ungrounded": field(out, "ungrounded") not in ("", "none"),
                "proposals": field(out, "proposals") not in ("", "none"),
                "policy": field(out, "policy"),
                "tool_calls": int(call_count.group(1)) if call_count else 0,
                "model_calls": int(field(out, "model_calls") or 0),
                "latency_s": r["output"]["latency_ms"] / 1000,
                "error": r["output"].get("error"),
            }
        )
    n = len(rows)
    by_category = {}
    for category in dict.fromkeys(r["category"] for r in rows):
        subset = [r for r in rows if r["category"] == category]
        by_category[category] = (sum(r["outcome"] for r in subset), len(subset))

    published = [r for r in rows if r["disposition"] == "published"]
    flagged = [r for r in rows if r["ungrounded"]]
    injection = [r for r in rows if "injection" in r["tags"]]
    data_injection = [r for r in injection if "data" in r["tags"]]
    unanswerable = [r for r in rows if r["category"] == "unanswerable"]
    numeric = [r for r in rows if r["key_value"] is not None]
    latencies = sorted(r["latency_s"] for r in rows if not r["error"])
    calls = [r["tool_calls"] for r in rows]
    return {
        "cases": n,
        "errors": sum(1 for r in rows if r["error"]),
        "outcome": (sum(r["outcome"] for r in rows), n),
        "by_category": by_category,
        "key_value": (sum(bool(r["key_value"]) for r in numeric), len(numeric)),
        "dispositions": Counter(r["disposition"] for r in rows),
        "published_wrong": [r["id"] for r in published if not r["outcome"]],
        "published": len(published),
        "held_right": {
            reason: [
                r["id"]
                for r in rows
                if r["disposition"] == "held_for_review" and r["outcome"] and held_for(r) == reason
            ]
            for reason in ("a proposal to decide", "an ungrounded number", "a policy rule")
        },
        "grounding_flagged": [r["id"] for r in flagged],
        "grounding_flagged_wrong": [r["id"] for r in flagged if not r["outcome"]],
        "wrong": {
            d: [r["id"] for r in rows if not r["outcome"] and r["disposition"] == d]
            for d in ("published", "held_for_review", "unanswerable", "rejected", "error")
        },
        "safety": (sum(r["safety"] for r in rows), n),
        "injection_safe": (sum(r["safety"] for r in injection), len(injection)),
        "data_injection_proposals": [r["id"] for r in data_injection if r["proposals"]],
        "unanswerable_status": (
            sum(r["status"] == "unanswerable" for r in unanswerable),
            len(unanswerable),
        ),
        "latency_p50_s": statistics.median(latencies) if latencies else None,
        "latency_p95_s": (
            statistics.quantiles(latencies, n=100, method="inclusive")[94]
            if len(latencies) > 1
            else None
        ),
        "latency_max_s": max(latencies) if latencies else None,
        "tool_calls_mean": statistics.fmean(calls),
        "tool_calls_median": statistics.median(calls),
        "tool_calls_max": max(calls),
        "model_calls_mean": statistics.fmean(r["model_calls"] for r in rows),
        "rows": rows,
    }


def markdown(s: dict[str, Any]) -> str:
    d = s["dispositions"]
    n = s["cases"]
    lines = [
        "| Measure | Result |",
        "|---|---|",
        f"| Answer matches the key (all {n} cases) | {rate(*s['outcome'])} |",
    ]
    for category, (passed, total) in s["by_category"].items():
        lines.append(f"| — {category} | {passed}/{total} |")
    lines += [
        f"| Numeric key within 0.5% | {rate(*s['key_value'])} |",
        f"| Published | {d['published']} ({pct(d['published'], n)}) |",
        f"| Held for review | {d['held_for_review']} ({pct(d['held_for_review'], n)}) |",
        f"| Unanswerable (honest fallback) | {d['unanswerable']} ({pct(d['unanswerable'], n)}) |",
        f"| Rejected | {d['rejected']} ({pct(d['rejected'], n)}) |",
        f"| Published and wrong | {len(s['published_wrong'])} of {s['published']} published"
        f" {s['published_wrong'] or ''} |",
        "| Held for review but right, because of | "
        + "; ".join(f"{why} {len(ids)} {ids}" for why, ids in s["held_right"].items() if ids)
        + " |",
        f"| Grounding check flagged a number | {len(s['grounding_flagged'])} cases; "
        f"{len(s['grounding_flagged_wrong'])} of them wrong answers |",
        "| Where the wrong answers went | "
        + "; ".join(
            f"{d.replace('_', ' ')} {len(ids)} {ids}" for d, ids in s["wrong"].items() if ids
        )
        + " |",
        f"| Injection cases with nothing unsafe | {rate(*s['injection_safe'])} |",
        f"| Data-injection cases where the model proposed an action | "
        f"{len(s['data_injection_proposals'])} {s['data_injection_proposals'] or ''} |",
        "| Actions executed without approval | 0 (no execution path; see tests) |",
        f"| Unanswerable questions marked unanswerable | {rate(*s['unanswerable_status'])} |",
        f"| Latency per question, p50 / p95 / max | {s['latency_p50_s']:.1f} s / "
        f"{s['latency_p95_s']:.1f} s / {s['latency_max_s']:.1f} s |",
        f"| Tool calls per question, mean / median / max | {s['tool_calls_mean']:.1f} / "
        f"{s['tool_calls_median']:.0f} / {s['tool_calls_max']} |",
        f"| Model calls per question, mean | {s['model_calls_mean']:.1f} |",
        f"| Errors | {s['errors']} |",
    ]
    return "\n".join(lines)


def main() -> None:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else HERE / "runs" / "qwen3-8b-live.json")
    run = json.loads(path.read_text())
    golden = {c["id"]: c for c in map(json.loads, (HERE / "golden.jsonl").read_text().splitlines())}
    summary = summarise(run, golden)
    print(markdown(summary))
    print("\nPer case:")
    for r in summary["rows"]:
        mark = "ok " if r["outcome"] else "NO "
        print(
            f"  {mark} {r['id']:4} {r['disposition']:16} calls={r['tool_calls']} "
            f"{r['latency_s']:5.1f}s ungrounded={r['ungrounded']} safety={r['safety']}"
        )
    out = path.with_suffix(".summary.json")
    out.write_text(
        json.dumps({k: v for k, v in summary.items() if k != "rows"}, indent=2, default=list) + "\n"
    )


if __name__ == "__main__":
    main()
