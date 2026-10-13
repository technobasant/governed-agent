import Link from "next/link";

import { AskForm } from "@/components/AskForm";
import { AutoRefresh } from "@/components/AutoRefresh";
import { Badge } from "@/components/Badge";
import { listRuns } from "@/lib/api";
import { formatMs, formatTime } from "@/lib/format";

export const dynamic = "force-dynamic";

export default async function Home() {
  const runs = await listRuns();
  return (
    <>
      <h1>Ask the warehouse</h1>
      <p className="lede">
        Answers are published only when every number traces to a query result. Anything that
        would change something is proposed here and waits for a person.
      </p>
      <AskForm />
      <AutoRefresh active={runs.some((r) => r.status === "running")} />
      <h2>Recent runs</h2>
      {runs.length === 0 ? (
        <p className="muted">No runs yet.</p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">Question</th>
                <th scope="col">Outcome</th>
                <th scope="col">Tool calls</th>
                <th scope="col">Pending actions</th>
                <th scope="col">Took</th>
                <th scope="col">Asked (UTC)</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((run) => (
                <tr key={run.id}>
                  <td>
                    <Link href={`/runs/${run.id}`}>{run.question}</Link>
                  </td>
                  <td>
                    <Badge value={run.status === "completed" ? run.disposition : run.status} />
                  </td>
                  <td>{run.tool_calls}</td>
                  <td>{run.pending_actions || "—"}</td>
                  <td>{formatMs(run.duration_ms)}</td>
                  <td>{formatTime(run.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
