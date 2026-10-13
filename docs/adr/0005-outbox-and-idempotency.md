# 5. Approved actions go through an outbox and execute at most once

Status: accepted · 2026-10

## Context

Approval and execution are two steps in two systems: this database, and whatever system
the action changes. Executing inside the approval request couples the reviewer's click to
the downstream system's availability and makes "approve twice" or "retry after a crash" a
double refund.

## Decision

* `approve` writes the decision and an outbox row in one transaction. The outbox has a
  unique constraint on the action, so an action is enqueued once.
* A worker claims due rows with `SELECT … FOR UPDATE SKIP LOCKED`, so concurrent workers
  never hold the same row.
* Each row carries an idempotency key. The effect is applied with that key, and the
  downstream system (here, the `action_effects` table) refuses a key it has seen —
  `INSERT … ON CONFLICT DO NOTHING`. A worker that dies after the effect and before marking
  the row done retries into a recognised duplicate, not a second effect.
* Failures back off exponentially and stop after five attempts; the action is marked
  `failed` with the error.

## Consequences

* "Exactly once" is not claimed. The effect happens at most once per key; the bookkeeping
  converges after retries.
* The downstream adapter must honour the idempotency key. Here it does by construction; a
  real payments API would need to support one, and most do.
* Execution is asynchronous to approval. The UI polls until the action is `executed` or
  `failed`.
