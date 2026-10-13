"""The read-only boundary between the model and the warehouse.

The model writes SQL; this module decides whether any of it runs. It parses the text,
refuses anything that is not a single query, refuses unknown tables, table functions,
file reads and personal-data columns, and wraps what remains in a hard row limit.
What executes is the re-rendered statement, never the model's original text, so a
comment or a second statement cannot ride along.

The DuckDB connection is also opened read-only with external access disabled: this
check is the first line, not the only one.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

MAX_SQL_CHARS = 5000

_FORBIDDEN = tuple(
    cls
    for name in (
        "Insert",
        "Update",
        "Delete",
        "Merge",
        "Create",
        "Drop",
        "Alter",
        "Command",
        "Copy",
        "Pragma",
        "Set",
        "Use",
        "Attach",
        "Detach",
        "Transaction",
        "Commit",
        "Rollback",
        "Describe",
        "Grant",
        "Revoke",
        "TruncateTable",
        "Analyze",
        "Install",
        "Summarize",
        "Export",
        "Kill",
        "LoadData",
        "Refresh",
        "Cache",
        "Uncache",
    )
    if (cls := getattr(exp, name, None)) is not None
)

_BLOCKED_FUNCTIONS = frozenset(
    {
        "getenv",
        "current_setting",
        "query",
        "query_table",
        "sniff_csv",
        "glob",
        "columns",
        "which_secret",
        "load",
        "install",
        "read_text",
        "read_blob",
        "sql_auto_complete",
    }
)
_BLOCKED_PREFIXES = (
    "read_",
    "duckdb_",
    "pragma_",
    "parquet_",
    "sqlite_",
    "postgres_",
    "mysql_",
    "iceberg_",
    "delta_",
    "http",
)
_BLOCKED_CLASSES = tuple(
    cls
    for name in ("ReadCSV", "ReadParquet", "Columns")
    if (cls := getattr(exp, name, None)) is not None
)


class SqlRejected(ValueError):
    """The statement will not run. The message is written for the model to act on."""


@dataclass(frozen=True)
class GuardPolicy:
    allowed: Mapping[str, frozenset[str]]
    """table -> columns the model may read."""
    blocked: Mapping[str, frozenset[str]]
    """table -> personal-data columns that may never be read, in any form."""
    max_rows: int = 200


@dataclass(frozen=True)
class GuardedQuery:
    sql: str
    """The re-rendered query, comments stripped. This is what is shown and audited."""
    wrapped: str
    """`sql` inside a row cap. This is what executes."""
    tables: frozenset[str] = field(default_factory=frozenset)


def _function_name(node: exp.Func) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.name).lower()
    return type(node).__name__.lower()


def guard(sql: str, policy: GuardPolicy) -> GuardedQuery:
    text = sql.strip().rstrip(";").strip()
    if not text:
        raise SqlRejected("empty query")
    if len(text) > MAX_SQL_CHARS:
        raise SqlRejected(f"query longer than {MAX_SQL_CHARS} characters")
    try:
        statements = [s for s in sqlglot.parse(text, read="duckdb") if s is not None]
    except ParseError as exc:
        raise SqlRejected(f"could not parse the query: {str(exc).splitlines()[0]}") from exc
    if len(statements) != 1:
        raise SqlRejected("exactly one statement is allowed")
    root = statements[0]
    if not isinstance(root, exp.Query):
        raise SqlRejected(f"only SELECT queries are allowed, got {type(root).__name__.upper()}")

    for node in root.walk():
        if isinstance(node, _FORBIDDEN):
            raise SqlRejected(
                f"{type(node).__name__.upper()} is not allowed; queries are read-only"
            )
        if isinstance(node, _BLOCKED_CLASSES):
            raise SqlRejected("table functions and file reads are not allowed")
        if isinstance(node, exp.Func):
            name = _function_name(node)
            if name in _BLOCKED_FUNCTIONS or name.startswith(_BLOCKED_PREFIXES):
                raise SqlRejected(f"function {name}() is not allowed")

    ctes = {str(c.alias_or_name).lower() for c in root.find_all(exp.CTE)}
    # `WITH customers AS (SELECT * FROM customers) SELECT email FROM customers` would hide
    # the real table behind a CTE of the same name, so names may not be reused.
    if shadowed := sorted(ctes & set(policy.allowed)):
        raise SqlRejected(f"a CTE may not reuse the table name {shadowed[0]!r}")
    aliases: dict[str, str] = {}
    tables: set[str] = set()
    for table in root.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            raise SqlRejected("table functions and file reads are not allowed")
        if table.args.get("db") or table.args.get("catalog"):
            raise SqlRejected("schema- or catalog-qualified tables are not allowed")
        name = str(table.name).lower()
        if name in ctes:
            continue
        if name not in policy.allowed:
            raise SqlRejected(
                f"unknown table {table.name!r}; allowed: {', '.join(sorted(policy.allowed))}"
            )
        tables.add(name)
        aliases[str(table.alias_or_name).lower()] = name

    in_scope_blocked = {col for t in tables for col in policy.blocked.get(t, frozenset())}
    for star in root.find_all(exp.Star):
        parent = star.parent
        # Only a projection can expose columns: `SELECT *` or `t.*`. The star inside
        # COUNT(*) reads nothing.
        if not isinstance(parent, exp.Column | exp.Select):
            continue
        qualifier = str(parent.table).lower() if isinstance(parent, exp.Column) else ""
        targets = {aliases[qualifier]} if qualifier in aliases else tables
        exposed = sorted(t for t in targets if policy.blocked.get(t))
        if exposed:
            allowed = ", ".join(sorted(policy.allowed[exposed[0]]))
            raise SqlRejected(
                f"SELECT * would expose personal data in {exposed[0]}; name the columns "
                f"instead (allowed: {allowed})"
            )

    for column in root.find_all(exp.Column):
        if isinstance(column.this, exp.Star):
            continue
        name = str(column.name).lower()
        qualifier = str(column.table).lower()
        if not qualifier and (name in aliases or name in tables):
            raise SqlRejected(f"selecting a whole row as {name!r} is not allowed; name the columns")
        if qualifier in aliases:
            owner = aliases[qualifier]
            if name in policy.blocked.get(owner, frozenset()):
                raise SqlRejected(f"{owner}.{name} is personal data and cannot be queried")
        elif not qualifier and name in in_scope_blocked:
            raise SqlRejected(f"{name} is personal data and cannot be queried")

    rendered = root.sql(dialect="duckdb", comments=False)
    wrapped = f"SELECT * FROM ({rendered}) AS guarded LIMIT {policy.max_rows + 1}"
    return GuardedQuery(sql=rendered, wrapped=wrapped, tables=frozenset(tables))
