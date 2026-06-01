/**
 * Unit tests for the BrowseGrid component.
 *
 * BrowseGrid fetches a page of the real catalog from the local
 * `GET /api/catalog?page=&limit=` proxy route (which forwards to the Python
 * API's `GET /catalog`) and renders a responsive product grid using the
 * existing ProductCard. The contract it consumes is the OpenAPI `CatalogPage`
 * schema: `{ items: CatalogItem[], page, limit, total }` where each
 * CatalogItem carries `price_cents` (ZAR-denominated cents) and a pre-signed
 * `image_url` proxy path.
 *
 * These are true unit tests: `fetch` is replaced with a vitest stub (the
 * convention used by ChatInput.test.tsx) so no real backend is hit, and
 * `next/image` is mocked (the convention used by ProductCard.test.tsx) to
 * avoid Next.js internals under jsdom.
 *
 * Written test-first (2A-i): BrowseGrid does not exist yet, so the dynamic
 * import below fails and every test reports its own intent.
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { components } from "@avsa/shared";

type CatalogPage = components["schemas"]["CatalogPage"];
type CatalogItem = components["schemas"]["CatalogItem"];

// Mock next/image — it requires Next.js internals not present under jsdom.
vi.mock("next/image", () => ({
  default: ({ src, alt }: { src: string; alt: string }) => (
    // eslint-disable-next-line @next/next/no-img-element
    <img src={src} alt={alt} />
  ),
}));

// Replace global fetch with a spy (ChatInput.test.tsx convention).
const mockFetch = vi.fn<typeof fetch>();
vi.stubGlobal("fetch", mockFetch);

beforeEach(() => {
  mockFetch.mockReset();
});

// Dynamically import after mocks are registered.
const { default: BrowseGrid } = await import("../../app/components/BrowseGrid");

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function item(overrides: Partial<CatalogItem> = {}): CatalogItem {
  return {
    id: "prod-001",
    title: "Red Dress",
    category: "Dresses",
    price_cents: 49999,
    currency: "ZAR",
    // The REAL shape `GET /catalog` emits: a read-time-signed, RELATIVE
    // `/images/{key}?token=…&expires=…` proxy path (catalog.py:_sign_image_url).
    // The grid must resolve+validate this through the proxyUrl seam so a real
    // image renders; if it regressed to passing this relative path straight to
    // <Image> (which would fail) the src assertion below would catch it.
    image_url: "/images/red-dress.jpg?token=abc&expires=99",
    ...overrides,
  };
}

function page(overrides: Partial<CatalogPage> = {}): CatalogPage {
  return {
    items: [item()],
    page: 1,
    limit: 20,
    total: 1,
    ...overrides,
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("BrowseGrid", () => {
  it("fetches the catalog from the /api/catalog proxy on mount", async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse(page()));

    render(<BrowseGrid />);

    await waitFor(() => expect(mockFetch).toHaveBeenCalled());
    const url = String(mockFetch.mock.calls[0]?.[0]);
    expect(url).toContain("/api/catalog");
    expect(url).toContain("page=1");
  });

  it("renders a card for each item returned by the catalog page", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse(
        page({
          items: [
            item({ id: "a", title: "Alpha Coat" }),
            item({ id: "b", title: "Beta Boots", category: "Shoes" }),
          ],
          total: 2,
        }),
      ),
    );

    render(<BrowseGrid />);

    expect(await screen.findByText("Alpha Coat")).toBeInTheDocument();
    expect(screen.getByText("Beta Boots")).toBeInTheDocument();
  });

  it("formats price_cents (ZAR cents) as a rand amount (49999c → 499.99)", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse(page({ items: [item({ price_cents: 49999 })] })),
    );

    render(<BrowseGrid />);

    const priceEl = await screen.findByTestId("product-price");
    // 49999 cents → R 499,99 — the cents must be divided by 100, not shown raw.
    expect(priceEl.textContent).toMatch(/499[.,]99/);
    expect(priceEl.textContent).not.toMatch(/49999/);
  });

  it("shows a loading state while the catalog request is in flight", async () => {
    let resolve!: (r: Response) => void;
    mockFetch.mockReturnValueOnce(
      new Promise<Response>((r) => {
        resolve = r;
      }),
    );

    render(<BrowseGrid />);

    expect(screen.getByTestId("browse-grid-loading")).toBeInTheDocument();

    resolve(jsonResponse(page()));
    await waitFor(() =>
      expect(screen.queryByTestId("browse-grid-loading")).not.toBeInTheDocument(),
    );
  });

  it("shows an empty state when a page past the end returns no items", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse(page({ items: [], page: 99, total: 1 })),
    );

    render(<BrowseGrid />);

    expect(await screen.findByTestId("browse-grid-empty")).toBeInTheDocument();
  });

  it("shows an error state when the catalog request fails (non-200)", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse({ code: "upstream_error", message: "boom" }, 502),
    );

    render(<BrowseGrid />);

    expect(await screen.findByTestId("browse-grid-error")).toBeInTheDocument();
  });

  it("shows an error state when fetch rejects (network failure)", async () => {
    mockFetch.mockRejectedValueOnce(new Error("network down"));

    render(<BrowseGrid />);

    expect(await screen.findByTestId("browse-grid-error")).toBeInTheDocument();
  });

  it("advances the page param when Next is clicked", async () => {
    const user = userEvent.setup();
    // total 40, limit 20 → 2 pages, so Next is enabled on page 1.
    mockFetch.mockResolvedValueOnce(
      jsonResponse(page({ items: [item({ id: "p1", title: "Page One" })], page: 1, limit: 20, total: 40 })),
    );
    mockFetch.mockResolvedValueOnce(
      jsonResponse(page({ items: [item({ id: "p2", title: "Page Two" })], page: 2, limit: 20, total: 40 })),
    );

    render(<BrowseGrid />);

    expect(await screen.findByText("Page One")).toBeInTheDocument();

    const next = screen.getByRole("button", { name: /next/i });
    await user.click(next);

    await waitFor(() => {
      const lastUrl = String(mockFetch.mock.calls[mockFetch.mock.calls.length - 1]?.[0]);
      expect(lastUrl).toContain("page=2");
    });
    expect(await screen.findByText("Page Two")).toBeInTheDocument();
  });

  it("goes back a page when Previous is clicked", async () => {
    const user = userEvent.setup();
    mockFetch.mockResolvedValueOnce(
      jsonResponse(page({ items: [item({ id: "p1", title: "Page One" })], page: 1, limit: 20, total: 40 })),
    );
    mockFetch.mockResolvedValueOnce(
      jsonResponse(page({ items: [item({ id: "p2", title: "Page Two" })], page: 2, limit: 20, total: 40 })),
    );
    mockFetch.mockResolvedValueOnce(
      jsonResponse(page({ items: [item({ id: "p1b", title: "Back To One" })], page: 1, limit: 20, total: 40 })),
    );

    render(<BrowseGrid />);
    await screen.findByText("Page One");

    await user.click(screen.getByRole("button", { name: /next/i }));
    await screen.findByText("Page Two");

    await user.click(screen.getByRole("button", { name: /previous/i }));

    await waitFor(() => {
      const lastUrl = String(mockFetch.mock.calls[mockFetch.mock.calls.length - 1]?.[0]);
      expect(lastUrl).toContain("page=1");
    });
    expect(await screen.findByText("Back To One")).toBeInTheDocument();
  });

  it("disables Previous on the first page", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse(page({ page: 1, limit: 20, total: 40 })),
    );

    render(<BrowseGrid />);

    await screen.findByTestId("product-price");
    expect(screen.getByRole("button", { name: /previous/i })).toBeDisabled();
  });

  it("disables Next on the last page", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse(page({ items: [item()], page: 1, limit: 20, total: 1 })),
    );

    render(<BrowseGrid />);

    await screen.findByTestId("product-price");
    expect(screen.getByRole("button", { name: /next/i })).toBeDisabled();
  });

  it("passes the relative signed image_url through to the card image src", async () => {
    // The route emits a RELATIVE `/images/{key}?token=…&expires=…` path; proxyUrl
    // returns it as-is so the Next.js images proxy route (app/images/[...path])
    // can serve it server-side without the browser needing to know AVSA_API_URL.
    // Asserting the non-placeholder src guards the blocker: if safeProxyUrl
    // regressed to returning "" the card would show a placeholder instead.
    const relative = "/images/red-dress.jpg?token=abc&expires=99";
    mockFetch.mockResolvedValueOnce(
      jsonResponse(page({ items: [item({ image_url: relative, title: "Red Dress" })] })),
    );

    render(<BrowseGrid />);

    const img = await screen.findByRole("img", { name: "Red Dress" });
    expect(img).toHaveAttribute("src", relative);
  });

  it("refetches the same page when Retry is clicked after a failure", async () => {
    const user = userEvent.setup();
    // First fetch rejects → error state with the Retry affordance.
    mockFetch.mockRejectedValueOnce(new Error("network down"));
    // Retry must fire a SECOND fetch; this one resolves → grid renders.
    mockFetch.mockResolvedValueOnce(
      jsonResponse(page({ items: [item({ id: "after-retry", title: "After Retry" })] })),
    );

    render(<BrowseGrid />);

    // The error recovery affordance appears after the first (failed) fetch.
    const retry = await screen.findByRole("button", { name: /retry/i });
    expect(mockFetch).toHaveBeenCalledTimes(1);

    await user.click(retry);

    // A real refetch must fire (the no-op `setPage(p => p)` bug would not).
    await waitFor(() => expect(mockFetch).toHaveBeenCalledTimes(2));
    expect(await screen.findByText("After Retry")).toBeInTheDocument();
  });

  it("exposes the grid as a list for accessibility", async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse(page({ items: [item({ id: "a" }), item({ id: "b" })], total: 2 })),
    );

    render(<BrowseGrid />);

    const list = await screen.findByRole("list");
    expect(within(list).getAllByRole("listitem")).toHaveLength(2);
  });
});
