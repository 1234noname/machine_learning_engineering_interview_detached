import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

/**
 * GET /api/catalog?page=&limit=
 *
 * Proxies to the AVSA API service at AVSA_API_URL/catalog. Forwards the
 * pagination query params verbatim so the browse grid never talks to the
 * Python API directly (no CORS / cross-origin exposure). The upstream host is
 * read from the environment (config-driven, per CLAUDE.md) so local / staging
 * / prod can differ without a code change.
 *
 * The upstream contract (specs/api/chat.openapi.yaml#getCatalog):
 *   - 200 CatalogPage on success (including an empty page past the end)
 *   - 422 for malformed pagination (page < 1 / limit < 1) — propagated as-is
 * An unreachable upstream maps to a 502 so the grid can show an error state.
 */
export async function GET(req: NextRequest): Promise<NextResponse> {
  const apiUrl = process.env.AVSA_API_URL ?? "http://localhost:8080";

  // Forward only the pagination params the contract defines.
  const upstream = new URL("/catalog", apiUrl);
  const { searchParams } = req.nextUrl;
  const page = searchParams.get("page");
  const limit = searchParams.get("limit");
  if (page !== null) upstream.searchParams.set("page", page);
  if (limit !== null) upstream.searchParams.set("limit", limit);

  let res: Response;
  try {
    res = await fetch(upstream.toString(), { cache: "no-store" });
  } catch (err: unknown) {
    const message =
      err instanceof Error ? err.message : "Upstream fetch failed";
    return NextResponse.json(
      { code: "upstream_error", message },
      { status: 502 },
    );
  }

  // Re-stream the JSON body verbatim, preserving the upstream status (200 page
  // or 422 malformed-pagination) so the client can react to each precisely.
  const data: unknown = await res.json().catch(() => null);
  return NextResponse.json(data, { status: res.status });
}
