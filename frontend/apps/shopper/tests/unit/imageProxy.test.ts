/**
 * Unit tests for the image-proxy URL helper.
 *
 *  (Phase 2b): the API serves images via
 *   GET http://{API_HOST}/images/{path}?token=...&expires=...
 *
 * `proxyUrl(rawImageUrl)` is the seam that (browse grid) and the
 * product card will both call to convert an `image_url` from the catalog
 * into the final URL to feed to <Image>. The helper:
 *   - Pass-through when the URL is already an absolute /images/** URL on
 *     the configured API host (the common case in Phase 2b: the seeder
 *     stores fully-signed URLs in catalog.products.image_url).
 *   - Rejects URLs whose pathname isn't /images/** (defence in depth —
 *     refuses to "render" a URL the remote-pattern wouldn't admit).
 *   - When AVSA_API_URL is set, treats that host as canonical and
 *     pass-throughs URLs on it.
 *
 * Why have this seam at all if it's mostly pass-through? Cheap to add now,
 * expensive to retro-fit later: when we add Cloudflare Images / a CDN
 * (post-MVP) the rewrite lives in one place. Phase 2b YAGNI is satisfied
 * because the rejection branch IS a real behaviour (CSP/remote-pattern
 * coupling), not speculative.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

type ProxyUrlFn = (raw: string) => string;

async function loadProxyUrl(): Promise<ProxyUrlFn> {
  // The `@vite-ignore` comment + computed specifier prevents Vite from
  // statically analysing the import path at parse time. Without it, a
  // missing module would error at file transform time and fail every
  // test in the file before any `it()` runs. We want per-test assertion
  // failures so each test reports its own intent.
  const specifier = "@/lib/imageProxy";
  try {
    const mod = (await import(/* @vite-ignore */ specifier)) as {
      proxyUrl?: ProxyUrlFn;
    };
    if (typeof mod.proxyUrl !== "function") {
      throw new Error(
        "@/lib/imageProxy exists but does not export `proxyUrl` as a " +
          "function — expected during 2A-i pre-implementation",
      );
    }
    return mod.proxyUrl;
  } catch (err) {
    throw new Error(
      "@/lib/imageProxy.proxyUrl not implemented yet — expected during " +
        "2A-i pre-implementation. Underlying: " +
        (err instanceof Error ? err.message : String(err)),
    );
  }
}

describe("imageProxy.proxyUrl", () => {
  beforeEach(() => {
    vi.unstubAllEnvs();
  });
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("passes through an absolute /images/** URL on the default API host unchanged", async () => {
    const proxyUrl = await loadProxyUrl();
    const input =
      "http://localhost:8080/images/abc123.jpg?token=deadbeef&expires=1735689600";
    expect(proxyUrl(input)).toBe(input);
  });

  it("passes through a nested /images/sub/dir/photo.png URL unchanged", async () => {
    const proxyUrl = await loadProxyUrl();
    const input = "http://localhost:8080/images/sub/dir/photo.png";
    expect(proxyUrl(input)).toBe(input);
  });

  it("throws on a URL whose pathname is not /images/**", async () => {
    const proxyUrl = await loadProxyUrl();
    expect(() =>
      proxyUrl("http://localhost:8080/admin/secret.png"),
    ).toThrow();
    expect(() => proxyUrl("http://localhost:8080/chat")).toThrow();
  });

  it("throws on a URL whose host is neither localhost:8080 nor AVSA_API_URL", async () => {
    const proxyUrl = await loadProxyUrl();
    // Default config — only localhost:8080 is canonical.
    expect(() =>
      proxyUrl("https://evil.example.com/images/steal.jpg"),
    ).toThrow();
  });

  it("throws on a non-URL input string (fail fast at the boundary)", async () => {
    const proxyUrl = await loadProxyUrl();
    expect(() => proxyUrl("not-a-url")).toThrow();
    expect(() => proxyUrl("")).toThrow();
  });

  it("with AVSA_API_URL set, passes through URLs on that host", async () => {
    vi.stubEnv("AVSA_API_URL", "https://images.example.com");
    const proxyUrl = await loadProxyUrl();
    const input =
      "https://images.example.com/images/abc123.jpg?token=t&expires=1";
    expect(proxyUrl(input)).toBe(input);
  });

  it("passes through an absolute external CDN URL whose path is not /images/**", async () => {
    const proxyUrl = await loadProxyUrl();
    // A catalog item whose image_url is a direct CDN link (not an /images/ proxy
    // path) must be passed through unchanged so the browser can render it.
    const cdn =
      "https://cdnb.lystit.com/520/650/n/photos/ec44-2015/01/12/pinko-black-normal.jpeg";
    expect(proxyUrl(cdn)).toBe(cdn);
  });

  it("with AVSA_API_URL set, still accepts localhost:8080 (dev workflow)", async () => {
    vi.stubEnv("AVSA_API_URL", "https://images.example.com");
    const proxyUrl = await loadProxyUrl();
    const input = "http://localhost:8080/images/abc123.jpg";
    expect(proxyUrl(input)).toBe(input);
  });

  it("returns a relative /images/… path unchanged (catalog route output)", async () => {
    // The catalog API emits relative /images/{key}?token=…&expires=… paths.
    // proxyUrl must return them as-is so the Next.js images proxy route at
    // app/images/[...path] can serve them — no absolute resolution needed
    // (AVSA_API_URL is server-only and undefined in the browser).
    const proxyUrl = await loadProxyUrl();
    const input =
      "/images/fashion200k/images/women/dresses/100/100_1.jpeg.jpg?token=abc&expires=9999999999";
    expect(proxyUrl(input)).toBe(input);
  });

  it("returns a plain relative /images/ path without query params unchanged", async () => {
    const proxyUrl = await loadProxyUrl();
    const input = "/images/fashion200k/images/women/tops/200/200_2.jpeg.jpg";
    expect(proxyUrl(input)).toBe(input);
  });
});
