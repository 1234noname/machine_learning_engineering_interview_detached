/**
 * Build the Next.js config object.
 *
 * The `images.remotePatterns` allowlist controls which hosts Next.js's <Image>
 * will fetch from. We admit the localhost dev API by default and the configured
 * `AVSA_API_URL` host when set. The host policy itself lives in
 * `imageRemotePatterns.mjs` (the single source of truth shared with
 * `next.config.js` and `imageProxy.ts`) so the allowlist can never drift from
 * the proxy's validation.
 *
 * Env is read per-call (not at module load) so tests can drive different
 * `AVSA_API_URL` values via `vi.stubEnv` without module-init caching.
 */
import type { NextConfig } from "next";

import { buildRemotePatterns } from "./imageRemotePatterns.mjs";

// Next's remote-patterns array type, derived from NextConfig so we don't deep
// import from `next/dist/...`. `NonNullable` strips the `| undefined` that
// `exactOptionalPropertyTypes` would otherwise reject — the shared builder
// always returns an array.
type RemotePatterns = NonNullable<
  NonNullable<NextConfig["images"]>["remotePatterns"]
>;

export function buildNextConfig(): NextConfig {
  // The shared `.mjs` returns structurally-correct patterns typed with a plain
  // `string` protocol (JSDoc can't express Next's `"http" | "https"` literal
  // union). Narrow once here, at the TS boundary, rather than weakening the
  // shared module's portability.
  const remotePatterns = buildRemotePatterns(
    process.env["AVSA_API_URL"],
  ) as RemotePatterns;

  return {
    output: "standalone",
    images: { remotePatterns },
  };
}
