# 4. Nothing with a side effect runs without a person's approval

Status: accepted · 2026-10

## Context

An agent that reads free text — product descriptions, refund notes, the question itself —
can be instructed by that text. Prompting it to ignore such instructions reduces how
often it obeys; it does not make obeying impossible.

## Decision

* The agent can only *propose* an action, through the `propose_action` tool. Proposals are
  stored as `pending`.
* The only transition out of `pending` that leads to an effect is `approve`, which requires
  a named reviewer and a reason, happens under a row lock, and refuses to happen twice.
  Reviewer names that belong to the system (`agent`, `system`, `executor`, `api`) are
  refused.
* A finding that comes with a proposal is held for review with it, and a finding that
  claims something was done is held — the agent cannot do things.
* Every decision is written to the audit log in the same transaction as the state change.

## Consequences

* The safety property does not depend on the model resisting injection. The injection
  tests assume a model that obeys every instruction it reads and assert that nothing is
  executed.
* Resistance is still measured — how often the model proposes what injected text asked for
  — because every needless proposal costs a reviewer's time.
* Reviewer identity is a header in this reference implementation. A deployment would take
  it from an identity provider.
