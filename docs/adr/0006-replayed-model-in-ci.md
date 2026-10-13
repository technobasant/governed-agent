# 6. CI replays a recorded model instead of calling one

Status: accepted · 2026-10

## Context

The golden set should run on every change, but CI has no GPU and a hosted model makes the
suite slow, costly and non-deterministic. A fake model tests the plumbing but says nothing
about how a real model behaves inside it.

## Decision

* Run the golden set against a real model (`qwen3:8b` under Ollama, temperature 0, seed 0)
  and record every model exchange to a JSONL cassette keyed by a hash of the exact request:
  model, messages, tool schemas and output schema.
* CI replays the cassette. Tool calls still execute against the real warehouse through the
  real MCP server and the real checks; only the model's words are recorded.
* A request with no recorded reply is an error, never a fallback. If a prompt, a tool
  description, the catalog or a tool result changes, the model would see something new,
  the replay fails, and the gate fails until the cassette is re-recorded with `make
  measure`.
* Query results are made byte-stable for this: one DuckDB thread, and floats rendered to
  twelve significant digits.
* The evalproof gate compares the replayed run to the committed baseline on paired cases.

## Consequences

* CI checks that the code still turns the recorded model behaviour into the same outcomes:
  same SQL guard verdicts, same grounding verdicts, same routing.
* CI does not detect a model getting better or worse; that needs a live run, which is what
  MEASUREMENTS.md records.
* Re-recording is a deliberate step with a diff to review, like updating a snapshot test.
