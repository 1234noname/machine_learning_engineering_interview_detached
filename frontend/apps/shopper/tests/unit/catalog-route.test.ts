/**
 * Unit tests for the catalog proxy route.
 *
 * `GET /api/catalog?page=&limit=` forwards to the Python API's
 * `GET /catalog` (at $AVSA_API_URL) and re-streams the JSON body. It mirrors
 * the existing `/api/products/[id]` and `/api/health` proxy routes: the
 * upstream host is read from the environment (never hard-coded), pagination
 * query params are forwarded verbatim, and upstream failures map to a 502.
 *
 * `fetch` is stubbed so no real backend is hit. Written test-first: the route
 * module does not exist yet, so the dynamic import fails until implemented.
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { NextRequest } from "next/server";

const mockFetch = vi.fn<typeof fetch>();
vi.stubGlobal("fetch", mockFetch);

beforeEach(() => {
  mockFetch.mockReset();
  vi.unstubAllEnvs();
});

const { GET } = await import("../../app/api/catalog/route");

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("GET /api/catalog", () => {
  it("forwards page and limit query params to the upstream /catalog", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({ items: [], page: 3, limit: 12, total: 0 }),
    );

    const req = new NextRequest("http://localhost:3000/api/catalog?page=3&limit=12");
    const res = await GET(req);

    expect(res.status).toBe(200);
    const upstreamUrl = String(mockFetch.mock.calls[0]?.[0]);
    expect(upstreamUrl).toContain("/catalog");
    expect(upstreamUrl).toContain("page=3");
    expect(upstreamUrl).toContain("limit=12");
  });

  it("uses AVSA_API_URL as the upstream host when set", async () => {
    vi.stubEnv("AVSA_API_URL", "http://api.internal:9000");
    mockFetch.mockResolvedValueOnce(
      jsonResponse({ items: [], page: 1, limit: 20, total: 0 }),
    );

    const req = new NextRequest("http://localhost:3000/api/catalog");
    await GET(req);

    const upstreamUrl = String(mockFetch.mock.calls[0]?.[0]);
    expect(upstreamUrl).toContain("http://api.internal:9000/catalog");
  });

  it("re-streams the upstream JSON body verbatim", async () => {
    const body = {
      items: [
        {
          id: "x",
          title: "T",
          category: "C",
          price_cents: 1234,
          currency: "ZAR",
          image_url: "http://localhost:8080/images/x.jpg?token=t&expires=1",
        },
      ],
      page: 1,
      limit: 20,
      total: 1,
    };
    mockFetch.mockResolvedValueOnce(jsonResponse(body));

    const req = new NextRequest("http://localhost:3000/api/catalog");
    const res = await GET(req);
    const data = (await res.json()) as typeof body;

    expect(res.status).toBe(200);
    expect(data).toEqual(body);
  });

  it("propagates the upstream 422 status for malformed pagination", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({ code: "invalid_pagination", message: "page < 1" }, 422),
    );

    const req = new NextRequest("http://localhost:3000/api/catalog?page=0");
    const res = await GET(req);

    expect(res.status).toBe(422);
  });

  it("returns 502 when the upstream fetch throws", async () => {
    mockFetch.mockRejectedValueOnce(new Error("ECONNREFUSED"));

    const req = new NextRequest("http://localhost:3000/api/catalog");
    const res = await GET(req);

    expect(res.status).toBe(502);
  });
});
