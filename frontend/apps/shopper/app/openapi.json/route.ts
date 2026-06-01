import type { NextRequest } from "next/server";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

/**
 * GET /openapi.json — proxy to the AVSA Python API's OpenAPI spec.
 *
 * The FastAPI Swagger UI loaded from /api/docs references /openapi.json at the
 * root of the origin. This route satisfies that request so the full API docs
 * experience works through the shopper LoadBalancer.
 */
export async function GET(_request: NextRequest): Promise<Response> {
  const apiUrl = process.env.AVSA_API_URL ?? "http://localhost:8080";

  let upstream: Response;
  try {
    upstream = await fetch(`${apiUrl}/openapi.json`);
  } catch {
    return new Response("Service unavailable", { status: 503 });
  }

  const responseHeaders = new Headers();
  responseHeaders.set("content-type", "application/json");
  const cc = upstream.headers.get("cache-control");
  if (cc) responseHeaders.set("cache-control", cc);

  return new Response(upstream.body, {
    status: upstream.status,
    headers: responseHeaders,
  });
}
