"""Persistence and the review state machine.

A proposed action moves `pending → approved → executed` or `pending → rejected`, and
`approved → failed` if execution keeps failing. A held finding moves `held → published` or
`held → rejected`. Every transition happens under a row lock, writes its audit entry in
the same transaction, and refuses to happen twice.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ga_api.db import (
    action_effects,
    audit_log,
    findings,
    outbox,
    proposed_actions,
    run_steps,
    runs,
    tool_calls,
)
from ga_core.models import AgentResult, Disposition

FINDING_STATE = {
    Disposition.PUBLISHED: "published",
    Disposition.HELD_FOR_REVIEW: "held",
    Disposition.REJECTED: "rejected",
    Disposition.UNANSWERABLE: "unanswerable",
}


class NotFound(LookupError):
    pass


class Conflict(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


async def audit(
    conn: AsyncConnection,
    actor: str,
    event: str,
    *,
    run_id: str | None = None,
    subject: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    await conn.execute(
        insert(audit_log).values(
            actor=actor,
            event=event,
            run_id=run_id,
            subject=subject,
            detail=detail or {},
        )
    )


def new_run_id() -> str:
    return f"run_{uuid.uuid4().hex[:12]}"


async def create_run(engine: AsyncEngine, run_id: str, question: str, model: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            insert(runs).values(
                id=run_id,
                question=question,
                model=model,
                status="running",
            )
        )
        await audit(conn, "api", "run.started", run_id=run_id, detail={"model": model})


async def fail_run(engine: AsyncEngine, run_id: str, error: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            update(runs)
            .where(runs.c.id == run_id)
            .values(
                status="failed",
                error=error[:2000],
                finished_at=_now(),
            )
        )
        await audit(conn, "api", "run.failed", run_id=run_id, detail={"error": error[:500]})


async def recover_interrupted(engine: AsyncEngine) -> int:
    """Runs still `running` at startup were interrupted by a restart; say so."""
    async with engine.begin() as conn:
        rows = (
            (
                await conn.execute(
                    update(runs)
                    .where(runs.c.status == "running")
                    .values(status="failed", error="interrupted by a restart", finished_at=_now())
                    .returning(runs.c.id)
                )
            )
            .scalars()
            .all()
        )
        for run_id in rows:
            await audit(
                conn,
                "api",
                "run.failed",
                run_id=run_id,
                detail={"error": "interrupted by a restart"},
            )
    return len(rows)


async def save_result(engine: AsyncEngine, result: AgentResult) -> None:
    """Persist everything a run produced, in one transaction."""
    run_id = result.run_id
    async with engine.begin() as conn:
        await conn.execute(
            update(runs)
            .where(runs.c.id == run_id)
            .values(
                status="completed",
                disposition=result.disposition.value,
                stop_reason=result.stop_reason,
                fallback=result.fallback,
                checks=result.checks.model_dump(mode="json"),
                usage=result.usage.model_dump(mode="json"),
                duration_ms=result.duration_ms,
                finished_at=_now(),
            )
        )
        if result.steps:
            await conn.execute(
                insert(run_steps),
                [
                    {
                        "run_id": run_id,
                        "seq": i,
                        "node": s.node,
                        "started_ms": s.started_ms,
                        "duration_ms": s.duration_ms,
                        "detail": s.detail,
                    }
                    for i, s in enumerate(result.steps)
                ],
            )
        if result.tool_calls:
            await conn.execute(
                insert(tool_calls),
                [
                    {
                        "run_id": run_id,
                        "call_id": c.id,
                        "seq": i,
                        "tool": c.tool,
                        "arguments": c.arguments,
                        "ok": c.ok,
                        "result": c.result,
                        "error": c.error,
                        "duration_ms": c.duration_ms,
                    }
                    for i, c in enumerate(result.tool_calls)
                ],
            )
        for c in result.tool_calls:
            await audit(
                conn,
                "agent",
                "tool.called",
                run_id=run_id,
                subject=c.id,
                detail={
                    "tool": c.tool,
                    "ok": c.ok,
                    "error": c.error,
                    **(
                        {"sql": (c.result or {}).get("executed_sql")}
                        if c.tool == "query_warehouse"
                        else {}
                    ),
                },
            )
        if result.finding is not None:
            state = FINDING_STATE[result.disposition]
            await conn.execute(
                insert(findings).values(
                    run_id=run_id,
                    status=result.finding.status,
                    answer=result.finding.answer,
                    confidence=result.finding.confidence,
                    numbers=[n.model_dump(mode="json") for n in result.finding.numbers],
                    caveats=result.finding.caveats,
                    state=state,
                )
            )
            await audit(
                conn,
                "system",
                f"finding.{state}",
                run_id=run_id,
                detail={
                    "ungrounded": [n.text for n in result.checks.ungrounded],
                    "policy": result.checks.policy_violations,
                },
            )
        elif result.checks.schema_errors:
            await audit(
                conn,
                "system",
                "finding.rejected",
                run_id=run_id,
                detail={"schema_errors": result.checks.schema_errors},
            )
        for p in result.proposals:
            action_id = f"act_{uuid.uuid4().hex[:12]}"
            await conn.execute(
                insert(proposed_actions).values(
                    id=action_id,
                    run_id=run_id,
                    proposal_id=p.proposal_id,
                    tool_call_id=p.tool_call_id,
                    action_type=p.action_type,
                    arguments=p.arguments,
                    reason=p.reason,
                    risk=p.risk,
                    state="pending",
                )
            )
            await audit(
                conn,
                "agent",
                "action.proposed",
                run_id=run_id,
                subject=action_id,
                detail={"action_type": p.action_type, "arguments": p.arguments, "risk": p.risk},
            )
        await audit(
            conn,
            "system",
            "run.completed",
            run_id=run_id,
            detail={
                "disposition": result.disposition.value,
                "stop_reason": result.stop_reason,
                "duration_ms": result.duration_ms,
            },
        )


async def decide_action(
    engine: AsyncEngine,
    action_id: str,
    *,
    approve: bool,
    reviewer: str,
    reason: str,
) -> dict[str, Any]:
    """Approve (and enqueue for execution) or reject a pending action. Exactly once."""
    async with engine.begin() as conn:
        row = (
            (
                await conn.execute(
                    select(proposed_actions)
                    .where(proposed_actions.c.id == action_id)
                    .with_for_update()
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise NotFound(f"no action {action_id}")
        if row["state"] != "pending":
            raise Conflict(f"action {action_id} is already {row['state']}")
        state = "approved" if approve else "rejected"
        await conn.execute(
            update(proposed_actions)
            .where(proposed_actions.c.id == action_id)
            .values(
                state=state,
                decided_by=reviewer,
                decided_at=_now(),
                decision_reason=reason,
            )
        )
        if approve:
            await conn.execute(
                pg_insert(outbox)
                .values(
                    action_id=action_id,
                    idempotency_key=f"exec:{action_id}",
                )
                .on_conflict_do_nothing(index_elements=["action_id"])
            )
        await audit(
            conn,
            reviewer,
            f"action.{state}",
            run_id=row["run_id"],
            subject=action_id,
            detail={"reason": reason, "action_type": row["action_type"]},
        )
        return {**row, "state": state, "decided_by": reviewer, "decision_reason": reason}


async def decide_finding(
    engine: AsyncEngine,
    run_id: str,
    *,
    publish: bool,
    reviewer: str,
    reason: str,
) -> dict[str, Any]:
    async with engine.begin() as conn:
        row = (
            (
                await conn.execute(
                    select(findings).where(findings.c.run_id == run_id).with_for_update()
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise NotFound(f"no finding for {run_id}")
        if row["state"] != "held":
            raise Conflict(f"finding for {run_id} is {row['state']}, not held for review")
        state = "published" if publish else "rejected"
        await conn.execute(
            update(findings)
            .where(findings.c.run_id == run_id)
            .values(
                state=state,
                decided_by=reviewer,
                decided_at=_now(),
                decision_reason=reason,
            )
        )
        await audit(
            conn,
            reviewer,
            f"finding.{state}",
            run_id=run_id,
            detail={"reason": reason, "by_review": True},
        )
        return {**row, "state": state, "decided_by": reviewer, "decision_reason": reason}


# ---- reads ------------------------------------------------------------------------------


async def list_runs(engine: AsyncEngine, limit: int = 50) -> list[dict[str, Any]]:
    pending = (
        select(func.count())
        .select_from(proposed_actions)
        .where(proposed_actions.c.run_id == runs.c.id, proposed_actions.c.state == "pending")
        .scalar_subquery()
    )
    calls = (
        select(func.count())
        .select_from(tool_calls)
        .where(tool_calls.c.run_id == runs.c.id)
        .scalar_subquery()
    )
    query = (
        select(
            runs.c.id,
            runs.c.question,
            runs.c.model,
            runs.c.status,
            runs.c.disposition,
            runs.c.created_at,
            runs.c.duration_ms,
            findings.c.state.label("finding_state"),
            pending.label("pending_actions"),
            calls.label("tool_calls"),
        )
        .select_from(runs.outerjoin(findings, findings.c.run_id == runs.c.id))
        .order_by(runs.c.created_at.desc())
        .limit(limit)
    )
    async with engine.connect() as conn:
        return [dict(r) for r in (await conn.execute(query)).mappings()]


async def get_run(engine: AsyncEngine, run_id: str) -> dict[str, Any]:
    async with engine.connect() as conn:
        run = (await conn.execute(select(runs).where(runs.c.id == run_id))).mappings().first()
        if run is None:
            raise NotFound(f"no run {run_id}")
        steps = (
            (
                await conn.execute(
                    select(run_steps).where(run_steps.c.run_id == run_id).order_by(run_steps.c.seq)
                )
            )
            .mappings()
            .all()
        )
        calls = (
            (
                await conn.execute(
                    select(tool_calls)
                    .where(tool_calls.c.run_id == run_id)
                    .order_by(tool_calls.c.seq)
                )
            )
            .mappings()
            .all()
        )
        finding = (
            (await conn.execute(select(findings).where(findings.c.run_id == run_id)))
            .mappings()
            .first()
        )
        actions = await _actions(conn, proposed_actions.c.run_id == run_id)
        trail = (
            (
                await conn.execute(
                    select(audit_log).where(audit_log.c.run_id == run_id).order_by(audit_log.c.id)
                )
            )
            .mappings()
            .all()
        )
    return {
        **run,
        "steps": [dict(s) for s in steps],
        "tool_calls": [dict(c) for c in calls],
        "finding": dict(finding) if finding else None,
        "actions": actions,
        "audit": [dict(a) for a in trail],
    }


async def _actions(conn: AsyncConnection, where: Any) -> list[dict[str, Any]]:
    query = (
        select(
            proposed_actions,
            runs.c.question,
            outbox.c.state.label("outbox_state"),
            outbox.c.attempts.label("outbox_attempts"),
            outbox.c.last_error.label("outbox_error"),
            action_effects.c.id.label("effect_id"),
            action_effects.c.applied_at.label("effect_applied_at"),
        )
        .select_from(
            proposed_actions.join(runs, runs.c.id == proposed_actions.c.run_id)
            .outerjoin(outbox, outbox.c.action_id == proposed_actions.c.id)
            .outerjoin(action_effects, action_effects.c.action_id == proposed_actions.c.id)
        )
        .where(where)
        .order_by(proposed_actions.c.created_at)
    )
    return [dict(r) for r in (await conn.execute(query)).mappings()]


async def review_queue(engine: AsyncEngine) -> dict[str, list[dict[str, Any]]]:
    async with engine.connect() as conn:
        actions = await _actions(conn, proposed_actions.c.state == "pending")
        held = (
            (
                await conn.execute(
                    select(findings, runs.c.question, runs.c.checks, runs.c.created_at)
                    .select_from(findings.join(runs, runs.c.id == findings.c.run_id))
                    .where(findings.c.state == "held")
                    .order_by(runs.c.created_at)
                )
            )
            .mappings()
            .all()
        )
    return {"actions": actions, "findings": [dict(f) for f in held]}


async def get_action(engine: AsyncEngine, action_id: str) -> dict[str, Any]:
    async with engine.connect() as conn:
        found = await _actions(conn, proposed_actions.c.id == action_id)
    if not found:
        raise NotFound(f"no action {action_id}")
    return found[0]


async def list_audit(engine: AsyncEngine, limit: int = 200) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        rows = (
            (await conn.execute(select(audit_log).order_by(audit_log.c.id.desc()).limit(limit)))
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]
