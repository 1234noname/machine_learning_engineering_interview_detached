import type { NextRequest } from "next/server";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

/**
 * GET /api/docs — proxy to the AVSA Python API's OpenAPI interactive docs.
 *
 * The FastAPI Swagger UI references /openapi.json at the origin root;
 * app/openapi.json/route.ts satisfies that request so the full UI loads.
 */
export async function GET(_request: NextRequest): Promise<Response> {
  const apiUrl = process.env.AVSA_API_URL ?? "http://localhost:8080";

  let upstream: Response;
  try {
    upstream = await fetch(`${apiUrl}/docs`);
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
