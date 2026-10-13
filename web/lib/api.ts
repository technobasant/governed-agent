import "server-only";

import type { AuditEntry, ReviewQueue, RunDetail, RunSummary } from "./types";

// Server components call the API directly; the browser goes through /api (see
// app/api/[...path]/route.ts), so the API never needs to be exposed or allow CORS.
const API_URL = process.env.API_URL ?? "http://127.0.0.1:8000";

export class NotFoundError extends Error {}

async function get<T>(path: string): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, { cache: "no-store" });
  if (response.status === 404) throw new NotFoundError(path);
  if (!response.ok) throw new Error(`GET ${path} failed: ${response.status}`);
  return (await response.json()) as T;
}

export const listRuns = () => get<RunSummary[]>("/runs");
export const getRun = (id: string) => get<RunDetail>(`/runs/${encodeURIComponent(id)}`);
export const getReviewQueue = () => get<ReviewQueue>("/review-queue");
export const listAudit = () => get<AuditEntry[]>("/audit?limit=300");
