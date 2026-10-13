import Link from "next/link";

import { AutoRefresh } from "@/components/AutoRefresh";
import { Decision } from "@/components/Decision";
import { getReviewQueue } from "@/lib/api";
import { describeArguments, formatTime } from "@/lib/format";

export const dynamic = "force-dynamic";

export default async function ReviewPage() {
  const queue = await getReviewQueue();
  return (
    <>
      <AutoRefresh active ms={5000} />
      <h1>Review queue</h1>
      <p className="lede">
        Nothing here has happened. Approving an action records your decision and queues it for
        execution, once; rejecting it ends it. Both go into the audit log with your reason.
      </p>

      <h2>Proposed actions ({queue.actions.length})</h2>
      {queue.actions.length === 0 ? <p className="muted">Nothing waiting.</p> : null}
      {queue.actions.map((action) => (
        <article key={action.id} className="card" aria-labelledby={`${action.id}-title`}>
          <header className="card-head">
            <h3 id={`${action.id}-title`}>
              <code>{action.action_type}</code>
            </h3>
            <span className={`risk risk-${action.risk}`}>{action.risk} risk</span>
            <span className="muted">{formatTime(action.created_at)}</span>
          </header>
          <p>{describeArguments(action.arguments)}</p>
          <p className="muted">
            From <Link href={`/runs/${action.run_id}`}>“{action.question}”</Link>. Agent’s reason:{" "}
            {action.reason}
          </p>
          <Decision
            endpoint={`/api/actions/${action.id}`}
            approveVerb="approve"
            approveLabel="Approve and execute"
            subject={`${action.action_type} from run ${action.run_id}`}
          />
        </article>
      ))}

      <h2>Findings held for review ({queue.findings.length})</h2>
      {queue.findings.length === 0 ? <p className="muted">Nothing waiting.</p> : null}
      {queue.findings.map((finding) => {
        const ungrounded = finding.checks?.numbers.filter((n) => !n.grounded) ?? [];
        return (
          <article key={finding.run_id} className="card" aria-label={finding.question}>
            <h3>
              <Link href={`/runs/${finding.run_id}`}>{finding.question}</Link>
            </h3>
            <p className="answer">{finding.answer}</p>
            {ungrounded.length ? (
              <p className="policy">
                Not grounded: {ungrounded.map((n) => n.text).join(", ")}
              </p>
            ) : null}
            {finding.checks?.policy_violations.map((v) => (
              <p key={v} className="policy">
                Policy: {v}
              </p>
            ))}
            <Decision
              endpoint={`/api/findings/${finding.run_id}`}
              approveVerb="publish"
              approveLabel="Publish finding"
              subject="the finding"
            />
          </article>
        );
      })}
    </>
  );
}
