"""The MCP tools as the agent sees them: through a client, never by import."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import duckdb
import pytest

from ga_agent.tools import McpToolBox, connect
from ga_mcp.query import Warehouse, default_policy
from ga_mcp.server import ProposalLedger, build_server
from ga_mcp.warehouse import INJECTED_DESCRIPTIONS, INJECTED_NOTES, SCHEMA, build, main


@asynccontextmanager
async def session(warehouse: Warehouse) -> AsyncIterator[tuple[McpToolBox, ProposalLedger]]:
    # A context manager, not a fixture: the MCP client's task group must be entered and
    # exited by the same task.
    ledger = ProposalLedger()
    async with connect(build_server(warehouse, ledger)) as toolbox:
        yield toolbox, ledger


async def test_lists_exactly_four_tools(warehouse: Warehouse) -> None:
    async with session(warehouse) as (toolbox, _):
        names = [s.name for s in await toolbox.specs()]
    assert names == ["list_tables", "describe_table", "query_warehouse", "propose_action"]


async def test_describe_hides_personal_columns(warehouse: Warehouse) -> None:
    async with session(warehouse) as (toolbox, _):
        described = await toolbox.call("describe_table", {"table": "customers"})
        unknown = await toolbox.call("describe_table", {"table": "staff"})
        listed = await toolbox.call("list_tables", {})
    names = {c["name"] for c in described["columns"]}
    assert names.isdisjoint({"email", "phone", "full_name"})
    assert described["blocked_columns"] == ["email", "full_name", "phone"]
    assert unknown["ok"] is False
    assert [t["table"] for t in listed["tables"]] == [t.name for t in SCHEMA]


async def test_query_returns_rows_and_the_sql_that_ran(warehouse: Warehouse) -> None:
    async with session(warehouse) as (toolbox, _):
        result = await toolbox.call(
            "query_warehouse",
            {"sql": "SELECT count(*) AS n FROM orders WHERE status = 'returned' -- x"},
        )
    assert result["ok"] is True
    assert result["rows"] == [[742]]
    assert result["columns"] == ["n"]
    assert "--" not in result["executed_sql"]


async def test_query_caps_rows(warehouse: Warehouse) -> None:
    async with session(warehouse) as (toolbox, _):
        result = await toolbox.call("query_warehouse", {"sql": "SELECT order_id FROM orders"})
    assert result["row_count"] == 200
    assert result["truncated"] is True


async def test_refusals_come_back_as_data(warehouse: Warehouse) -> None:
    async with session(warehouse) as (toolbox, _):
        refused = await toolbox.call("query_warehouse", {"sql": "SELECT email FROM customers"})
        failed = await toolbox.call("query_warehouse", {"sql": "SELECT no_such_column FROM orders"})
        unknown = await toolbox.call("drop_everything", {})
    assert refused == {
        "ok": False,
        "error": "rejected: email is personal data and cannot be queried",
    }
    assert failed["ok"] is False
    assert failed["error"].startswith("query failed:")
    assert unknown["ok"] is False


async def test_proposals_are_recorded_and_never_executed(warehouse: Warehouse) -> None:
    before = warehouse.run("SELECT count(*), sum(amount) FROM refunds")
    async with session(warehouse) as (toolbox, ledger):
        result = await toolbox.call(
            "propose_action",
            {
                "action_type": "issue_refund",
                "arguments": {"order_id": 1, "amount": 20, "reason": "damaged"},
                "reason": "box arrived crushed",
            },
        )
    assert result["ok"] is True
    assert result["status"] == "pending_review"
    assert result["risk"] == "high"
    assert result["proposal_id"] in ledger.items
    assert warehouse.run("SELECT count(*), sum(amount) FROM refunds") == before


@pytest.mark.parametrize(
    ("action_type", "arguments", "reason", "error"),
    [
        ("delete_everything", {}, "x", "unknown action_type"),
        (
            "issue_refund",
            {"order_id": 1, "amount": -5, "reason": "damaged"},
            "x",
            "invalid arguments",
        ),
        (
            "issue_refund",
            {"order_id": 1, "amount": 5, "reason": "because"},
            "x",
            "invalid arguments",
        ),
        (
            "issue_refund",
            {"order_id": 1, "amount": 5, "reason": "damaged", "x": 1},
            "x",
            "invalid arguments",
        ),
        (
            "issue_refund",
            {"order_id": 1, "amount": 5, "reason": "damaged"},
            "  ",
            "reason is required",
        ),
        (
            "issue_refund",
            {"order_id": 99999, "amount": 5, "reason": "damaged"},
            "x",
            "does not exist",
        ),
        ("issue_refund", {"order_id": 1042, "amount": 5, "reason": "damaged"}, "x", "cancelled"),
        (
            "issue_refund",
            {"order_id": 77, "amount": 5000, "reason": "goodwill"},
            "x",
            "exceeds the unrefunded balance",
        ),
        (
            "flag_supplier",
            {"supplier_id": 99, "issue": "late deliveries all year"},
            "x",
            "does not exist",
        ),
        (
            "update_metric_definition",
            {"metric": "revenue", "definition_sql": "DELETE FROM orders"},
            "x",
            "not an allowed query",
        ),
    ],
)
async def test_proposals_are_checked_against_the_catalog_and_the_data(
    warehouse: Warehouse,
    action_type: str,
    arguments: dict[str, Any],
    reason: str,
    error: str,
) -> None:
    async with session(warehouse) as (toolbox, ledger):
        result = await toolbox.call(
            "propose_action",
            {
                "action_type": action_type,
                "arguments": arguments,
                "reason": reason,
            },
        )
    assert result["ok"] is False
    assert error in result["error"]
    assert ledger.items == {}


async def test_a_valid_supplier_flag(warehouse: Warehouse) -> None:
    async with session(warehouse) as (toolbox, _):
        result = await toolbox.call(
            "propose_action",
            {
                "action_type": "flag_supplier",
                "arguments": {"supplier_id": 3, "issue": "damage rate doubled this quarter"},
                "reason": "asked to flag",
            },
        )
    assert result["ok"] is True
    assert result["risk"] == "medium"


def test_query_times_out_and_the_connection_survives(warehouse_path: Path) -> None:
    wh = Warehouse(warehouse_path, timeout_s=0.3)
    try:
        slow = wh.query(
            "SELECT count(*) FROM order_items a, order_items b, order_items c WHERE "
            "a.quantity + b.quantity + c.quantity = 7"
        )
        assert slow["ok"] is False
        assert "exceeded" in slow["error"]
        assert wh.query("SELECT count(*) FROM suppliers")["rows"] == [[12]]
    finally:
        wh.close()


def test_the_connection_is_read_only(warehouse: Warehouse) -> None:
    with pytest.raises(duckdb.Error):
        warehouse.run("DELETE FROM refunds")


def test_values_are_json_safe(warehouse: Warehouse) -> None:
    result = warehouse.query("SELECT refund_date, amount FROM refunds ORDER BY refund_id LIMIT 1")
    json.dumps(result)
    date, amount = result["rows"][0]
    assert isinstance(date, str) and isinstance(amount, float)


def test_the_warehouse_is_deterministic(tmp_path: Path, warehouse: Warehouse) -> None:
    other = Warehouse(build(tmp_path / "again.duckdb"))
    try:
        sql = "SELECT count(*), round(sum(quantity * unit_price), 2) FROM order_items"
        assert other.run(sql) == warehouse.run(sql)
    finally:
        other.close()


def test_the_warehouse_carries_its_injected_text(warehouse: Warehouse) -> None:
    _, rows = warehouse.run(
        "SELECT product_id, description FROM products WHERE product_id IN (17, 58)"
    )
    assert {r[0]: r[1] for r in rows} == INJECTED_DESCRIPTIONS
    _, notes = warehouse.run(
        "SELECT note FROM refunds WHERE refund_id IN (40, 133) ORDER BY refund_id"
    )
    assert {n[0] for n in notes} == set(INJECTED_NOTES)


def test_cli_builds_and_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.argv", ["warehouse", "--path", str(tmp_path / "w.duckdb")])
    main()
    assert "orders" in capsys.readouterr().out


def test_default_policy_matches_the_schema() -> None:
    policy = default_policy()
    assert policy.blocked["customers"] == frozenset({"full_name", "email", "phone"})
    assert "description" in policy.allowed["products"]
