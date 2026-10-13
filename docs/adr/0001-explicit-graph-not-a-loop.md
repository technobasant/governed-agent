# 1. The agent is an explicit graph, not a loop around the model

Status: accepted · 2026-10

## Context

The simplest agent is a loop: call the model, run whatever tools it asks for, repeat until
it stops. That loop puts every decision that matters — when to stop, what counts as an
answer, what happens to an answer that fails a check — inside the model's reply or inside
ad-hoc `if` statements around it.

Here those decisions are the point. A finding must parse against a schema, pass a policy,
pass a grounding check, and then be routed. Each of those is a step that can fail, and
each failure has a defined next step.

## Decision

* Build the agent as a LangGraph `StateGraph` with named nodes — `plan`, `act`, `tools`,
  `draft`, `validate`, `policy`, `ground`, `route` — and conditional edges that this code
  decides, not the model.
* The model's turns are limited to `act` (choose tools) and `draft` (write the finding as
  JSON against the `Finding` schema). Everything after `draft` is deterministic.
* Bounds are edges: a turn budget, a tool-call budget, a per-run proposal limit and a
  wall-clock deadline each route to `draft` or `route` with a recorded `stop_reason`.
* Every node is timed and recorded as a step, so the run timeline in the UI is the graph's
  actual path.

## Consequences

* The path a run took is data: the UI shows it, the audit log records it, and tests assert
  it node by node.
* LangGraph is a dependency for what is, in the end, a small state machine. The graph is
  compiled without a checkpointer; nothing here relies on LangGraph persistence, so
  replacing it with a hand-written state machine would touch one module.
* A single repair attempt on a malformed draft is an explicit edge, not a retry loop in a
  helper, and it is visible in the timeline.
