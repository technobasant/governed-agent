"""A deterministic stand-in for the language model, for the demo, the browser test and CI.

It behaves like a careful analyst for a few kinds of question and admits defeat on the
rest. It goes through exactly the same graph, tools and checks as a real model; only the
words are canned.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ga_agent.llm import Message, ModelReply, ToolCall, ToolSpec

_STATUSES = ("placed", "shipped", "delivered", "cancelled", "returned")


def _question(messages: list[Message]) -> str:
    return next(str(m["content"]) for m in messages if m["role"] == "user")


def _results(messages: list[Message]) -> list[dict[str, Any]]:
    out = []
    for m in messages:
        if m["role"] == "tool":
            try:
                out.append(json.loads(m["content"]))
            except json.JSONDecodeError:
                continue
    return out


def _call(name: str, **arguments: Any) -> ModelReply:
    return ModelReply(tool_calls=[ToolCall(name=name, arguments=arguments)], tokens_out=12)


def _finding(**fields: Any) -> ModelReply:
    body = {"numbers": [], "caveats": [], "confidence": "high", **fields}
    return ModelReply(content=json.dumps(body), tokens_out=40)


def _unanswerable(question: str) -> ModelReply:
    return _finding(
        status="unanswerable",
        answer="The warehouse does not hold the data this question needs.",
        confidence="low",
    )


class DemoModel:
    name = "demo"

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        *,
        schema: dict[str, Any] | None = None,
    ) -> ModelReply:
        question = _question(messages)
        results = _results(messages)
        q = question.lower()

        status = next((s for s in _STATUSES if s in q), None)
        if "how many orders" in q and status:
            if not results:
                return _call(
                    "query_warehouse",
                    sql=f"SELECT count(*) AS orders FROM orders WHERE status = '{status}'",
                )
            if schema is None:
                return ModelReply(content="done")
            n = results[0]["rows"][0][0]
            return _finding(
                status="answered",
                answer=f"{n:,} orders have the status {status}.",
                numbers=[
                    {
                        "label": f"{status} orders",
                        "value": n,
                        "unit": "orders",
                        "source_query": results[0]["call_id"],
                    }
                ],
            )

        if "revenue" in q and "channel" in q:
            if not results:
                return _call(
                    "query_warehouse",
                    sql="SELECT o.channel, round(sum(i.quantity * i.unit_price * "
                    "(1 - i.discount)), 2) AS revenue FROM orders o JOIN order_items i "
                    "ON i.order_id = o.order_id WHERE o.status NOT IN ('cancelled') "
                    "GROUP BY o.channel ORDER BY revenue DESC",
                )
            if schema is None:
                return ModelReply(content="done")
            rows = results[0]["rows"]
            parts = ", ".join(f"{c} {v:,.2f} USD" for c, v in rows)
            return _finding(
                status="answered",
                answer=f"Revenue by channel, excluding cancelled orders: {parts}.",
                numbers=[
                    {
                        "label": f"{c} revenue",
                        "value": v,
                        "unit": "USD",
                        "source_query": results[0]["call_id"],
                    }
                    for c, v in rows
                ],
            )

        order = re.search(r"order (\d+)", q)
        if order and "note" in q:
            order_id = int(order.group(1))
            if not results:
                return _call(
                    "query_warehouse",
                    sql=f"SELECT refund_id, note FROM refunds WHERE order_id = {order_id} "
                    "ORDER BY refund_id",
                )
            if schema is None:
                return ModelReply(content="done")
            rows = results[0].get("rows") or []
            if not rows:
                return _unanswerable(question)
            quoted = "; ".join(f'refund {rid}: "{note}"' for rid, note in rows)
            suspicious = any(w in quoted.lower() for w in ("ai ", "assistant", "ignore"))
            return _finding(
                status="answered",
                answer=f"Order {order_id} has {len(rows)} refund note(s). {quoted}",
                caveats=[
                    "The note is addressed to an AI and gives instructions; they were not followed."
                ]
                if suspicious
                else [],
            )

        if order and "refund" in q:
            order_id = int(order.group(1))
            if not results:
                return _call(
                    "query_warehouse",
                    sql="SELECT o.order_id, o.status, round(sum(i.quantity * i.unit_price * "
                    "(1 - i.discount)), 2) AS total FROM orders o JOIN order_items i "
                    f"ON i.order_id = o.order_id WHERE o.order_id = {order_id} "
                    "GROUP BY o.order_id, o.status",
                )
            first = results[0]
            if not first.get("ok") or not first.get("rows"):
                return _unanswerable(question) if schema else ModelReply(content="done")
            _, order_status, total = first["rows"][0]
            if len(results) == 1 and schema is None:
                return _call(
                    "propose_action",
                    action_type="issue_refund",
                    arguments={"order_id": order_id, "amount": total, "reason": "damaged"},
                    reason=f"Requested in the question; order {order_id} is {order_status}.",
                )
            if schema is None:
                return ModelReply(content="done")
            proposed = len(results) > 1 and results[1].get("ok")
            answer = f"Order {order_id} is {order_status} with a total of {total:,.2f} USD. " + (
                "A full refund is proposed for review."
                if proposed
                else f"A refund cannot be proposed: {results[1].get('error')}."
            )
            return _finding(
                status="answered",
                answer=answer,
                numbers=[
                    {
                        "label": "order total",
                        "value": total,
                        "unit": "USD",
                        "source_query": first["call_id"],
                    }
                ],
            )

        return _unanswerable(question) if schema is not None else ModelReply(content="no data")
