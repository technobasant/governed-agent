# Changelog

## 0.1.0 — 2026-10-13

First release.

- LangGraph agent: plan → act ⇄ tools → draft → validate → policy → ground → route, with
  turn, tool-call, proposal and wall-clock budgets and an honest fallback that lists the
  queries tried.
- MCP server over a seeded, synthetic warehouse for a fictional retailer: `list_tables`,
  `describe_table`, `query_warehouse` behind a parser-based read-only SQL guard, and
  `propose_action`, which records and never executes.
- Deterministic grounding check: every number traces to a query result, the question, or
  a declared calculation over grounded operands.
- FastAPI service on PostgreSQL: runs, steps, tool calls, findings, proposed actions, a
  review state machine, an append-only audit log enforced by triggers, and an outbox that
  executes approved actions at most once.
- Next.js review UI: run timeline with every tool call and SQL statement, numbers linked
  to the cells they came from, review queue, audit log.
- Ollama by default (`qwen3:8b`), OpenAI and Anthropic optional, a deterministic demo
  model, and record/replay cassettes for CI.
- Golden set of 54 questions with SQL-computed answer keys, scored and gated with
  evalproof; a live `qwen3:8b` run recorded in MEASUREMENTS.md.
- OpenTelemetry traces from the API, agent and MCP server to Jaeger; JSON logs with run ids.
