"""Ask one question from the command line and print the full result as JSON.

uv run python -m ga_agent "How many orders were returned?" --model ollama:qwen3:8b
"""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from ga_agent.graph import Agent
from ga_agent.llm import model_from_spec
from ga_agent.tools import connect
from ga_core.telemetry import setup_logging, setup_tracing


async def _main(args: argparse.Namespace) -> None:
    if args.mcp_url:
        target: object = args.mcp_url
    else:
        from ga_mcp.query import Warehouse
        from ga_mcp.server import build_server

        target = build_server(Warehouse(args.warehouse))
    async with connect(target) as toolbox:  # type: ignore[arg-type]
        agent = Agent(model_from_spec(args.model), toolbox)
        result = await agent.run(args.question)
    print(result.model_dump_json(indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("question")
    parser.add_argument("--model", default=os.environ.get("GA_MODEL", "ollama:qwen3:8b"))
    parser.add_argument(
        "--warehouse",
        type=Path,
        default=Path(os.environ.get("GA_WAREHOUSE", "data/warehouse.duckdb")),
    )
    parser.add_argument("--mcp-url", default=os.environ.get("GA_MCP_URL"))
    args = parser.parse_args()
    setup_logging(os.environ.get("LOG_LEVEL", "WARNING"))
    setup_tracing("ga-agent")
    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
