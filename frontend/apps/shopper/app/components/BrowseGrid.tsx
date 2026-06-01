"use client";

import { useEffect, useState } from "react";
import type { components } from "@avsa/shared";
import ProductCard from "./ProductCard";
import { safeProxyUrl } from "@/lib/imageProxy";

type CatalogPage = components["schemas"]["CatalogPage"];

type Status = "loading" | "ready" | "error";

/**
 * BrowseGrid — the shopper landing's catalog browse surface.
 *
 * Fetches a stably-ordered, paginated page of the real catalog from the local
 * `GET /api/catalog` proxy (which forwards to the Python API's `GET /catalog`)
 * and renders it as a responsive grid of the existing ProductCard. Modest by
 * design — proves the catalog is real and explorable without going through
 * chat. Coexists with the chat surface; it does not replace it.
 *
 * Contract notes (specs/api/chat.openapi.yaml#CatalogItem):
 *   - `price_cents` is a ZAR-denominated integer of cents → divide by 100 for
 *     the rand amount ProductCard formats.
 *   - `image_url` is a read-time-signed, RELATIVE `/images/{key}?token=…`
 *     proxy path; the `proxyUrl` seam resolves it against the canonical API
 *     origin and validates it (host + `/images/` path) before <Image> renders.
 *   - the response `limit` is the CLAMPED effective page size; pagination is
 *     derived from the echoed `page`/`limit`/`total`, not the requested value.
 */
export default function BrowseGrid() {
  const [page, setPage] = useState(1);
  const [data, setData] = useState<CatalogPage | null>(null);
  const [status, setStatus] = useState<Status>("loading");
  // Bumped by Retry to force a refetch of the SAME page. Setting `page` to its
  // current value would be a React no-op (no re-render, no effect re-run), so
  // the error-recovery affordance needs a distinct, always-changing dep.
  const [reloadNonce, setReloadNonce] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setStatus("loading");

    fetch(`/api/catalog?page=${page}`, { cache: "no-store" })
      .then(async (res) => {
        if (!res.ok) {
          if (!cancelled) setStatus("error");
          return;
        }
        const body = (await res.json()) as CatalogPage;
        if (!cancelled) {
          setData(body);
          setStatus("ready");
        }
      })
      .catch(() => {
        if (!cancelled) setStatus("error");
      });

    return () => {
      cancelled = true;
    };
  }, [page, reloadNonce]);

  // Effective page size is the clamped value the server echoes back; fall back
  // to the requested page while the first response is still in flight.
  const limit = data?.limit ?? 0;
  const total = data?.total ?? 0;
  const totalPages = limit > 0 ? Math.max(1, Math.ceil(total / limit)) : 1;
  const currentPage = data?.page ?? page;
  const hasPrev = currentPage > 1;
  const hasNext = currentPage < totalPages;

  return (
    <section className="browse-grid" aria-label="Browse catalog">
      <div className="browse-grid__header">
        <h2 className="browse-grid__heading">Browse the catalog</h2>
        {status === "ready" && total > 0 && (
          <span className="browse-grid__count">{total} products</span>
        )}
      </div>

      {status === "loading" && (
        <p
          className="browse-grid__loading"
          data-testid="browse-grid-loading"
          aria-busy="true"
        >
          Loading products…
        </p>
      )}

      {status === "error" && (
        <div className="browse-grid__error" data-testid="browse-grid-error" role="alert">
          <p>Couldn&apos;t load the catalog right now.</p>
          <button
            type="button"
            className="browse-grid__retry"
            onClick={() => setReloadNonce((n) => n + 1)}
          >
            Retry
          </button>
        </div>
      )}

      {status === "ready" && data && data.items.length === 0 && (
        <p className="browse-grid__empty" data-testid="browse-grid-empty">
          No products on this page.
        </p>
      )}

      {status === "ready" && data && data.items.length > 0 && (
        <div className="browse-grid__items" role="list">
          {data.items.map((p) => (
            <div key={p.id} role="listitem">
              <ProductCard
                title={p.title}
                category={p.category}
                price={p.price_cents / 100}
                imageUrl={safeProxyUrl(p.image_url)}
              />
            </div>
          ))}
        </div>
      )}

      {status === "ready" && data && (
        <nav className="browse-grid__pagination" aria-label="Catalog pagination">
          <button
            type="button"
            className="browse-grid__page-btn"
            onClick={() => setPage((p) => Math.max(1, p - 1))}
            disabled={!hasPrev}
          >
            Previous
          </button>
          <span className="browse-grid__page-status" aria-live="polite">
            Page {currentPage} of {totalPages}
          </span>
          <button
            type="button"
            className="browse-grid__page-btn"
            onClick={() => setPage((p) => p + 1)}
            disabled={!hasNext}
          >
            Next
          </button>
        </nav>
      )}
    </section>
  );
}
