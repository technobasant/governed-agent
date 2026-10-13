import type { Disposition } from "./types";

export const DISPOSITION_LABEL: Record<Disposition, string> = {
  published: "Published",
  held_for_review: "Held for review",
  unanswerable: "Can't answer",
  rejected: "Rejected",
};

export function formatTime(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("en-GB", {
    dateStyle: "medium",
    timeStyle: "medium",
    timeZone: "UTC",
  });
}

export function formatMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms)} ms`;
}

export function formatCell(value: unknown): string {
  if (value === null || value === undefined) return "NULL";
  if (typeof value === "number") return Number.isInteger(value) ? String(value) : value.toString();
  return String(value);
}

export function cellId(callId: string, row: number, column: string): string {
  return `cell-${callId}-${row}-${column.replace(/[^a-zA-Z0-9_-]/g, "_")}`;
}

export function describeArguments(args: Record<string, unknown>): string {
  return Object.entries(args)
    .map(([k, v]) => `${k}: ${typeof v === "string" ? v : JSON.stringify(v)}`)
    .join(", ");
}
