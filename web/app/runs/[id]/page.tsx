import Link from "next/link";
import { notFound } from "next/navigation";

import { AutoRefresh } from "@/components/AutoRefresh";
import { Badge } from "@/components/Badge";
import { Decision } from "@/components/Decision";
import { ResultTable } from "@/components/ResultTable";
import { getRun, NotFoundError } from "@/lib/api";
import { cellId, describeArguments, formatMs, formatTime } from "@/lib/format";
import type { Action, NumberCheck, RunDetail } from "@/lib/types";

export const dynamic = "force-dynamic";

async function load(id: string): Promise<RunDetail> {
  try {
    return await getRun(id);
  } catch (error) {
    if (error instanceof NotFoundError) notFound();
    throw error;
  }
}

function anchor(check: NumberCheck): string | null {
  if (check.tool_call_id && check.column && check.row !== null && check.row >= 0) {
    return `#${cellId(check.tool_call_id, check.row, check.column)}`;
  }
  return check.tool_call_id ? `#${check.tool_call_id}` : null;
}

function ActionCard({ action }: { action: Action }) {
  return (
    <article className="card" aria-labelledby={`${action.id}-title`}>
      <header className="card-head">
        <h3 id={`${action.id}-title`}>
          <code>{action.action_type}</code>
        </h3>
        <Badge value={action.state} />
        <span className={`risk risk-${action.risk}`}>{action.risk} risk</span>
      </header>
      <p>{describeArguments(action.arguments)}</p>
      <p className="muted">
        Proposed by the agent in <a href={`#${action.tool_call_id}`}>{action.tool_call_id}</a>:{" "}
        {action.reason}
      </p>
      {action.decided_by ? (
        <p>
          {action.state === "rejected" ? "Rejected" : "Approved"} by <b>{action.decided_by}</b>{" "}
          at {formatTime(action.decided_at)}: “{action.decision_reason}”
        </p>
      ) : null}
      {action.state === "pending" ? (
        <Decision
          endpoint={`/api/actions/${action.id}`}
          approveVerb="approve"
          approveLabel="Approve and execute"
          subject={action.action_type}
        />
      ) : null}
      {action.outbox_state ? (
        <p className="muted">
          Outbox: {action.outbox_state}, {action.outbox_attempts ?? 0} attempt(s)
          {action.effect_id ? `; effect #${action.effect_id} at ${formatTime(action.effect_applied_at)}` : ""}
          {action.outbox_error ? `; last error: ${action.outbox_error}` : ""}
        </p>
      ) : null}
    </article>
  );
}

export default async function RunPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const run = await load(id);
  const checks = run.checks?.numbers ?? [];
  const cited = new Set(
    checks
      .map(anchor)
      .filter((href): href is string => href !== null)
      .map((href) => href.slice(1)),
  );
  const moving =
    run.status === "running" || run.actions.some((a) => a.state === "approved");

  return (
    <>
      <AutoRefresh active={moving} />
      <p className="crumbs">
        <Link href="/">Runs</Link> / <code>{run.id}</code>
      </p>
      <h1>{run.question}</h1>
      <p className="meta">
        <Badge value={run.status === "completed" ? run.disposition : run.status} />{" "}
        <span>{run.model}</span> · <span>{formatMs(run.duration_ms)}</span> ·{" "}
        <span>{run.usage?.model_calls ?? 0} model calls</span> ·{" "}
        <span>{run.tool_calls.length} tool calls</span>
        {run.stop_reason ? <> · stopped: {run.stop_reason}</> : null}
      </p>

      {run.status === "running" ? <p role="status">Working…</p> : null}
      {run.error ? <p className="error">Run failed: {run.error}</p> : null}

      <section aria-labelledby="finding">
        <h2 id="finding">Finding</h2>
        {run.finding ? (
          <div className="card">
            <p className="answer">{run.finding.answer}</p>
            <p className="muted">
              Model confidence: {run.finding.confidence} · state: <Badge value={run.finding.state} />
              {run.finding.decided_by
                ? ` · ${run.finding.state} by ${run.finding.decided_by}: “${run.finding.decision_reason}”`
                : ""}
            </p>
            {run.finding.caveats.length ? (
              <ul className="caveats">
                {run.finding.caveats.map((c) => (
                  <li key={c}>{c}</li>
                ))}
              </ul>
            ) : null}
            {run.finding.state === "held" ? (
              <Decision
                endpoint={`/api/findings/${run.id}`}
                approveVerb="publish"
                approveLabel="Publish finding"
                subject="the finding"
              />
            ) : null}
          </div>
        ) : (
          <p className="muted">No finding was accepted.</p>
        )}
        {run.fallback ? <pre className="fallback">{run.fallback}</pre> : null}
      </section>

      <section aria-labelledby="checks">
        <h2 id="checks">Checks</h2>
        {checks.length === 0 && !run.checks?.policy_violations.length && !run.checks?.schema_errors.length ? (
          <p className="muted">No numbers to check and no policy findings.</p>
        ) : null}
        {checks.length ? (
          <ul className="checks">
            {checks.map((check, i) => {
              const href = anchor(check);
              return (
                <li key={i} className={check.grounded ? "pass" : "fail"}>
                  <span className="verdict">{check.grounded ? "Grounded" : "Not grounded"}</span>{" "}
                  <b>{check.text}</b> — {check.how}{" "}
                  {href ? <a href={href}>show source</a> : null}
                </li>
              );
            })}
          </ul>
        ) : null}
        {run.checks?.policy_violations.map((v) => (
          <p key={v} className="policy">
            Policy: {v}
          </p>
        ))}
        {run.checks?.schema_errors.map((e) => (
          <p key={e} className="error">
            Schema: {e}
          </p>
        ))}
      </section>

      <section aria-labelledby="actions">
        <h2 id="actions">Proposed actions</h2>
        {run.actions.length ? (
          run.actions.map((a) => <ActionCard key={a.id} action={a} />)
        ) : (
          <p className="muted">Nothing was proposed.</p>
        )}
      </section>

      <section aria-labelledby="timeline">
        <h2 id="timeline">Timeline</h2>
        <ol className="timeline">
          {run.steps.map((step) => (
            <li key={step.seq}>
              <span className="node">{step.node}</span>{" "}
              <span className="muted">
                +{formatMs(step.started_ms)} · {formatMs(step.duration_ms)}
              </span>{" "}
              {Object.keys(step.detail).length ? (
                <code className="detail">{JSON.stringify(step.detail)}</code>
              ) : null}
            </li>
          ))}
        </ol>
        {run.tool_calls.map((call) => (
          <article key={call.call_id} id={call.call_id} className="card tool">
            <header className="card-head">
              <h3>
                <code>{call.call_id}</code> {call.tool}
              </h3>
              <Badge value={call.ok ? "ok" : "failed"} />
              <span className="muted">{formatMs(call.duration_ms)}</span>
            </header>
            {call.tool === "query_warehouse" ? (
              <>
                <pre className="sql">{String(call.result?.executed_sql ?? call.arguments.sql ?? "")}</pre>
                {call.ok && call.result ? (
                  <ResultTable callId={call.call_id} result={call.result} cited={cited} />
                ) : null}
              </>
            ) : (
              <pre className="args">{JSON.stringify(call.arguments, null, 2)}</pre>
            )}
            {call.error ? <p className="error">{call.error}</p> : null}
          </article>
        ))}
      </section>

      <section aria-labelledby="trail">
        <h2 id="trail">Audit trail</h2>
        <ol className="trail">
          {run.audit.map((entry) => (
            <li key={entry.id}>
              <span className="muted">{formatTime(entry.at)}</span> <b>{entry.actor}</b>{" "}
              <code>{entry.event}</code> {entry.subject ? <code>{entry.subject}</code> : null}
            </li>
          ))}
        </ol>
      </section>
    </>
  );
}
