"""Configuration, from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    database_url: str = field(
        default_factory=lambda: _env(
            "DATABASE_URL", "postgresql+asyncpg://agent:agent@127.0.0.1:5432/agent"
        )
    )
    model: str = field(default_factory=lambda: _env("GA_MODEL", "ollama:qwen3:8b"))
    warehouse: Path = field(
        default_factory=lambda: Path(_env("GA_WAREHOUSE", "data/warehouse.duckdb"))
    )
    mcp_url: str | None = field(default_factory=lambda: os.environ.get("GA_MCP_URL") or None)
    max_concurrent_runs: int = field(
        default_factory=lambda: int(_env("GA_MAX_CONCURRENT_RUNS", "2"))
    )
    outbox_worker: bool = field(default_factory=lambda: _env("GA_OUTBOX_WORKER", "1") == "1")
    outbox_poll_s: float = field(default_factory=lambda: float(_env("GA_OUTBOX_POLL_S", "1.0")))
