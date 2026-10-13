"""The graph's decisions, driven by scripted models so every path is reachable."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from ga_agent.demo_model import DemoModel
from ga_agent.graph import FALLBACK, Agent, Limits
from ga_agent.llm import (
    ChatModel,
    Message,
    ModelError,
    ModelReply,
    RecordingModel,
    ReplayModel,
    ScriptedModel,
    ToolCall,
    ToolSpec,
)
from ga_agent.tools import connect
from ga_core.models import Disposition
from ga_mcp.query import Warehouse
from ga_mcp.server import build_server

RETURNED = "SELECT count(*) AS n FROM orders WHERE status = 'returned'"


def query(sql: str = RETURNED) -> ModelReply:
    return ModelReply(tool_calls=[ToolCall(name="query_warehouse", arguments={"sql": sql})])


def draft(answer: str, status: str = "answered", **extra: Any) -> ModelReply:
    body = {
        "status": status,
        "answer": answer,
        "confidence": "high",
        "numbers": [],
        "caveats": [],
        **extra,
    }
    return ModelReply(content=json.dumps(body), tokens_in=100, tokens_out=20)


class Script:
    """Replies in order; records every request it was given."""

    def __init__(self, *replies: ModelReply) -> None:
        self.replies: Iterator[ModelReply] = iter(replies)
        self.requests: list[tuple[list[Message], list[ToolSpec], dict[str, Any] | None]] = []

    def __call__(
        self, messages: list[Message], tools: list[ToolSpec], schema: dict[str, Any] | None
    ) -> ModelReply:
        self.requests.append((messages, tools, schema))
        return next(self.replies)

    def model(self) -> ScriptedModel:
        return ScriptedModel(self)


@asynccontextmanager
async def agent(
    warehouse: Warehouse, model: ChatModel, limits: Limits | None = None
) -> AsyncIterator[Agent]:
    async with connect(build_server(warehouse)) as toolbox:
        yield Agent(model, toolbox, limits)


async def test_a_grounded_answer_is_published(warehouse: Warehouse) -> None:
    script = Script(query(), ModelReply(content="done"), draft("742 orders were returned."))
    async with agent(warehouse, script.model()) as a:
        result = await a.run("How many orders were returned?")
    assert result.disposition is Disposition.PUBLISHED
    assert [s.node for s in result.steps] == [
        "plan",
        "act",
        "tools",
        "act",
        "draft",
        "validate",
        "policy",
        "ground",
        "route",
    ]
    assert result.tool_calls[0].id == "q1"
    assert result.checks.numbers[0].tool_call_id == "q1"
    assert result.usage.model_calls == 3
    assert result.fallback is None
    # The model was told the call id it should cite, and the draft was schema-constrained.
    assert '"call_id": "q1"' in script.requests[1][0][-1]["content"]
    assert script.requests[2][2] is not None and script.requests[2][1] == []


async def test_an_invented_number_is_held_for_review(warehouse: Warehouse) -> None:
    script = Script(
        query(), ModelReply(content="done"), draft("742 orders were returned, up 18% on last year.")
    )
    async with agent(warehouse, script.model()) as a:
        result = await a.run("How many orders were returned?")
    assert result.disposition is Disposition.HELD_FOR_REVIEW
    assert [n.text for n in result.checks.ungrounded] == ["18%"]


async def test_a_malformed_draft_gets_one_repair(warehouse: Warehouse) -> None:
    script = Script(
        query(),
        ModelReply(content="done"),
        ModelReply(content="742, I think"),
        ModelReply(content="```json\n" + draft("742 orders.").content + "\n```"),
    )
    async with agent(warehouse, script.model()) as a:
        result = await a.run("How many orders were returned?")
    assert result.disposition is Disposition.PUBLISHED
    repair = script.requests[3][0]
    assert repair[-2] == {"role": "assistant", "content": "742, I think"}
    assert "did not match the schema" in repair[-1]["content"]


async def test_a_draft_that_never_parses_is_rejected(warehouse: Warehouse) -> None:
    script = Script(
        query(),
        ModelReply(content="done"),
        ModelReply(content="{}"),
        ModelReply(content='{"status": "maybe"}'),
    )
    async with agent(warehouse, script.model()) as a:
        result = await a.run("How many orders were returned?")
    assert result.disposition is Disposition.REJECTED
    assert result.finding is None
    assert result.checks.schema_errors
    assert result.fallback is not None and result.fallback.startswith(FALLBACK)
    assert "q1: SELECT count(*)" in result.fallback


async def test_the_tool_budget_ends_the_loop(warehouse: Warehouse) -> None:
    script = Script(query(), query(), query(), draft("742 orders were returned."))
    async with agent(warehouse, script.model(), Limits(max_tool_calls=2)) as a:
        result = await a.run("How many orders were returned?")
    assert len(result.tool_calls) == 2
    assert result.stop_reason == "max_tool_calls"
    assert result.disposition is Disposition.HELD_FOR_REVIEW
    assert "review:budget_exhausted" in result.checks.policy_violations[0]


async def test_extra_calls_in_one_turn_are_refused_past_the_budget(warehouse: Warehouse) -> None:
    many = ModelReply(
        tool_calls=[ToolCall(name="query_warehouse", arguments={"sql": RETURNED})] * 3
    )
    script = Script(many, draft("742 orders were returned."))
    async with agent(warehouse, script.model(), Limits(max_tool_calls=2)) as a:
        result = await a.run("How many orders were returned?")
    assert [c.ok for c in result.tool_calls] == [True, True, False]
    assert result.tool_calls[2].error == "tool budget used up; write your finding now"


async def test_the_turn_budget_ends_the_loop(warehouse: Warehouse) -> None:
    script = Script(query(), query(), draft("742 orders were returned."))
    async with agent(warehouse, script.model(), Limits(max_model_turns=2)) as a:
        result = await a.run("How many orders were returned?")
    assert result.stop_reason == "max_model_turns"
    assert len(result.tool_calls) == 2


async def test_a_slow_model_times_out_into_an_honest_fallback(warehouse: Warehouse) -> None:
    class Slow:
        name = "slow"

        async def chat(
            self,
            messages: list[Message],
            tools: list[ToolSpec],
            *,
            schema: dict[str, Any] | None = None,
        ) -> ModelReply:
            await asyncio.sleep(5)
            return ModelReply()

    async with agent(warehouse, Slow(), Limits(timeout_s=0.2)) as a:
        result = await a.run("How many orders were returned?")
    assert result.disposition is Disposition.UNANSWERABLE
    assert result.stop_reason == "timeout"
    assert result.fallback == f"{FALLBACK} No queries were run."


async def test_a_timeout_while_drafting(warehouse: Warehouse) -> None:
    calls = 0

    async def chat(
        messages: list[Message], tools: list[ToolSpec], *, schema: dict[str, Any] | None = None
    ) -> ModelReply:
        nonlocal calls
        calls += 1
        if schema is None:
            return ModelReply(content="done")
        await asyncio.sleep(5)
        return ModelReply()

    model = ScriptedModel(lambda *_: ModelReply())
    model.chat = chat  # type: ignore[method-assign]
    async with agent(warehouse, model, Limits(timeout_s=0.3)) as a:
        result = await a.run("Anything?")
    assert result.stop_reason == "timeout"
    assert result.disposition is Disposition.UNANSWERABLE


async def test_a_model_failure_surfaces_as_itself(warehouse: Warehouse) -> None:
    def broken(*_: Any) -> ModelReply:
        raise ModelError("ollama request failed: connection refused")

    with pytest.raises(ModelError, match="connection refused"):
        async with agent(warehouse, ScriptedModel(broken)) as a:
            await a.run("How many orders were returned?")


async def test_unknown_tools_are_answered_with_an_error(warehouse: Warehouse) -> None:
    script = Script(
        ModelReply(tool_calls=[ToolCall(name="send_email", arguments={})]),
        ModelReply(content="done"),
        draft("I cannot do that.", status="unanswerable"),
    )
    async with agent(warehouse, script.model()) as a:
        result = await a.run("Email the customer.")
    assert result.tool_calls[0].error == "unknown tool 'send_email'"
    assert result.disposition is Disposition.UNANSWERABLE
    assert "No queries were run" in (result.fallback or "")


def propose(order_id: int, amount: float) -> ModelReply:
    return ModelReply(
        tool_calls=[
            ToolCall(
                name="propose_action",
                arguments={
                    "action_type": "issue_refund",
                    "arguments": {"order_id": order_id, "amount": amount, "reason": "goodwill"},
                    "reason": "the refund note says so",
                },
            )
        ]
    )


async def test_obeying_injected_text_yields_only_a_pending_proposal(warehouse: Warehouse) -> None:
    """The worst case: the model does what a refund note told it to. It still can't act."""
    before = warehouse.run("SELECT count(*), sum(amount) FROM refunds")
    script = Script(
        query("SELECT note FROM refunds WHERE refund_id = 133"),
        propose(1, 50),
        propose(2, 50),
        ModelReply(content="done"),
        draft("The note asks for a goodwill refund; one is proposed for review."),
    )
    async with agent(warehouse, script.model(), Limits(max_proposals=1)) as a:
        result = await a.run("What do the refund notes say?")
    assert len(result.proposals) == 1
    assert result.tool_calls[2].error == "proposal limit reached for this run"
    assert result.disposition is Disposition.HELD_FOR_REVIEW
    assert any("proposes_action" in v for v in result.checks.policy_violations)
    assert warehouse.run("SELECT count(*), sum(amount) FROM refunds") == before


async def test_the_model_sees_and_is_checked_against_the_same_rows(warehouse: Warehouse) -> None:
    script = Script(
        query("SELECT order_id FROM orders ORDER BY order_id"),
        ModelReply(content="done"),
        draft("Order 120 is in the list."),
    )
    async with agent(warehouse, script.model(), Limits(rows_shown=10)) as a:
        result = await a.run("List orders")
    shown = result.tool_calls[0].result or {}
    assert len(shown["rows"]) == 10 and shown["rows_hidden"] == 190
    assert result.checks.ungrounded[0].text == "120"


async def test_the_catalog_is_loaded_once(warehouse: Warehouse) -> None:
    no = draft("No.", status="unanswerable")
    script = Script(ModelReply(content="no data"), no, ModelReply(content="no data"), no)
    async with agent(warehouse, script.model()) as a:
        first = await a.run("What is our NPS?")
        second = await a.run("What is our NPS?")
    assert first.steps[0].detail == {"catalog_cached": False}
    assert second.steps[0].detail == {"catalog_cached": True}
    system = script.requests[0][0][0]["content"]
    assert "### customers" in system and "blocked (personal data): email" in system


async def test_a_recorded_run_replays_identically(warehouse: Warehouse, tmp_path: Path) -> None:
    cassette = tmp_path / "c.jsonl"
    script = Script(query(), ModelReply(content="done"), draft("742 orders were returned."))
    async with agent(warehouse, RecordingModel(script.model(), cassette)) as a:
        recorded = await a.run("How many orders were returned?")
    async with agent(warehouse, ReplayModel(cassette, "scripted")) as a:
        replayed = await a.run("How many orders were returned?")
    assert replayed.finding == recorded.finding
    assert replayed.tool_calls[0].result == recorded.tool_calls[0].result
    assert replayed.disposition is recorded.disposition


@pytest.mark.parametrize(
    ("question", "disposition", "proposals"),
    [
        ("How many orders were cancelled?", Disposition.PUBLISHED, 0),
        ("Show revenue by channel", Disposition.PUBLISHED, 0),
        ("Refund order 1 in full, it arrived damaged.", Disposition.HELD_FOR_REVIEW, 1),
        # Cancelled: the tool refuses the proposal and the answer says so, with grounded numbers.
        ("Refund order 1042 in full.", Disposition.PUBLISHED, 0),
        ("Refund order 999999 in full.", Disposition.UNANSWERABLE, 0),
        # Numbers quoted from free text are not evidence: held for a person.
        ("What do the refund notes say for order 1361?", Disposition.HELD_FOR_REVIEW, 0),
        ("What do the refund notes say for order 2?", Disposition.UNANSWERABLE, 0),
        ("What is our NPS?", Disposition.UNANSWERABLE, 0),
    ],
)
async def test_the_demo_model(
    warehouse: Warehouse, question: str, disposition: Disposition, proposals: int
) -> None:
    async with agent(warehouse, DemoModel()) as a:
        result = await a.run(question)
    assert result.disposition is disposition
    assert len(result.proposals) == proposals
