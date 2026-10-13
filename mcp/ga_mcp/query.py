"""Executes guarded queries against the warehouse, read-only, with a timeout."""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import decimal
import threading
from pathlib import Path
from typing import Any

import duckdb

from ga_mcp.sqlguard import GuardPolicy, SqlRejected, guard
from ga_mcp.warehouse import SCHEMA, TABLES

DEFAULT_TIMEOUT_S = 5.0


def default_policy(max_rows: int = 200) -> GuardPolicy:
    return GuardPolicy(
        allowed={t.name: t.allowed for t in SCHEMA},
        blocked={t.name: t.blocked for t in SCHEMA},
        max_rows=max_rows,
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, float):
        # Twelve significant digits: more than any figure here needs, and identical on
        # every platform, so a recorded run replays byte for byte.
        return float(f"{value:.12g}")
    if isinstance(value, dt.date | dt.datetime):
        return value.isoformat()
    if isinstance(value, int | str | bool) or value is None:
        return value
    return str(value)


class QueryTimeout(RuntimeError):
    pass


class Warehouse:
    """A read-only DuckDB handle. Safe to share across threads; queries are serialised."""

    def __init__(
        self,
        path: Path,
        *,
        policy: GuardPolicy | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.path = path
        self.policy = policy or default_policy()
        self.timeout_s = timeout_s
        self._con = duckdb.connect(
            str(path),
            read_only=True,
            config={
                "enable_external_access": False,
                "autoload_known_extensions": False,
                "autoinstall_known_extensions": False,
                "lock_configuration": True,
                # One thread: the data is small, and floating-point aggregates then come
                # out the same on every run.
                "threads": 1,
            },
        )
        self._lock = threading.Lock()
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self._running: duckdb.DuckDBPyConnection | None = None

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
        self._con.close()

    def list_tables(self) -> list[dict[str, Any]]:
        return [{"table": t.name, "description": t.description} for t in SCHEMA]

    def describe(self, table: str) -> dict[str, Any]:
        spec = TABLES.get(table.lower())
        if spec is None:
            return {"ok": False, "error": f"unknown table {table!r}"}
        return {
            "ok": True,
            "table": spec.name,
            "description": spec.description,
            "columns": [
                {"name": c.name, "type": c.type, "note": c.note} for c in spec.columns if not c.pii
            ],
            "blocked_columns": sorted(spec.blocked),
        }

    def _execute(self, sql: str, params: list[Any] | None = None) -> tuple[list[str], list[Any]]:
        cur = self._con.cursor()
        self._running = cur
        try:
            rel = cur.execute(sql, params or [])
            columns = [d[0] for d in rel.description or []]
            return columns, rel.fetchall()
        finally:
            self._running = None
            cur.close()

    def run(self, sql: str, params: list[Any] | None = None) -> tuple[list[str], list[Any]]:
        """Run trusted, internal SQL (answer keys, validation). Not exposed to the model."""
        with self._lock:
            return self._execute(sql, params)

    def query(self, sql: str) -> dict[str, Any]:
        """The model's entry point: guard, cap, time out, serialise. Never raises."""
        try:
            guarded = guard(sql, self.policy)
        except SqlRejected as exc:
            return {"ok": False, "error": f"rejected: {exc}"}
        with self._lock:
            future = self._pool.submit(self._execute, guarded.wrapped)
            try:
                columns, rows = future.result(timeout=self.timeout_s)
            except concurrent.futures.TimeoutError:
                # A cursor is its own connection: interrupt the one that is running.
                running = self._running
                if running is not None:
                    running.interrupt()
                future.cancel()
                return {
                    "ok": False,
                    "error": f"query exceeded {self.timeout_s:.0f}s and was stopped",
                }
            except duckdb.Error as exc:
                message = str(exc).splitlines()[0][:300]
                return {
                    "ok": False,
                    "error": f"query failed: {message}",
                    "executed_sql": guarded.sql,
                }
        cap = self.policy.max_rows
        truncated = len(rows) > cap
        rows = rows[:cap]
        return {
            "ok": True,
            "executed_sql": guarded.sql,
            "columns": columns,
            "rows": [[_jsonable(v) for v in row] for row in rows],
            "row_count": len(rows),
            "truncated": truncated,
            "max_rows": cap,
        }
