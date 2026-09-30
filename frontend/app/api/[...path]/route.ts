import type { NextRequest } from "next/server";

import { isAuthorized, unauthorizedResponse } from "@/lib/auth";

/**
 * Same-origin proxy to the FastAPI backend.
 * - keeps the backend token server-side (API_AUTH_TOKEN), no CORS needed in the browser
 * - streams request bodies (multi-GB video uploads) and responses (video range requests)
 */
const BACKEND_URL = process.env.BACKEND_URL || "http://localhost:8000";
const FORWARD_REQUEST_HEADERS = ["content-type", "content-length", "range", "accept", "if-none-match", "if-modified-since", "if-range"];
const DROP_RESPONSE_HEADERS = new Set(["connection", "keep-alive", "transfer-encoding", "content-encoding"]);

export const dynamic = "force-dynamic";

async function handler(request: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  if (!isAuthorized(request.headers.get("authorization"))) return unauthorizedResponse();
  const { path } = await ctx.params;
  const target = new URL(`/api/${path.map(encodeURIComponent).join("/")}${request.nextUrl.search}`, BACKEND_URL);

  const headers = new Headers();
  for (const name of FORWARD_REQUEST_HEADERS) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }
  if (process.env.API_AUTH_TOKEN) headers.set("authorization", `Bearer ${process.env.API_AUTH_TOKEN}`);

  const init: RequestInit & { duplex?: "half" } = { method: request.method, headers, redirect: "manual", cache: "no-store" };
  if (request.method !== "GET" && request.method !== "HEAD" && request.body) {
    init.body = request.body;
    init.duplex = "half";
  }

  let upstream: Response;
  try {
    upstream = await fetch(target, init);
  } catch {
    return Response.json(
      { detail: `Backend niet bereikbaar op ${BACKEND_URL}. Draait de API (uvicorn app.main:app)?` },
      { status: 502 },
    );
  }
  const out = new Headers();
  const encoded = upstream.headers.has("content-encoding");
  upstream.headers.forEach((value, key) => {
    if (DROP_RESPONSE_HEADERS.has(key)) return;
    if (encoded && key === "content-length") return;
    out.set(key, value);
  });
  return new Response(upstream.body, { status: upstream.status, statusText: upstream.statusText, headers: out });
}

export { handler as DELETE, handler as GET, handler as HEAD, handler as PATCH, handler as POST, handler as PUT };
