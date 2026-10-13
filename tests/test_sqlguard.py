"""The read-only boundary. Every case here is a way a model, or text it read, might try
to get more out of the warehouse than a single read of non-personal columns."""

from __future__ import annotations

import pytest

from ga_mcp.query import default_policy
from ga_mcp.sqlguard import MAX_SQL_CHARS, SqlRejected, guard

POLICY = default_policy()


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT count(*) FROM orders WHERE status = 'returned'",
        "SELECT o.channel, sum(i.quantity) FROM orders o JOIN order_items i USING (order_id) "
        "GROUP BY 1",
        "WITH r AS (SELECT order_id, amount FROM refunds) SELECT sum(amount) FROM r",
        "SELECT customer_id, city FROM customers WHERE segment = 'trade'",
        "SELECT count(*) FROM customers",
        "SELECT count(DISTINCT customer_id) FROM orders",
        "SELECT o.* FROM orders o",
        "SELECT * FROM suppliers",
        "SELECT name, rank() OVER (ORDER BY rating DESC) FROM suppliers",
        "SELECT order_id FROM orders WHERE customer_id IN (SELECT customer_id FROM customers "
        "WHERE country = 'GB')",
        "FROM orders SELECT order_id",
        "select ORDER_ID from ORDERS;",
        "SELECT city FROM customers UNION SELECT country FROM suppliers",
    ],
)
def test_allows_plain_reads(sql: str) -> None:
    guarded = guard(sql, POLICY)
    assert guarded.wrapped.endswith(f"LIMIT {POLICY.max_rows + 1}")
    assert guarded.wrapped.startswith("SELECT * FROM (")


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        ("INSERT INTO refunds VALUES (1, 1, DATE '2026-01-01', 10, 'late', 'x')", "only SELECT"),
        ("UPDATE orders SET status = 'returned'", "only SELECT"),
        ("DELETE FROM refunds", "only SELECT"),
        ("DROP TABLE orders", "only SELECT"),
        ("CREATE TABLE x AS SELECT * FROM orders", "only SELECT"),
        ("ALTER TABLE orders ADD COLUMN x INT", "only SELECT"),
        ("COPY orders TO '/tmp/orders.csv'", "only SELECT"),
        ("ATTACH '/tmp/other.duckdb' AS other", "only SELECT"),
        ("PRAGMA database_list", "only SELECT"),
        ("SET threads = 64", "only SELECT"),
        ("INSTALL httpfs", "only SELECT"),
        ("LOAD httpfs", "only SELECT"),
        ("DESCRIBE customers", "only SELECT"),
        ("SHOW TABLES", "only SELECT"),
        ("SUMMARIZE customers", "only SELECT"),
        ("EXPORT DATABASE '/tmp/dump'", "parse|only SELECT"),
        ("WITH x AS (DELETE FROM orders RETURNING *) SELECT * FROM x", "DELETE is not allowed"),
    ],
)
def test_refuses_anything_but_a_query(sql: str, reason: str) -> None:
    with pytest.raises(SqlRejected, match=reason):
        guard(sql, POLICY)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1 FROM orders; DROP TABLE orders",
        "SELECT 1 FROM orders; -- harmless\nDELETE FROM refunds",
        "SELECT 1 FROM orders;SELECT 2 FROM orders",
    ],
)
def test_refuses_more_than_one_statement(sql: str) -> None:
    with pytest.raises(SqlRejected, match="exactly one statement"):
        guard(sql, POLICY)


def test_comments_never_reach_the_database() -> None:
    guarded = guard("SELECT 1 FROM orders /*; DROP TABLE orders */ -- and more", POLICY)
    assert "DROP" not in guarded.wrapped
    assert "--" not in guarded.wrapped
    assert "/*" not in guarded.wrapped


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT email FROM customers",
        "SELECT c.email FROM customers c",
        'SELECT c."EMAIL" FROM customers c',
        "SELECT lower(full_name) FROM customers",
        "SELECT md5(phone) FROM customers",
        "SELECT full_name FROM orders JOIN customers USING (customer_id)",
        "SELECT city FROM customers UNION SELECT email FROM customers",
        "SELECT x.e FROM (SELECT email AS e FROM customers) x",
        "WITH c AS (SELECT email FROM customers) SELECT * FROM c",
        "SELECT order_id FROM orders WHERE customer_id IN "
        "(SELECT customer_id FROM customers WHERE email LIKE '%@example.test')",
    ],
)
def test_refuses_personal_data_in_any_position(sql: str) -> None:
    with pytest.raises(SqlRejected, match="personal data"):
        guard(sql, POLICY)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM customers",
        "SELECT c.* FROM customers c",
        "SELECT * FROM orders o, customers c",
        "SELECT * EXCLUDE (email, phone, full_name) FROM customers",
        "SELECT email FROM (SELECT * FROM customers)",
        "WITH c AS (SELECT * FROM customers) SELECT * FROM c",
        "SELECT * FROM (customers)",
        "FROM customers",
    ],
)
def test_refuses_star_over_personal_data(sql: str) -> None:
    with pytest.raises(SqlRejected, match="SELECT \\* would expose personal data"):
        guard(sql, POLICY)


@pytest.mark.parametrize(
    "sql",
    ["SELECT c FROM customers c", "SELECT to_json(customers) FROM customers"],
)
def test_refuses_whole_row_references(sql: str) -> None:
    with pytest.raises(SqlRejected, match="whole row"):
        guard(sql, POLICY)


def test_refuses_a_cte_that_shadows_a_table() -> None:
    with pytest.raises(SqlRejected, match="may not reuse the table name 'customers'"):
        guard("WITH customers AS (SELECT * FROM customers) SELECT email FROM customers", POLICY)


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        ("SELECT * FROM read_csv('/etc/passwd')", "table functions"),
        ("SELECT * FROM read_parquet('s3://bucket/x.parquet')", "table functions|read_"),
        ("SELECT * FROM '/etc/passwd'", "unknown table"),
        ("SELECT * FROM duckdb_settings()", "duckdb_settings|table functions"),
        ("SELECT * FROM duckdb_tables", "unknown table"),
        ("SELECT getenv('HOME')", "getenv"),
        ("SELECT current_setting('threads')", "current_setting"),
        ("SELECT read_text('/etc/hosts')", "read_text|table functions"),
        ("SELECT * FROM query('SELECT email FROM customers')", r"query\(\)|table functions"),
        ("SELECT * FROM information_schema.tables", "qualified"),
        ("SELECT order_id FROM main.orders", "qualified"),
        ("SELECT * FROM sqlite_master", "unknown table"),
        ("SELECT * FROM staff", "unknown table 'staff'"),
    ],
)
def test_refuses_files_settings_and_unknown_tables(sql: str, reason: str) -> None:
    with pytest.raises(SqlRejected, match=reason):
        guard(sql, POLICY)


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        ("", "empty"),
        ("   ;  ", "empty"),
        ("SELEC order_id FRM orders", "parse|only SELECT"),
        ("SELECT " + "1, " * MAX_SQL_CHARS + "1 FROM orders", "longer than"),
    ],
)
def test_refuses_malformed_input(sql: str, reason: str) -> None:
    with pytest.raises(SqlRejected, match=reason):
        guard(sql, POLICY)


def test_a_missing_limit_is_capped_and_a_large_one_is_overridden() -> None:
    assert guard("SELECT order_id FROM orders", POLICY).wrapped.endswith("LIMIT 201")
    big = guard("SELECT order_id FROM orders LIMIT 1000000", POLICY)
    assert big.wrapped.endswith("LIMIT 201")
    assert "LIMIT 1000000" in big.sql


def test_reports_the_tables_it_read() -> None:
    guarded = guard(
        "WITH r AS (SELECT order_id FROM refunds) SELECT count(*) FROM orders JOIN r USING "
        "(order_id)",
        POLICY,
    )
    assert guarded.tables == frozenset({"orders", "refunds"})
