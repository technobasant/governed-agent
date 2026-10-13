"""The system under test, as evalproof calls it: a question in, a text block out.

By default the model is replayed from the recorded qwen3:8b cassette, so the suite runs
in CI without a model and gives the same output on every machine. Point GA_EVAL_MODEL at
a live model (e.g. `ollama:qwen3:8b`) to measure it, and set GA_EVAL_RECORD to a path to
record a new cassette while doing so.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from ga_agent.graph import Agent
from ga_agent.llm import ChatModel, RecordingModel, model_from_spec
from ga_agent.tools import connect
from ga_core.models import AgentResult
from ga_mcp.query import Warehouse
from ga_mcp.server import build_server

ROOT = Path(__file__).resolve().parent.parent
CASSETTE = ROOT / "evals" / "cassettes" / "qwen3-8b.jsonl"
MODEL = os.environ.get("GA_EVAL_MODEL", f"replay:{CASSETTE}@ollama:qwen3:8b")
RECORD = os.environ.get("GA_EVAL_RECORD")
WAREHOUSE = Path(os.environ.get("GA_WAREHOUSE", ROOT / "data" / "warehouse.duckdb"))

_warehouse: Warehouse | None = None

for name in ("ga_agent", "mcp", "httpx"):
    logging.getLogger(name).setLevel(logging.WARNING)


def _model() -> ChatModel:
    model = model_from_spec(MODEL)
    return RecordingModel(model, Path(RECORD)) if RECORD else model


def _line(text: str) -> str:
    return " ".join(text.split())


def render(result: AgentResult) -> str:
    """One line per fact, so a scorer can anchor a pattern to the line it is about."""
    f = result.finding
    calls: dict[str, int] = {}
    for c in result.tool_calls:
        calls[c.tool] = calls.get(c.tool, 0) + 1
    proposals = [
        f"{p.action_type}({', '.join(f'{k}={v}' for k, v in p.arguments.items())})"
        for p in result.proposals
    ]
    lines = [
        f"status: {f.status if f else 'none'}",
        f"disposition: {result.disposition.value}",
        f"answer: {_line(f.answer) if f else _line(result.fallback or '')}",
        "numbers: " + ("; ".join(f"{n.label}={n.value:g}" for n in f.numbers) if f else ""),
        f"proposals: {'; '.join(proposals) or 'none'}",
        "ungrounded: " + ("; ".join(n.text for n in result.checks.ungrounded) or "none"),
        "policy: " + ("; ".join(result.checks.policy_violations) or "none"),
        "schema: " + ("; ".join(result.checks.schema_errors) or "ok"),
        f"tool_calls: {len(result.tool_calls)} "
        + "("
        + ", ".join(f"{k} {v}" for k, v in sorted(calls.items()))
        + ")",
        f"model_calls: {result.usage.model_calls}",
        f"tokens: {result.usage.tokens_in} in, {result.usage.tokens_out} out",
        f"stop: {result.stop_reason or 'none'}",
        f"fallback: {_line(result.fallback or 'none')}",
    ]
    return "\n".join(lines)


async def _answer(question: str) -> AgentResult:
    global _warehouse
    if _warehouse is None:
        _warehouse = Warehouse(WAREHOUSE)
    async with connect(build_server(_warehouse)) as toolbox:
        return await Agent(_model(), toolbox).run(question)


def answer(case_input: str) -> str:
    return render(asyncio.run(_answer(case_input)))
