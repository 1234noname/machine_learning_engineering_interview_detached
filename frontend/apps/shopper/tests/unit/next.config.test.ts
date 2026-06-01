/**
 * Unit tests for next.config image remote-patterns.
 *
 * The API proxy serves images at `http://{API_HOST}/images/{path}?token=...`.
 * For Next.js's <Image> to render those URLs the host must appear in the
 * `images.remotePatterns` allowlist. These tests assert the SHAPE of the
 * patterns array `buildNextConfig()` returns — directly, without re-implementing
 * Next's glob matcher in the test (the previous `patternAdmitsUrl` helper was a
 * test-private matcher that risked drifting from Next's real behaviour).
 *
 * The single source of truth for the allowlist is `imageRemotePatterns.mjs`,
 * shared by `buildNextConfig` and `imageProxy.proxyUrl` — so the config and the
 * runtime validator can never drift apart.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

type RemotePattern = {
  protocol?: string;
  hostname: string;
  port?: string;
  pathname?: string;
};

type NextImagesConfig = {
  remotePatterns?: RemotePattern[];
};

type NextConfigShape = {
  images?: NextImagesConfig;
  [key: string]: unknown;
};

type BuildNextConfig = () => NextConfigShape;

async function loadBuildNextConfig(): Promise<BuildNextConfig> {
  // `@vite-ignore` + computed specifier keeps a missing module from failing
  // the whole test file at transform time.
  const specifier = "@/lib/nextConfig";
  const mod = (await import(/* @vite-ignore */ specifier)) as {
    buildNextConfig?: BuildNextConfig;
  };
  if (typeof mod.buildNextConfig !== "function") {
    throw new Error(
      "@/lib/nextConfig must export buildNextConfig as a function",
    );
  }
  return mod.buildNextConfig;
}

describe("next.config — images.remotePatterns", () => {
  beforeEach(() => {
    vi.unstubAllEnvs();
  });
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("includes a pattern with pathname /images/**", async () => {
    const build = await loadBuildNextConfig();
    const config = build();
    const patterns = config.images?.remotePatterns ?? [];
    expect(patterns.length).toBeGreaterThan(0);
    expect(patterns.some((p) => p.pathname === "/images/**")).toBe(true);
  });

  it("default config includes the localhost:8080 dev API host", async () => {
    const build = await loadBuildNextConfig();
    const config = build();
    const patterns = config.images?.remotePatterns ?? [];
    expect(
      patterns.some(
        (p) =>
          p.hostname === "localhost" &&
          p.port === "8080" &&
          p.pathname === "/images/**",
      ),
    ).toBe(true);
  });

  it("every pattern is scoped to the /images/** subtree", async () => {
    // Guards against accidentally widening the allowlist (e.g. /** or /api/**)
    // — a broader remote-pattern would let <Image> fetch from any path.
    const build = await loadBuildNextConfig();
    const config = build();
    const patterns = config.images?.remotePatterns ?? [];
    for (const p of patterns) {
      expect(p.pathname).toBe("/images/**");
    }
  });

  it("AVSA_API_URL env override adds the configured host while preserving the localhost dev entry", async () => {
    vi.stubEnv("AVSA_API_URL", "https://images.example.com");
    const build = await loadBuildNextConfig();
    const config = build();
    const patterns = config.images?.remotePatterns ?? [];
    expect(
      patterns.some(
        (p) =>
          p.hostname === "images.example.com" && p.pathname === "/images/**",
      ),
    ).toBe(true);
    // The override MUST NOT silently drop the default localhost entry — dev
    // workflows still need it.
    expect(
      patterns.some((p) => p.hostname === "localhost" && p.port === "8080"),
    ).toBe(true);
  });
});
