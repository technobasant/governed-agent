"""Executes approved actions from the outbox, at most once each.

Approval writes an outbox row in the same transaction as the decision, so an approved
action cannot be lost. The worker claims rows with `FOR UPDATE SKIP LOCKED`, so two
workers never hold the same row. The effect is applied with the row's idempotency key,
and the downstream system refuses a key it has already seen — so a worker that crashes
after the effect but before marking the row done retries into a no-op, not a second
refund.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from ga_api.db import action_effects, outbox, proposed_actions
from ga_api.store import audit
from ga_core.actions import validate_action
from ga_core.telemetry import log, tracer

_log = logging.getLogger("ga_api.outbox")
_tracer = tracer("ga_api")

MAX_ATTEMPTS = 5


@dataclass(frozen=True)
class Applied:
    effect_id: int
    duplicate: bool


class Effects:
    """The adapter to downstream systems. Its own transaction: it is somebody else's system."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    async def apply(
        self, action_id: str, action_type: str, arguments: dict[str, Any], key: str
    ) -> Applied:
        validate_action(action_type, arguments)  # the catalog is checked again at the edge
        async with self.engine.begin() as conn:
            inserted = (
                await conn.execute(
                    pg_insert(action_effects)
                    .values(
                        idempotency_key=key,
                        action_id=action_id,
                        action_type=action_type,
                        arguments=arguments,
                    )
                    .on_conflict_do_nothing(index_elements=["idempotency_key"])
                    .returning(action_effects.c.id)
                )
            ).scalar()
            if inserted is not None:
                return Applied(int(inserted), duplicate=False)
            existing = (
                await conn.execute(
                    select(action_effects.c.id).where(action_effects.c.idempotency_key == key)
                )
            ).scalar_one()
            return Applied(int(existing), duplicate=True)


AfterEffect = Callable[[str], Awaitable[None]]


async def drain(
    engine: AsyncEngine,
    effects: Effects,
    *,
    batch: int = 10,
    after_effect: AfterEffect | None = None,
) -> dict[str, int]:
    """Process what is due in the outbox once. Returns counts by outcome."""
    counts = {"executed": 0, "duplicate": 0, "retried": 0, "failed": 0}
    async with engine.begin() as conn:
        rows = (
            (
                await conn.execute(
                    select(
                        outbox.c.id,
                        outbox.c.action_id,
                        outbox.c.idempotency_key,
                        outbox.c.attempts,
                        proposed_actions.c.action_type,
                        proposed_actions.c.arguments,
                        proposed_actions.c.run_id,
                    )
                    .join(proposed_actions, proposed_actions.c.id == outbox.c.action_id)
                    .where(outbox.c.state == "queued", outbox.c.available_at <= datetime.now(UTC))
                    .order_by(outbox.c.id)
                    .limit(batch)
                    .with_for_update(of=outbox, skip_locked=True)
                )
            )
            .mappings()
            .all()
        )
        for row in rows:
            attempts = row["attempts"] + 1
            with _tracer.start_as_current_span("outbox.execute") as span:
                span.set_attribute("ga.action_id", row["action_id"])
                try:
                    applied = await effects.apply(
                        row["action_id"],
                        row["action_type"],
                        row["arguments"],
                        row["idempotency_key"],
                    )
                    if after_effect is not None:
                        await after_effect(row["action_id"])
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"[:500]
                    final = attempts >= MAX_ATTEMPTS
                    await conn.execute(
                        update(outbox)
                        .where(outbox.c.id == row["id"])
                        .values(
                            attempts=attempts,
                            last_error=error,
                            state="failed" if final else "queued",
                            available_at=datetime.now(UTC) + timedelta(seconds=2**attempts),
                        )
                    )
                    if final:
                        await conn.execute(
                            update(proposed_actions)
                            .where(proposed_actions.c.id == row["action_id"])
                            .values(state="failed", error=error)
                        )
                    await audit(
                        conn,
                        "executor",
                        "action.failed" if final else "action.retry",
                        run_id=row["run_id"],
                        subject=row["action_id"],
                        detail={"attempt": attempts, "error": error},
                    )
                    counts["failed" if final else "retried"] += 1
                    log(
                        _log,
                        "outbox attempt failed",
                        action_id=row["action_id"],
                        attempt=attempts,
                        error=error,
                    )
                    continue
                await conn.execute(
                    update(outbox)
                    .where(outbox.c.id == row["id"])
                    .values(
                        state="done",
                        attempts=attempts,
                        processed_at=datetime.now(UTC),
                    )
                )
                await conn.execute(
                    update(proposed_actions)
                    .where(
                        proposed_actions.c.id == row["action_id"],
                        proposed_actions.c.state == "approved",
                    )
                    .values(state="executed", executed_at=datetime.now(UTC))
                )
                await audit(
                    conn,
                    "executor",
                    "action.executed",
                    run_id=row["run_id"],
                    subject=row["action_id"],
                    detail={
                        "effect_id": applied.effect_id,
                        "duplicate": applied.duplicate,
                        "attempt": attempts,
                    },
                )
                counts["duplicate" if applied.duplicate else "executed"] += 1
    return counts


async def worker(engine: AsyncEngine, effects: Effects, poll_s: float, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await drain(engine, effects)
        except Exception:  # keep the loop alive; the next poll retries
            _log.exception("outbox drain failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=poll_s)
