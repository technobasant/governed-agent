"""The service end to end against a real PostgreSQL: runs, review, outbox, audit."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import func, insert, select, text
from sqlalchemy.exc import DBAPIError

from ga_agent.demo_model import DemoModel
from ga_agent.llm import Message, ModelError, ToolSpec
from ga_api import outbox as outbox_module
from ga_api import store
from ga_api.app import Services
from ga_api.db import action_effects, outbox, runs
from ga_api.outbox import drain
from tests.conftest import running_api

pytestmark = pytest.mark.integration

REFUND = "Refund order 1 in full, it arrived damaged."
DANA = {"X-Reviewer": "dana"}


def services(app: Any) -> Services:
    svc: Services = app.state.services
    return svc


async def ask(client: httpx.AsyncClient, question: str) -> dict[str, Any]:
    response = await client.post("/runs", params={"wait": "true"}, json={"question": question})
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def count(app: Any, table: Any) -> int:
    async with services(app).engine.connect() as conn:
        return int((await conn.execute(select(func.count()).select_from(table))).scalar_one())


async def test_a_published_run_and_its_trail(database_url: str, warehouse_path: Path) -> None:
    async with running_api(database_url, warehouse_path, DemoModel()) as (_, client):
        assert (await client.get("/healthz")).json() == {"ok": True, "model": "demo"}
        run = await ask(client, "How many orders were returned?")
        assert run["status"] == "completed"
        assert run["disposition"] == "published"
        assert run["finding"]["state"] == "published"
        assert run["tool_calls"][0]["result"]["rows"] == [[742]]
        assert [s["node"] for s in run["steps"]][-1] == "route"
        assert [e["event"] for e in run["audit"]] == [
            "run.started",
            "tool.called",
            "finding.published",
            "run.completed",
        ]
        listed = (await client.get("/runs")).json()
        assert listed[0]["id"] == run["id"] and listed[0]["tool_calls"] == 1
        assert (await client.get("/runs/run_missing")).status_code == 404


async def test_a_background_run(database_url: str, warehouse_path: Path) -> None:
    async with running_api(database_url, warehouse_path, DemoModel()) as (_, client):
        accepted = await client.post("/runs", json={"question": "Show revenue by channel"})
        assert accepted.status_code == 202
        run_id = accepted.json()["id"]
        for _ in range(100):
            run = (await client.get(f"/runs/{run_id}")).json()
            if run["status"] != "running":
                break
            await asyncio.sleep(0.05)
        assert run["disposition"] == "published"


async def test_approve_executes_exactly_once(database_url: str, warehouse_path: Path) -> None:
    async with running_api(database_url, warehouse_path, DemoModel()) as (app, client):
        run = await ask(client, REFUND)
        assert run["disposition"] == "held_for_review"
        queue = (await client.get("/review-queue")).json()
        assert len(queue["actions"]) == 1 and len(queue["findings"]) == 1
        action_id = queue["actions"][0]["id"]
        assert queue["actions"][0]["arguments"] == {
            "order_id": 1,
            "amount": 610.48,
            "reason": "damaged",
        }

        reason = {"reason": "photo shows the damage"}
        assert (await client.post(f"/actions/{action_id}/approve", json=reason)).status_code == 401
        assert (
            await client.post(
                f"/actions/{action_id}/approve", json=reason, headers={"X-Reviewer": "agent"}
            )
        ).status_code == 403
        assert (
            await client.post(f"/actions/{action_id}/approve", json={"reason": ""}, headers=DANA)
        ).status_code == 422
        assert await count(app, action_effects) == 0  # proposing and queueing never execute

        approved = await client.post(f"/actions/{action_id}/approve", json=reason, headers=DANA)
        assert approved.status_code == 200
        assert approved.json()["state"] == "approved"
        assert approved.json()["outbox_state"] == "queued"
        assert (
            await client.post(f"/actions/{action_id}/approve", json=reason, headers=DANA)
        ).status_code == 409
        assert (
            await client.post(f"/actions/{action_id}/reject", json=reason, headers=DANA)
        ).status_code == 409

        assert (await client.post("/admin/outbox/drain")).json()["executed"] == 1
        assert (await client.post("/admin/outbox/drain")).json()["executed"] == 0
        action = (await client.get(f"/actions/{action_id}")).json()
        assert action["state"] == "executed" and action["effect_id"] is not None
        assert await count(app, action_effects) == 1
        events = [e["event"] for e in (await client.get("/audit")).json()]
        assert events[:2] == ["action.executed", "action.approved"]
        assert (await client.get("/actions/act_missing")).status_code == 404
        assert (
            await client.post("/actions/act_missing/approve", json=reason, headers=DANA)
        ).status_code == 404


async def test_concurrent_approvals_enqueue_once(database_url: str, warehouse_path: Path) -> None:
    async with running_api(database_url, warehouse_path, DemoModel()) as (app, client):
        await ask(client, REFUND)
        action_id = (await client.get("/review-queue")).json()["actions"][0]["id"]
        responses = await asyncio.gather(
            *[
                client.post(
                    f"/actions/{action_id}/approve",
                    json={"reason": "ok by me"},
                    headers={"X-Reviewer": f"reviewer{i}"},
                )
                for i in range(5)
            ]
        )
        assert sorted(r.status_code for r in responses) == [200, 409, 409, 409, 409]
        assert await count(app, outbox) == 1


async def test_reject_never_executes(database_url: str, warehouse_path: Path) -> None:
    async with running_api(database_url, warehouse_path, DemoModel()) as (app, client):
        await ask(client, REFUND)
        action_id = (await client.get("/review-queue")).json()["actions"][0]["id"]
        rejected = await client.post(
            f"/actions/{action_id}/reject", json={"reason": "no evidence of damage"}, headers=DANA
        )
        assert rejected.json()["state"] == "rejected"
        assert (await client.post("/admin/outbox/drain")).json()["executed"] == 0
        assert await count(app, outbox) == 0
        assert await count(app, action_effects) == 0


async def test_a_crash_after_the_effect_does_not_repeat_it(
    database_url: str,
    warehouse_path: Path,
) -> None:
    async with running_api(database_url, warehouse_path, DemoModel()) as (app, client):
        await ask(client, REFUND)
        action_id = (await client.get("/review-queue")).json()["actions"][0]["id"]
        await client.post(f"/actions/{action_id}/approve", json={"reason": "fine"}, headers=DANA)
        svc = services(app)

        async def crash(_: str) -> None:
            raise RuntimeError("worker died after the refund went out")

        first = await drain(svc.engine, svc.effects, after_effect=crash)
        assert first["retried"] == 1
        assert await count(app, action_effects) == 1
        async with svc.engine.begin() as conn:  # skip the backoff
            await conn.execute(text("UPDATE outbox SET available_at = now()"))
        second = await drain(svc.engine, svc.effects)
        assert second == {"executed": 0, "duplicate": 1, "retried": 0, "failed": 0}
        assert await count(app, action_effects) == 1
        assert (await client.get(f"/actions/{action_id}")).json()["state"] == "executed"


async def test_an_action_that_keeps_failing_is_marked_failed(
    database_url: str,
    warehouse_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(outbox_module, "MAX_ATTEMPTS", 1)
    async with running_api(database_url, warehouse_path, DemoModel()) as (app, client):
        await ask(client, REFUND)
        action_id = (await client.get("/review-queue")).json()["actions"][0]["id"]
        await client.post(f"/actions/{action_id}/approve", json={"reason": "fine"}, headers=DANA)

        async def down(*_: Any) -> Any:
            raise ConnectionError("payments system unavailable")

        monkeypatch.setattr(services(app).effects, "apply", down)
        assert (await client.post("/admin/outbox/drain")).json()["failed"] == 1
        action = (await client.get(f"/actions/{action_id}")).json()
        assert action["state"] == "failed"
        assert "payments system unavailable" in action["error"]
        assert action["outbox_state"] == "failed"


async def test_the_background_worker_executes_approved_actions(
    database_url: str,
    warehouse_path: Path,
) -> None:
    async with running_api(
        database_url, warehouse_path, DemoModel(), outbox_worker=True, outbox_poll_s=0.05
    ) as (_, client):
        await ask(client, REFUND)
        action_id = (await client.get("/review-queue")).json()["actions"][0]["id"]
        await client.post(f"/actions/{action_id}/approve", json={"reason": "fine"}, headers=DANA)
        for _ in range(100):
            action = (await client.get(f"/actions/{action_id}")).json()
            if action["state"] == "executed":
                break
            await asyncio.sleep(0.05)
        assert action["state"] == "executed"


async def test_held_findings_are_decided_once(database_url: str, warehouse_path: Path) -> None:
    async with running_api(database_url, warehouse_path, DemoModel()) as (_, client):
        held = await ask(client, REFUND)
        other = await ask(client, "Refund order 2 in full, it arrived damaged.")
        published = await client.post(
            f"/findings/{held['id']}/publish", json={"reason": "figures check out"}, headers=DANA
        )
        assert published.json()["state"] == "published"
        assert (
            await client.post(
                f"/findings/{held['id']}/reject", json={"reason": "late"}, headers=DANA
            )
        ).status_code == 409
        rejected = await client.post(
            f"/findings/{other['id']}/reject", json={"reason": "wrong order"}, headers=DANA
        )
        assert rejected.json()["state"] == "rejected"
        assert (
            await client.post(
                "/findings/run_missing/publish", json={"reason": "x" * 3}, headers=DANA
            )
        ).status_code == 404
        assert (await client.get("/review-queue")).json()["findings"] == []


async def test_the_audit_log_is_append_only(database_url: str, warehouse_path: Path) -> None:
    async with running_api(database_url, warehouse_path, DemoModel()) as (app, client):
        await ask(client, "How many orders were returned?")
        engine = services(app).engine
        for statement in (
            "UPDATE audit_log SET actor = 'someone-else'",
            "DELETE FROM audit_log",
            "TRUNCATE audit_log",
        ):
            with pytest.raises(DBAPIError, match="append-only"):
                async with engine.begin() as conn:
                    await conn.execute(text(statement))
        assert len((await client.get("/audit")).json()) == 4


async def test_a_model_failure_fails_the_run(database_url: str, warehouse_path: Path) -> None:
    class Broken:
        name = "broken"

        async def chat(
            self,
            messages: list[Message],
            tools: list[ToolSpec],
            *,
            schema: dict[str, Any] | None = None,
        ) -> Any:
            raise ModelError("ollama request failed: connection refused")

    async with running_api(database_url, warehouse_path, Broken()) as (_, client):
        run = await ask(client, "How many orders were returned?")
        assert run["status"] == "failed"
        assert "connection refused" in run["error"]
        assert [e["event"] for e in run["audit"]] == ["run.started", "run.failed"]


async def test_interrupted_runs_are_marked_failed(database_url: str, warehouse_path: Path) -> None:
    async with running_api(database_url, warehouse_path, DemoModel()) as (app, client):
        engine = services(app).engine
        async with engine.begin() as conn:
            await conn.execute(
                insert(runs).values(id="run_orphan", question="q?", model="demo", status="running")
            )
        assert await store.recover_interrupted(engine) == 1
        run = (await client.get("/runs/run_orphan")).json()
        assert run["status"] == "failed" and run["error"] == "interrupted by a restart"


async def test_request_validation(database_url: str, warehouse_path: Path) -> None:
    async with running_api(database_url, warehouse_path, DemoModel()) as (_, client):
        assert (await client.post("/runs", json={"question": "?"})).status_code == 422
        assert (await client.post("/runs", json={})).status_code == 422
