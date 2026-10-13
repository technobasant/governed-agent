"""The agent as an explicit graph: plan → act ⇄ tools → draft → validate → policy → ground → route.

Every edge is a decision this module makes, not the model. The model chooses which tools
to call and what to write; the graph decides when it has had enough turns, whether what
it wrote is a `Finding` at all, and whether that finding may become state.

    plan      load the table catalog over MCP and frame the question
    act       one model turn with the tools available
    tools     run the requested tool calls through MCP, within the budget
    draft     ask for the finding as JSON against the `Finding` schema
    validate  parse it; one repair attempt, then reject
    policy    deterministic rules: personal data, claimed side effects, partial results
    ground    every number must trace to a query result or a declared calculation
    route     publish, hold for review, report unanswerable, or reject
"""

from __future__ import annotations

import asyncio
import json
import logging
import operator
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from importlib.resources import files
from typing import Annotated, Any, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from ga_agent.grounding import check_finding
from ga_agent.llm import ChatModel, Message, ModelReply, ToolCall, ToolSpec
from ga_agent.policy import Violation, check_policy, route
from ga_agent.tools import ToolBox
from ga_core.models import (
    AgentResult,
    Checks,
    Disposition,
    Finding,
    ProposedActionSpec,
    Step,
    ToolCallRecord,
    Usage,
)
from ga_core.telemetry import log, run_id_var, tracer

_log = logging.getLogger("ga_agent")
_tracer = tracer("ga_agent")

SYSTEM_PROMPT = files("ga_agent").joinpath("prompts/system.md").read_text()
DRAFT_PROMPT = files("ga_agent").joinpath("prompts/draft.md").read_text()
FINDING_SCHEMA: dict[str, Any] = Finding.model_json_schema()
FALLBACK = "I can't answer this from the data available."


@dataclass(frozen=True)
class Limits:
    max_model_turns: int = 8
    max_tool_calls: int = 8
    max_proposals: int = 2
    timeout_s: float = 300.0
    repair_attempts: int = 1
    rows_shown: int = 50
    """Rows of a result the model sees. Grounding checks against exactly these rows."""


class AgentState(TypedDict, total=False):
    question: str
    t0: float
    deadline: float
    messages: Annotated[list[Message], operator.add]
    tool_calls: Annotated[list[ToolCallRecord], operator.add]
    proposals: Annotated[list[ProposedActionSpec], operator.add]
    steps: Annotated[list[Step], operator.add]
    pending: list[ToolCall]
    model_turns: int
    usage: Usage
    drafts: list[str]
    finding: Finding | None
    checks: Checks
    violations: list[Violation]
    stop_reason: str | None
    disposition: Disposition
    fallback: str | None


def render_catalog(tables: list[dict[str, Any]]) -> str:
    parts = []
    for t in tables:
        lines = [f"### {t['table']}", t.get("description", "")]
        for c in t.get("columns", []):
            note = f" — {c['note']}" if c.get("note") else ""
            lines.append(f"- {c['name']} {c['type']}{note}")
        if t.get("blocked_columns"):
            lines.append(f"- blocked (personal data): {', '.join(t['blocked_columns'])}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _shown(result: dict[str, Any], rows_shown: int) -> dict[str, Any]:
    """The part of a tool result the model sees, and that grounding checks against."""
    rows = result.get("rows")
    if not isinstance(rows, list) or len(rows) <= rows_shown:
        return result
    return {
        **result,
        "rows": rows[:rows_shown],
        "rows_hidden": len(rows) - rows_shown,
        "note": f"only the first {rows_shown} rows are shown; aggregate in SQL instead",
    }


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    return text.strip()


def _add_usage(usage: Usage | None, reply: ModelReply) -> Usage:
    usage = usage or Usage()
    return Usage(
        model_calls=usage.model_calls + 1,
        tokens_in=usage.tokens_in + reply.tokens_in,
        tokens_out=usage.tokens_out + reply.tokens_out,
    )


def fallback_text(tool_calls: list[ToolCallRecord]) -> str:
    tried = [c for c in tool_calls if c.tool == "query_warehouse"]
    if not tried:
        return f"{FALLBACK} No queries were run."
    lines = [FALLBACK, "Queries tried:"]
    for c in tried:
        sql = " ".join(str(c.arguments.get("sql", "")).split())[:300]
        outcome = f"{(c.result or {}).get('row_count', 0)} rows" if c.ok else (c.error or "failed")
        lines.append(f"- {c.id}: {sql} ({outcome})")
    return "\n".join(lines)


NodeFn = Callable[[AgentState], Awaitable[dict[str, Any]]]


class Node(Protocol):
    """What LangGraph calls: one argument, named `state`."""

    def __call__(self, state: AgentState) -> Awaitable[dict[str, Any]]: ...


class Agent:
    """Answers one question per `run`. Safe to share; holds no per-run state."""

    def __init__(self, model: ChatModel, toolbox: ToolBox, limits: Limits | None = None) -> None:
        self.model = model
        self.toolbox = toolbox
        self.limits = limits or Limits()
        self._catalog: str | None = None
        self._graph = self._build()

    # ---- nodes -------------------------------------------------------------------------

    async def _plan(self, state: AgentState) -> dict[str, Any]:
        cached = self._catalog is not None
        if self._catalog is None:
            listed = await self.toolbox.call("list_tables", {})
            described = [
                await self.toolbox.call("describe_table", {"table": t["table"]})
                for t in listed.get("tables", [])
            ]
            self._catalog = render_catalog([d for d in described if d.get("ok")])
        system = SYSTEM_PROMPT.replace("{max_tool_calls}", str(self.limits.max_tool_calls))
        system = system.replace("{catalog}", self._catalog)
        return {
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": state["question"]},
            ],
            "_detail": {"catalog_cached": cached},
        }

    async def _chat(
        self,
        state: AgentState,
        messages: list[Message],
        tools: list[ToolSpec],
        schema: dict[str, Any] | None = None,
    ) -> ModelReply | None:
        remaining = state["deadline"] - time.monotonic()
        if remaining <= 0:
            return None
        try:
            async with asyncio.timeout(remaining):
                return await self.model.chat(messages, tools, schema=schema)
        except TimeoutError:
            return None

    async def _act(self, state: AgentState) -> dict[str, Any]:
        turns = state.get("model_turns", 0)
        if turns >= self.limits.max_model_turns:
            return {"pending": [], "stop_reason": "max_model_turns"}
        specs = await self.toolbox.specs()
        reply = await self._chat(state, state["messages"], specs)
        if reply is None:
            return {"pending": [], "stop_reason": "timeout"}
        start = len(state.get("tool_calls", [])) + len(state.get("pending", []))
        calls = [
            ToolCall(id=f"q{start + i + 1}", name=c.name, arguments=c.arguments)
            for i, c in enumerate(reply.tool_calls)
        ]
        message: Message = {"role": "assistant", "content": reply.content}
        if calls:
            message["tool_calls"] = [c.model_dump() for c in calls]
        return {
            "messages": [message],
            "pending": calls,
            "model_turns": turns + 1,
            "usage": _add_usage(state.get("usage"), reply),
            "_detail": {"tool_calls": [c.name for c in calls], "tokens_out": reply.tokens_out},
        }

    async def _tools(self, state: AgentState) -> dict[str, Any]:
        used = len(state.get("tool_calls", []))
        proposals = len(state.get("proposals", []))
        names = {s.name for s in await self.toolbox.specs()}
        records: list[ToolCallRecord] = []
        messages: list[Message] = []
        new_proposals: list[ProposedActionSpec] = []
        stop: str | None = None
        for call in state.get("pending", []):
            started = time.perf_counter()
            if used >= self.limits.max_tool_calls:
                result = {"ok": False, "error": "tool budget used up; write your finding now"}
                stop = "max_tool_calls"
            elif call.name not in names:
                result = {"ok": False, "error": f"unknown tool {call.name!r}"}
            elif call.name == "propose_action" and proposals >= self.limits.max_proposals:
                result = {"ok": False, "error": "proposal limit reached for this run"}
            else:
                with _tracer.start_as_current_span("agent.tool") as span:
                    span.set_attribute("ga.tool", call.name)
                    span.set_attribute("ga.call_id", call.id)
                    result = await self.toolbox.call(call.name, call.arguments)
                    span.set_attribute("ga.ok", bool(result.get("ok")))
                used += 1
            shown = _shown(result, self.limits.rows_shown)
            ok = bool(shown.get("ok"))
            records.append(
                ToolCallRecord(
                    id=call.id,
                    tool=call.name,
                    arguments=call.arguments,
                    ok=ok,
                    result=shown if ok else None,
                    error=None if ok else str(shown.get("error", "failed")),
                    duration_ms=round((time.perf_counter() - started) * 1000, 1),
                )
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": call.name,
                    "content": json.dumps({"call_id": call.id, **shown}, default=str),
                }
            )
            if call.name == "propose_action" and ok:
                proposals += 1
                new_proposals.append(
                    ProposedActionSpec(
                        proposal_id=shown["proposal_id"],
                        action_type=shown["action_type"],
                        arguments=shown["arguments"],
                        reason=shown["reason"],
                        risk=shown["risk"],
                        tool_call_id=call.id,
                    )
                )
        if stop is None and used >= self.limits.max_tool_calls:
            stop = "max_tool_calls"
        update: dict[str, Any] = {
            "tool_calls": records,
            "messages": messages,
            "proposals": new_proposals,
            "pending": [],
            "_detail": {"calls": [f"{r.id}:{r.tool}:{'ok' if r.ok else 'error'}" for r in records]},
        }
        if stop:
            update["stop_reason"] = stop
        return update

    async def _draft(self, state: AgentState) -> dict[str, Any]:
        drafts = state.get("drafts", [])
        messages = [*state["messages"], {"role": "user", "content": DRAFT_PROMPT}]
        if drafts:
            errors = "; ".join(state["checks"].schema_errors)
            messages += [
                {"role": "assistant", "content": drafts[-1]},
                {
                    "role": "user",
                    "content": f"That did not match the schema: {errors}. "
                    "Return corrected JSON only.",
                },
            ]
        reply = await self._chat(state, messages, [], FINDING_SCHEMA)
        if reply is None:
            return {"drafts": [*drafts], "stop_reason": "timeout", "_detail": {"timeout": True}}
        return {
            "drafts": [*drafts, reply.content],
            "usage": _add_usage(state.get("usage"), reply),
            "_detail": {"attempt": len(drafts) + 1, "tokens_out": reply.tokens_out},
        }

    async def _validate(self, state: AgentState) -> dict[str, Any]:
        drafts = state.get("drafts", [])
        if not drafts:
            return {"finding": None, "checks": Checks(schema_errors=["no finding was produced"])}
        try:
            finding = Finding.model_validate_json(_strip_fences(drafts[-1]))
        except ValidationError as exc:
            errors = [
                f"{'.'.join(str(p) for p in e['loc']) or 'finding'}: {e['msg']}"
                for e in exc.errors()[:10]
            ]
            return {
                "finding": None,
                "checks": Checks(schema_errors=errors),
                "_detail": {"errors": errors},
            }
        return {"finding": finding, "checks": Checks(), "_detail": {"status": finding.status}}

    async def _policy(self, state: AgentState) -> dict[str, Any]:
        finding = state["finding"]
        assert finding is not None
        violations = check_policy(
            finding,
            state.get("tool_calls", []),
            state.get("proposals", []),
            state.get("stop_reason"),
        )
        checks = state["checks"].model_copy(
            update={"policy_violations": [v.render() for v in violations]}
        )
        return {
            "violations": violations,
            "checks": checks,
            "_detail": {"violations": [v.code for v in violations]},
        }

    async def _ground(self, state: AgentState) -> dict[str, Any]:
        finding = state["finding"]
        assert finding is not None
        numbers = check_finding(finding, state["question"], state.get("tool_calls", []))
        checks = state["checks"].model_copy(update={"numbers": numbers})
        return {
            "checks": checks,
            "_detail": {
                "numbers": len(numbers),
                "ungrounded": [n.text for n in numbers if not n.grounded],
            },
        }

    async def _route(self, state: AgentState) -> dict[str, Any]:
        finding = state.get("finding")
        checks = state.get("checks") or Checks()
        disposition = route(finding, checks, state.get("violations", []))
        fallback = None
        if disposition in (Disposition.UNANSWERABLE, Disposition.REJECTED):
            fallback = fallback_text(state.get("tool_calls", []))
        return {
            "disposition": disposition,
            "fallback": fallback,
            "_detail": {"disposition": disposition.value},
        }

    # ---- edges -------------------------------------------------------------------------

    @staticmethod
    def _after_act(state: AgentState) -> str:
        if state.get("stop_reason") == "timeout":
            return "route"
        if state.get("pending"):
            return "tools"
        return "draft"

    def _after_tools(self, state: AgentState) -> str:
        if state.get("stop_reason") == "max_tool_calls":
            return "draft"
        return "act"

    @staticmethod
    def _after_draft(state: AgentState) -> str:
        return "route" if state.get("stop_reason") == "timeout" else "validate"

    def _after_validate(self, state: AgentState) -> str:
        if state.get("finding") is not None:
            return "policy"
        if len(state.get("drafts", [])) <= self.limits.repair_attempts and state.get("drafts"):
            return "draft"
        return "route"

    # ---- wiring ------------------------------------------------------------------------

    def _timed(self, name: str, fn: NodeFn) -> Node:
        async def node(state: AgentState) -> dict[str, Any]:
            started = time.perf_counter()
            with _tracer.start_as_current_span(f"agent.{name}") as span:
                span.set_attribute("ga.run_id", run_id_var.get() or "")
                update = await fn(state)
            detail = update.pop("_detail", {})
            duration = (time.perf_counter() - started) * 1000
            update["steps"] = [
                Step(
                    node=name,
                    started_ms=round((started - state["t0"]) * 1000, 1),
                    duration_ms=round(duration, 1),
                    detail=detail,
                )
            ]
            log(_log, "agent step", node=name, duration_ms=round(duration, 1), **detail)
            return update

        return node

    def _build(self) -> Any:
        g: StateGraph[AgentState] = StateGraph(AgentState)
        for name, fn in (
            ("plan", self._plan),
            ("act", self._act),
            ("tools", self._tools),
            ("draft", self._draft),
            ("validate", self._validate),
            ("policy", self._policy),
            ("ground", self._ground),
            ("route", self._route),
        ):
            g.add_node(name, self._timed(name, fn))
        g.add_edge(START, "plan")
        g.add_edge("plan", "act")
        g.add_conditional_edges("act", self._after_act, ["tools", "draft", "route"])
        g.add_conditional_edges("tools", self._after_tools, ["act", "draft"])
        g.add_conditional_edges("draft", self._after_draft, ["validate", "route"])
        g.add_conditional_edges("validate", self._after_validate, ["policy", "draft", "route"])
        g.add_edge("policy", "ground")
        g.add_edge("ground", "route")
        g.add_edge("route", END)
        return g.compile()

    async def run(self, question: str, *, run_id: str | None = None) -> AgentResult:
        run_id = run_id or f"run_{uuid.uuid4().hex[:12]}"
        token = run_id_var.set(run_id)
        t0 = time.perf_counter()
        try:
            with _tracer.start_as_current_span("agent.run") as span:
                span.set_attribute("ga.run_id", run_id)
                span.set_attribute("ga.model", self.model.name)
                state: AgentState = await self._graph.ainvoke(
                    {
                        "question": question,
                        "t0": t0,
                        "deadline": time.monotonic() + self.limits.timeout_s,
                    },
                    config={"recursion_limit": 4 * self.limits.max_model_turns + 16},
                )
                span.set_attribute("ga.disposition", state["disposition"].value)
        finally:
            run_id_var.reset(token)
        return AgentResult(
            run_id=run_id,
            question=question,
            model=self.model.name,
            disposition=state["disposition"],
            finding=state.get("finding"),
            proposals=state.get("proposals", []),
            tool_calls=state.get("tool_calls", []),
            steps=state.get("steps", []),
            checks=state.get("checks") or Checks(),
            fallback=state.get("fallback"),
            stop_reason=state.get("stop_reason"),
            usage=state.get("usage") or Usage(),
            duration_ms=round((time.perf_counter() - t0) * 1000, 1),
        )
