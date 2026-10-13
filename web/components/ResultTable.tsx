import { cellId, formatCell } from "@/lib/format";
import type { QueryResult } from "@/lib/types";

/** A query result with an anchor on every cell, so a number in a finding can link here. */
export function ResultTable({
  callId,
  result,
  cited,
}: {
  callId: string;
  result: QueryResult;
  cited: Set<string>;
}) {
  const columns = result.columns ?? [];
  const rows = result.rows ?? [];
  if (rows.length === 0) return <p className="muted">No rows.</p>;
  return (
    <div className="table-wrap">
      <table className="result">
        <caption className="sr-only">Rows returned by {callId}</caption>
        <thead>
          <tr>
            <th scope="col">#</th>
            {columns.map((c) => (
              <th scope="col" key={c}>
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, r) => (
            <tr key={r}>
              <td className="muted">{r}</td>
              {row.map((value, c) => {
                const id = cellId(callId, r, columns[c] ?? String(c));
                return (
                  <td key={c} id={id} className={cited.has(id) ? "cited" : undefined}>
                    {formatCell(value)}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
      {result.truncated || result.rows_hidden ? (
        <p className="muted">
          {result.rows_hidden ? `${result.rows_hidden} more rows were not shown to the model. ` : ""}
          {result.truncated ? "The result hit the row limit." : ""}
        </p>
      ) : null}
    </div>
  );
}
