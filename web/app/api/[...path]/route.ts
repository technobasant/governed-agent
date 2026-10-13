import type { NextRequest } from "next/server";

// The browser's only way to the API. Server-side, so the API stays on the internal
// network, and allow-listed, so admin endpoints are not reachable from a page.
const API_URL = process.env.API_URL ?? "http://127.0.0.1:8000";
const ALLOWED = new Set(["runs", "review-queue", "actions", "findings", "audit"]);
const FORWARDED_HEADERS = ["content-type", "x-reviewer"];

async function proxy(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
): Promise<Response> {
  const { path } = await params;
  if (!ALLOWED.has(path[0] ?? "")) {
    return Response.json({ detail: "not found" }, { status: 404 });
  }
  const url = new URL(`${API_URL}/${path.map(encodeURIComponent).join("/")}`);
  url.search = request.nextUrl.search;
  const headers = new Headers();
  for (const name of FORWARDED_HEADERS) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }
  const upstream = await fetch(url, {
    method: request.method,
    headers,
    body: request.method === "GET" ? undefined : await request.text(),
    cache: "no-store",
  });
  return new Response(upstream.body, {
    status: upstream.status,
    headers: { "content-type": upstream.headers.get("content-type") ?? "application/json" },
  });
}

export { proxy as GET, proxy as POST };
