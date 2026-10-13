"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

const EXAMPLES = [
  "How many orders were returned?",
  "Show revenue by channel",
  "Refund order 1 in full, it arrived damaged.",
  "What is our Net Promoter Score?",
];

export function AskForm() {
  const router = useRouter();
  const [question, setQuestion] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    const response = await fetch("/api/runs", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ question }),
    });
    setBusy(false);
    if (!response.ok) {
      setError(`The API refused the question (${response.status}).`);
      return;
    }
    const { id } = (await response.json()) as { id: string };
    router.push(`/runs/${id}`);
  }

  return (
    <form onSubmit={submit} className="ask">
      <label htmlFor="question">Question</label>
      <textarea
        id="question"
        name="question"
        rows={2}
        required
        minLength={3}
        maxLength={1000}
        value={question}
        onChange={(e) => setQuestion(e.target.value)}
        placeholder="How many orders were returned?"
      />
      <div className="row">
        <button type="submit" disabled={busy}>
          {busy ? "Asking…" : "Ask"}
        </button>
        <span className="hint">
          Try:{" "}
          {EXAMPLES.map((example) => (
            <button
              key={example}
              type="button"
              className="link"
              onClick={() => setQuestion(example)}
            >
              {example}
            </button>
          ))}
        </span>
      </div>
      <p role="alert" className="error">
        {error}
      </p>
    </form>
  );
}
