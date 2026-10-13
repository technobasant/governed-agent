import Link from "next/link";

import { listAudit } from "@/lib/api";
import { formatTime } from "@/lib/format";

export const dynamic = "force-dynamic";

export default async function AuditPage() {
  const entries = await listAudit();
  return (
    <>
      <h1>Audit log</h1>
      <p className="lede">
        Append-only: the database rejects updates, deletes and truncation of this table.
      </p>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th scope="col">#</th>
              <th scope="col">When (UTC)</th>
              <th scope="col">Actor</th>
              <th scope="col">Event</th>
              <th scope="col">Run</th>
              <th scope="col">Subject</th>
              <th scope="col">Detail</th>
            </tr>
          </thead>
          <tbody>
            {entries.map((e) => (
              <tr key={e.id}>
                <td>{e.id}</td>
                <td>{formatTime(e.at)}</td>
                <td>{e.actor}</td>
                <td>
                  <code>{e.event}</code>
                </td>
                <td>{e.run_id ? <Link href={`/runs/${e.run_id}`}>{e.run_id}</Link> : "—"}</td>
                <td>{e.subject ?? "—"}</td>
                <td>
                  <code className="detail">{JSON.stringify(e.detail)}</code>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
