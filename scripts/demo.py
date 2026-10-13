"""Ask a few questions through the API and show what each one became.

uv run python scripts/demo.py --api http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import textwrap

import httpx

QUESTIONS = (
    "How many orders were returned?",
    "What do the refund notes say for order 1361?",
    "Order 1 arrived damaged. Please refund it in full.",
    "What is our Net Promoter Score?",
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--web", default="http://localhost:3000")
    args = parser.parse_args()

    with httpx.Client(base_url=args.api, timeout=600) as client:
        print(f"model: {client.get('/healthz').json()['model']}\n")
        for question in QUESTIONS:
            run = client.post("/runs", params={"wait": "true"}, json={"question": question})
            run.raise_for_status()
            body = run.json()
            finding = body.get("finding") or {}
            print(f"Q: {question}")
            took = f"{body['duration_ms']:.0f} ms, {len(body['tool_calls'])} tool calls"
            print(f"   {body['status']} → {body.get('disposition')}  ({took})")
            answer = finding.get("answer") or body.get("fallback") or body.get("error") or ""
            print(textwrap.indent(textwrap.fill(answer, 88), "   "))
            checks = body.get("checks") or {}
            ungrounded = [n["text"] for n in checks.get("numbers", []) if not n["grounded"]]
            if ungrounded:
                print(f"   not grounded: {', '.join(ungrounded)}")
            for action in body["actions"]:
                print(
                    f"   proposed: {action['action_type']} {action['arguments']} → "
                    f"{action['state']}"
                )
            print(f"   {args.web}/runs/{body['id']}\n")
    print(f"Review queue: {args.web}/review")


if __name__ == "__main__":
    main()
