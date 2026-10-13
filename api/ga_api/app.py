"""The HTTP API: ask, inspect, review, and the audit trail.

Nothing here lets a caller execute an action directly. The only way to an effect is
`approve`, which records a person's decision and enqueues the action; the outbox worker
does the rest.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ga_agent.graph import Agent, Limits
from ga_agent.llm import ChatModel, model_from_spec
from ga_agent.tools import connect
from ga_api import store
from ga_api.db import make_engine, migrate
from ga_api.outbox import Effects, drain, worker
from ga_api.schemas import (
    ActionOut,
    AskRequest,
    AuditOut,
    Decision,
    DrainResult,
    FindingOut,
    ReviewQueue,
    RunDetail,
    RunSummary,
)
from ga_api.settings import Settings
from ga_core.telemetry import log, run_id_var, setup_logging, setup_tracing

_log = logging.getLogger("ga_api")

RESERVED_ACTORS = frozenset({"agent", "system", "executor", "api"})
_REVIEWER_RE = re.compile(r"[a-z][a-z0-9._-]{1,39}")


@dataclass
class Services:
    engine: AsyncEngine
    agent: Agent
    effects: Effects
    model_name: str
    limiter: asyncio.Semaphore
    tasks: set[asyncio.Task[None]] = field(default_factory=set)


def reviewer(x_reviewer: Annotated[str | None, Header()] = None) -> str:
    """Who is deciding. A header for now; an identity provider in a real deployment."""
    if not x_reviewer or not _REVIEWER_RE.fullmatch(x_reviewer):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "X-Reviewer header is required")
    if x_reviewer in RESERVED_ACTORS:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"{x_reviewer!r} cannot review")
    return x_reviewer


def get_services(request: Request) -> Services:
    svc: Services = request.app.state.services
    return svc


Svc = Annotated[Services, Depends(get_services)]
Reviewer = Annotated[str, Depends(reviewer)]


def create_app(
    settings: Settings | None = None,
    *,
    model: ChatModel | None = None,
    limits: Limits | None = None,
) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = make_engine(settings.database_url)
        await migrate(engine)
        interrupted = await store.recover_interrupted(engine)
        async with AsyncExitStack() as stack:
            if settings.mcp_url:
                target: Any = settings.mcp_url
            else:
                from ga_mcp.query import Warehouse
                from ga_mcp.server import build_server

                target = build_server(Warehouse(settings.warehouse))
            toolbox = await stack.enter_async_context(connect(target))
            chat = model or model_from_spec(settings.model)
            services = Services(
                engine=engine,
                agent=Agent(chat, toolbox, limits),
                effects=Effects(engine),
                model_name=chat.name,
                limiter=asyncio.Semaphore(settings.max_concurrent_runs),
            )
            app.state.services = services
            stop = asyncio.Event()
            loop_task = (
                asyncio.create_task(worker(engine, services.effects, settings.outbox_poll_s, stop))
                if settings.outbox_worker
                else None
            )
            log(
                _log,
                "api started",
                model=chat.name,
                mcp=settings.mcp_url or "in-process",
                interrupted_runs=interrupted,
            )
            try:
                yield
            finally:
                stop.set()
                if loop_task is not None:
                    await loop_task
                for task in list(services.tasks):
                    task.cancel()
                await asyncio.gather(*services.tasks, return_exceptions=True)
        await engine.dispose()

    app = FastAPI(title="governed-agent", version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def access_log(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        started = time.perf_counter()
        response = await call_next(request)
        if request.url.path != "/healthz":
            log(
                _log,
                "request",
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
            )
        return response

    async def execute(svc: Services, run_id: str, question: str) -> None:
        token = run_id_var.set(run_id)
        try:
            async with svc.limiter:
                result = await svc.agent.run(question, run_id=run_id)
            await store.save_result(svc.engine, result)
        except asyncio.CancelledError:
            await store.fail_run(svc.engine, run_id, "cancelled at shutdown")
            raise
        except Exception as exc:
            _log.exception("run failed")
            await store.fail_run(svc.engine, run_id, f"{type(exc).__name__}: {exc}")
        finally:
            run_id_var.reset(token)

    @app.get("/healthz")
    async def healthz(svc: Svc) -> dict[str, Any]:
        async with svc.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"ok": True, "model": svc.model_name}

    @app.post("/runs", status_code=status.HTTP_202_ACCEPTED, response_model=None)
    async def ask(
        body: AskRequest,
        svc: Svc,
        response: Response,
        wait: Annotated[bool, Query()] = False,
    ) -> dict[str, Any]:
        run_id = store.new_run_id()
        await store.create_run(svc.engine, run_id, body.question.strip(), svc.model_name)
        if wait:
            await execute(svc, run_id, body.question.strip())
            response.status_code = status.HTTP_200_OK
            return RunDetail.model_validate(await store.get_run(svc.engine, run_id)).model_dump(
                mode="json"
            )
        task = asyncio.create_task(execute(svc, run_id, body.question.strip()))
        svc.tasks.add(task)
        task.add_done_callback(svc.tasks.discard)
        return {"id": run_id, "status": "running"}

    @app.get("/runs")
    async def runs(svc: Svc, limit: Annotated[int, Query(ge=1, le=200)] = 50) -> list[RunSummary]:
        return [RunSummary.model_validate(r) for r in await store.list_runs(svc.engine, limit)]

    @app.get("/runs/{run_id}")
    async def run(run_id: str, svc: Svc) -> RunDetail:
        try:
            return RunDetail.model_validate(await store.get_run(svc.engine, run_id))
        except store.NotFound as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    @app.get("/review-queue")
    async def review_queue(svc: Svc) -> ReviewQueue:
        return ReviewQueue.model_validate(await store.review_queue(svc.engine))

    @app.get("/actions/{action_id}")
    async def action(action_id: str, svc: Svc) -> ActionOut:
        try:
            return ActionOut.model_validate(await store.get_action(svc.engine, action_id))
        except store.NotFound as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    async def _decide_action(
        svc: Services, action_id: str, approve: bool, who: str, body: Decision
    ) -> ActionOut:
        try:
            await store.decide_action(
                svc.engine, action_id, approve=approve, reviewer=who, reason=body.reason
            )
        except store.NotFound as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        except store.Conflict as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        return ActionOut.model_validate(await store.get_action(svc.engine, action_id))

    @app.post("/actions/{action_id}/approve")
    async def approve(action_id: str, body: Decision, svc: Svc, who: Reviewer) -> ActionOut:
        return await _decide_action(svc, action_id, True, who, body)

    @app.post("/actions/{action_id}/reject")
    async def reject(action_id: str, body: Decision, svc: Svc, who: Reviewer) -> ActionOut:
        return await _decide_action(svc, action_id, False, who, body)

    async def _decide_finding(
        svc: Services, run_id: str, publish: bool, who: str, body: Decision
    ) -> FindingOut:
        try:
            row = await store.decide_finding(
                svc.engine, run_id, publish=publish, reviewer=who, reason=body.reason
            )
        except store.NotFound as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        except store.Conflict as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        return FindingOut.model_validate(row)

    @app.post("/findings/{run_id}/publish")
    async def publish_finding(run_id: str, body: Decision, svc: Svc, who: Reviewer) -> FindingOut:
        return await _decide_finding(svc, run_id, True, who, body)

    @app.post("/findings/{run_id}/reject")
    async def reject_finding(run_id: str, body: Decision, svc: Svc, who: Reviewer) -> FindingOut:
        return await _decide_finding(svc, run_id, False, who, body)

    @app.get("/audit")
    async def audit(svc: Svc, limit: Annotated[int, Query(ge=1, le=1000)] = 200) -> list[AuditOut]:
        return [AuditOut.model_validate(a) for a in await store.list_audit(svc.engine, limit)]

    @app.post("/admin/outbox/drain")
    async def drain_outbox(svc: Svc) -> DrainResult:
        """Run the executor once, now. The background worker does the same every poll."""
        return DrainResult.model_validate(await drain(svc.engine, svc.effects))

    return app


def main() -> None:
    import os

    import uvicorn

    setup_logging()
    setup_tracing("ga-api")
    app = create_app()
    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        FastAPIInstrumentor.instrument_app(app)
        HTTPXClientInstrumentor().instrument()
    uvicorn.run(
        app,
        host=os.environ.get("GA_API_HOST", "127.0.0.1"),
        port=int(os.environ.get("GA_API_PORT", "8000")),
        access_log=False,
        log_config=None,
    )


if __name__ == "__main__":
    main()
