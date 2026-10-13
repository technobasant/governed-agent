from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from ga_agent.graph import Limits
from ga_agent.llm import ChatModel
from ga_mcp.query import Warehouse
from ga_mcp.warehouse import build


@pytest.fixture(scope="session")
def warehouse_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build(tmp_path_factory.mktemp("wh") / "warehouse.duckdb")


@pytest.fixture(scope="session")
def warehouse(warehouse_path: Path) -> Iterator[Warehouse]:
    wh = Warehouse(warehouse_path)
    yield wh
    wh.close()


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    """CI provides a PostgreSQL service; locally a throwaway container is started."""
    url = os.environ.get("GA_TEST_DATABASE_URL")
    if url:
        yield url
        return
    try:
        from testcontainers.postgres import PostgresContainer

        container = PostgresContainer("postgres:17-alpine", driver="asyncpg")
        container.start()
    except Exception as exc:  # no Docker: skip, don't fail
        pytest.skip(f"PostgreSQL unavailable: {exc}")
    try:
        yield container.get_connection_url()
    finally:
        container.stop()


async def reset_database(url: str) -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await engine.dispose()


@asynccontextmanager
async def running_api(
    database_url: str,
    warehouse_path: Path,
    model: ChatModel,
    limits: Limits | None = None,
    **settings: Any,
) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient]]:
    from ga_api.app import create_app
    from ga_api.settings import Settings

    await reset_database(database_url)
    config = Settings(
        database_url=database_url,
        model=model.name,
        warehouse=warehouse_path,
        mcp_url=None,
        outbox_worker=settings.pop("outbox_worker", False),
        **settings,
    )
    app = create_app(config, model=model, limits=limits)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
            yield app, client
