// Mirrors the API's response models (api/ga_api/schemas.py).

export type Disposition = "published" | "held_for_review" | "unanswerable" | "rejected";

export interface NumberCheck {
  text: string;
  value: number;
  grounded: boolean;
  how: string | null;
  tool_call_id: string | null;
  column: string | null;
  row: number | null;
}

export interface Checks {
  schema_errors: string[];
  policy_violations: string[];
  numbers: NumberCheck[];
}

export interface Usage {
  model_calls: number;
  tokens_in: number;
  tokens_out: number;
}

export interface RunSummary {
  id: string;
  question: string;
  model: string;
  status: "running" | "completed" | "failed";
  disposition: Disposition | null;
  finding_state: string | null;
  pending_actions: number;
  tool_calls: number;
  created_at: string;
  duration_ms: number | null;
}

export interface Step {
  seq: number;
  node: string;
  started_ms: number;
  duration_ms: number;
  detail: Record<string, unknown>;
}

export interface QueryResult {
  ok: boolean;
  executed_sql?: string;
  columns?: string[];
  rows?: unknown[][];
  row_count?: number;
  truncated?: boolean;
  rows_hidden?: number;
  [key: string]: unknown;
}

export interface ToolCall {
  call_id: string;
  seq: number;
  tool: string;
  arguments: Record<string, unknown>;
  ok: boolean;
  result: QueryResult | null;
  error: string | null;
  duration_ms: number;
}

export interface NumberClaim {
  label: string;
  value: number;
  unit: string | null;
  source_query: string | null;
  calculation: string | null;
}

export interface Finding {
  status: "answered" | "unanswerable";
  answer: string;
  confidence: string;
  numbers: NumberClaim[];
  caveats: string[];
  state: "published" | "held" | "rejected" | "unanswerable";
  decided_by: string | null;
  decided_at: string | null;
  decision_reason: string | null;
}

export type ActionState = "pending" | "approved" | "rejected" | "executed" | "failed";

export interface Action {
  id: string;
  run_id: string;
  question: string;
  proposal_id: string;
  tool_call_id: string;
  action_type: string;
  arguments: Record<string, unknown>;
  reason: string;
  risk: "low" | "medium" | "high";
  state: ActionState;
  created_at: string;
  decided_by: string | null;
  decided_at: string | null;
  decision_reason: string | null;
  executed_at: string | null;
  error: string | null;
  outbox_state: string | null;
  outbox_attempts: number | null;
  outbox_error: string | null;
  effect_id: number | null;
  effect_applied_at: string | null;
}

export interface AuditEntry {
  id: number;
  at: string;
  actor: string;
  event: string;
  run_id: string | null;
  subject: string | null;
  detail: Record<string, unknown>;
}

export interface RunDetail {
  id: string;
  question: string;
  model: string;
  status: RunSummary["status"];
  disposition: Disposition | null;
  stop_reason: string | null;
  fallback: string | null;
  error: string | null;
  checks: Checks | null;
  usage: Usage | null;
  duration_ms: number | null;
  created_at: string;
  finished_at: string | null;
  steps: Step[];
  tool_calls: ToolCall[];
  finding: Finding | null;
  actions: Action[];
  audit: AuditEntry[];
}

export interface HeldFinding extends Finding {
  run_id: string;
  question: string;
  checks: Checks | null;
  created_at: string;
}

export interface ReviewQueue {
  actions: Action[];
  findings: HeldFinding[];
}
