# 2. MCP is the boundary between the agent and the data

Status: accepted · 2026-10

## Context

An agent that imports the database client can do anything the client can. A read-only
promise kept by prompt or by convention is not a boundary.

## Decision

* The warehouse is reachable only through an MCP server exposing four tools:
  `list_tables`, `describe_table`, `query_warehouse` and `propose_action`. The agent is an
  MCP client and never imports the warehouse module.
* `query_warehouse` runs only what the SQL guard accepts: one statement, a query, known
  tables, no table functions or file reads, no personal-data columns in any position, no
  `SELECT *` over a table that has them, re-rendered without comments and wrapped in a row
  cap. The DuckDB connection is also opened read-only with external access, extension
  loading and configuration changes disabled — the guard is the first line, not the only
  one.
* `propose_action` validates a proposal against a typed catalog and against the data (the
  order exists, is not cancelled, the amount is within the unrefunded balance) and records
  it. It has no code path that executes anything.
* Tools never raise; a refusal is returned as `{"ok": false, "error": ...}` written for the
  model to act on.
* The same server runs in process for tests and as its own service over streamable HTTP in
  Docker Compose, with DNS-rebinding protection on an explicit host allow-list.

## Consequences

* The agent's capabilities are the server's tool list. Adding a capability is a reviewed
  change to the server, not a prompt edit.
* Every query the model wrote, the SQL that actually ran, and the rows it returned are
  recorded per tool call and shown to the reviewer.
* The guard is a parser-based allow-list, so it refuses some harmless queries (for
  example `SELECT * EXCLUDE (...)` over the customers table). The refusal message tells
  the model what to do instead.
