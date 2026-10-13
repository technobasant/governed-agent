"use client";

import { useRouter } from "next/navigation";
import { useId, useState, useSyncExternalStore } from "react";

interface Props {
  /** e.g. `/api/actions/act_123` or `/api/findings/run_123` */
  endpoint: string;
  approveVerb: "approve" | "publish";
  approveLabel: string;
  subject: string;
}

const REVIEWER_KEY = "governed-agent.reviewer";

// The reviewer's name is remembered in localStorage between decisions.
function subscribe(onChange: () => void): () => void {
  window.addEventListener("storage", onChange);
  return () => window.removeEventListener("storage", onChange);
}
const storedReviewer = () => window.localStorage.getItem(REVIEWER_KEY) ?? "";
const noReviewer = () => "";

export function Decision({ endpoint, approveVerb, approveLabel, subject }: Props) {
  const router = useRouter();
  const id = useId();
  const stored = useSyncExternalStore(subscribe, storedReviewer, noReviewer);
  const [typed, setReviewer] = useState<string | null>(null);
  const reviewer = typed ?? stored;
  const [reason, setReason] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function decide(verb: string) {
    setBusy(true);
    setMessage(null);
    window.localStorage.setItem(REVIEWER_KEY, reviewer);
    const response = await fetch(`${endpoint}/${verb}`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-reviewer": reviewer },
      body: JSON.stringify({ reason }),
    });
    setBusy(false);
    if (!response.ok) {
      const body = (await response.json().catch(() => ({}))) as { detail?: unknown };
      const detail = typeof body.detail === "string" ? body.detail : "check the fields";
      setMessage(`Not recorded (${response.status}): ${detail}`);
      return;
    }
    setReason("");
    setMessage("Recorded.");
    router.refresh();
  }

  const ready = reviewer.trim().length >= 2 && reason.trim().length >= 3 && !busy;
  return (
    <form
      className="decision"
      aria-label={`Decide on ${subject}`}
      onSubmit={(e) => {
        e.preventDefault();
        void decide(approveVerb);
      }}
    >
      <div className="field">
        <label htmlFor={`${id}-reviewer`}>Reviewer</label>
        <input
          id={`${id}-reviewer`}
          value={reviewer}
          onChange={(e) => setReviewer(e.target.value.toLowerCase())}
          pattern="[a-z][a-z0-9._\-]{1,39}"
          placeholder="your.name"
          autoComplete="username"
          required
        />
      </div>
      <div className="field grow">
        <label htmlFor={`${id}-reason`}>Reason</label>
        <input
          id={`${id}-reason`}
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          minLength={3}
          maxLength={500}
          placeholder="Why — this goes into the audit log"
          required
        />
      </div>
      <div className="buttons">
        <button type="submit" disabled={!ready}>
          {approveLabel}
        </button>
        <button type="button" className="secondary" disabled={!ready} onClick={() => void decide("reject")}>
          Reject
        </button>
      </div>
      <p role="status" className="status">
        {message}
      </p>
    </form>
  );
}
