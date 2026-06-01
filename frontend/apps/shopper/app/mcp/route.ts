import type { NextRequest } from "next/server";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

/**
 * POST /mcp — JSON-RPC 2.0 pass-through to AVSA's conformant MCP server.
 *
 * Forwards the request body verbatim to $AVSA_MCP_URL (the Elixir
 * AVSA.MCP.Server — Streamable HTTP + JSON-RPC 2.0) and streams the
 * response back. This is the single MCP endpoint: `initialize`, `tools/list`
 * and `tools/call` are all dispatched by method in the JSON-RPC body, so the
 * proxy does not interpret the payload — it just relays it.
 *
 * Exists so the smoke suite and external MCP clients can reach the MCP surface
 * through the shopper LoadBalancer without cross-origin issues or exposing the
 * orchestrator's MCP port directly.
 *
 * Auth: the JSON-RPC server expects `Authorization: Bearer <key>` (constant-time
 * compared, config-driven via :mcp_api_key). We forward that header unchanged
 * when present; when the server is unkeyed (local default) it is ignored.
 */
export async function POST(request: NextRequest): Promise<Response> {
  const mcpUrl = process.env.AVSA_MCP_URL ?? "http://localhost:8082";

  let body: string;
  try {
    body = await request.text();
  } catch {
    return new Response("Bad request", { status: 400 });
  }

  const upstreamHeaders: Record<string, string> = {
    "content-type": "application/json",
  };
  const auth = request.headers.get("authorization");
  if (auth) upstreamHeaders["authorization"] = auth;

  let upstream: Response;
  try {
    upstream = await fetch(mcpUrl, {
      method: "POST",
      headers: upstreamHeaders,
      body,
    });
  } catch {
    return new Response("Service unavailable", { status: 503 });
  }

  const responseHeaders = new Headers();
  const ct = upstream.headers.get("content-type");
  if (ct) responseHeaders.set("content-type", ct);

  return new Response(upstream.body, {
    status: upstream.status,
    headers: responseHeaders,
  });
}
