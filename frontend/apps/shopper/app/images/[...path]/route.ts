import { NextRequest, NextResponse } from "next/server";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

/**
 * GET /images/[...path] — server-side image proxy.
 *
 * The catalog API signs image URLs as relative `/images/{key}?token=…&expires=…`
 * paths. Client components cannot resolve these to the API host because
 * AVSA_API_URL is a server-only env var (no NEXT_PUBLIC_ prefix, intentional:
 * the API URL should not be in the client bundle). This route proxies the
 * request server-side to the API, so the browser always fetches from the
 * shopper origin and never needs to know the API host.
 *
 * Forwards only `token` and `expires` — the two query params the API's signed
 * image endpoint requires. Other params are dropped at this boundary.
 */
export async function GET(
  req: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
): Promise<NextResponse> {
  const apiUrl = process.env.AVSA_API_URL ?? "http://localhost:8080";
  const { path } = await params;

  const upstream = new URL(`/images/${path.join("/")}`, apiUrl);
  const { searchParams } = req.nextUrl;
  const token = searchParams.get("token");
  const expires = searchParams.get("expires");
  if (token !== null) upstream.searchParams.set("token", token);
  if (expires !== null) upstream.searchParams.set("expires", expires);

  let res: Response;
  try {
    res = await fetch(upstream.toString(), { cache: "no-store" });
  } catch (err: unknown) {
    const message = err instanceof Error ? err.message : "upstream fetch failed";
    return NextResponse.json({ code: "upstream_error", message }, { status: 502 });
  }

  const headers = new Headers();
  const contentType = res.headers.get("content-type");
  if (contentType) headers.set("content-type", contentType);
  const cacheControl = res.headers.get("cache-control");
  if (cacheControl) headers.set("cache-control", cacheControl);

  const body = await res.arrayBuffer();
  return new NextResponse(body, { status: res.status, headers });
}
