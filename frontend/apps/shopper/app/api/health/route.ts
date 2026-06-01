import { NextResponse } from "next/server";

// Proxy GET /api/health → $AVSA_API_URL/health
// Returns 200 with the upstream JSON body on success, or 502 on error.
// The upstream URL is intentionally not hard-coded: it is read from the
// environment so staging/prod/local environments can differ without a code
// change (per CLAUDE.md config-driven principle).
export async function GET(): Promise<NextResponse> {
  const apiUrl = process.env["AVSA_API_URL"] ?? "http://localhost:8080";

  try {
    const upstream = await fetch(`${apiUrl}/health`, {
      // Prevent Next.js from caching the health response — health checks must
      // always reflect the current state of the upstream service.
      cache: "no-store",
    });

    const body: unknown = await upstream.json();
    return NextResponse.json(body, { status: upstream.status });
  } catch {
    return NextResponse.json(
      { status: "unreachable" },
      { status: 502 },
    );
  }
}
