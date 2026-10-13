import { DISPOSITION_LABEL } from "@/lib/format";
import type { Disposition } from "@/lib/types";

const TONE: Record<string, string> = {
  published: "ok",
  executed: "ok",
  approved: "info",
  held_for_review: "warn",
  held: "warn",
  pending: "warn",
  running: "info",
  unanswerable: "muted",
  rejected: "bad",
  failed: "bad",
  completed: "muted",
};

export function Badge({ value }: { value: string | null }) {
  if (!value) return null;
  const label = DISPOSITION_LABEL[value as Disposition] ?? value.replaceAll("_", " ");
  return <span className={`badge badge-${TONE[value] ?? "muted"}`}>{label}</span>;
}
