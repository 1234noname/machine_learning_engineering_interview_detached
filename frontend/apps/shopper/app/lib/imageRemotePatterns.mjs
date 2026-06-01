/**
 * Canonical image-host policy — the SINGLE SOURCE OF TRUTH for which hosts may
 * serve catalog images (Issue #064).
 *
 * This lives in a plain `.mjs` so BOTH consumers can import it without a
 * CJS/ESM transpile step:
 *   - `next.config.js` (ESM under `"type": "module"`) imports it to build the
 *     `images.remotePatterns` allowlist that Next.js's <Image> enforces.
 *   - `app/lib/nextConfig.ts` and `app/lib/imageProxy.ts` import it so the
 *     unit tests and the runtime proxy share the exact same host policy.
 *
 * Keeping the logic here (not duplicated in `next.config.js`) means there is
 * one place to change when the hosting story evolves — no drift between the
 * remote-pattern allowlist and the proxy's validation.
 */

/**
 * The default dev API host. The API proxy (#064 backend slice) serves images at
 * `http://localhost:8080/images/{path}?token=...&expires=...` in local dev.
 * Always admitted so dev workflows render real images without extra config.
 */
export const DEFAULT_API_URL = "http://localhost:8080";

/** Only the `/images/**` subtree is served — never `/chat`, `/admin`, etc. */
const IMAGE_PATHNAME = "/images/**";

/**
 * Turn an absolute API URL into a Next.js `remotePattern` entry scoped to the
 * `/images/**` subtree. Returns `null` for an unparseable URL so callers can
 * skip a malformed override rather than crash the whole config.
 *
 * @param {string} apiUrl - a full URL, e.g. "http://localhost:8080".
 * @returns {{protocol: string, hostname: string, port: string, pathname: string} | null}
 */
export function remotePatternForUrl(apiUrl) {
  let url;
  try {
    url = new URL(apiUrl);
  } catch {
    return null;
  }
  return {
    protocol: url.protocol.replace(/:$/, ""),
    hostname: url.hostname,
    // URL.port is "" for a protocol's default port; Next.js treats "" the same.
    port: url.port,
    pathname: IMAGE_PATHNAME,
  };
}

/**
 * Build the `images.remotePatterns` allowlist: always the localhost default,
 * plus the configured `AVSA_API_URL` host when it differs.
 *
 * @param {string | undefined} apiUrl - typically `process.env.AVSA_API_URL`.
 * @returns {Array<{protocol: string, hostname: string, port: string, pathname: string}>}
 */
export function buildRemotePatterns(apiUrl) {
  const defaultPattern = remotePatternForUrl(DEFAULT_API_URL);
  const patterns = defaultPattern ? [defaultPattern] : [];

  if (apiUrl && apiUrl !== DEFAULT_API_URL) {
    const override = remotePatternForUrl(apiUrl);
    if (override && !samePattern(override, defaultPattern)) {
      patterns.push(override);
    }
  }
  return patterns;
}

/**
 * Set of canonical `protocol://hostname[:port]` origins that may serve images.
 * Used by the proxy to validate a raw URL's host. Always includes the localhost
 * default; adds the `AVSA_API_URL` origin when configured.
 *
 * @param {string | undefined} apiUrl - typically `process.env.AVSA_API_URL`.
 * @returns {Set<string>}
 */
export function canonicalOrigins(apiUrl) {
  const origins = new Set([originOf(DEFAULT_API_URL)].filter(Boolean));
  if (apiUrl) {
    const origin = originOf(apiUrl);
    if (origin) origins.add(origin);
  }
  return origins;
}

/** The image subtree prefix every served path must start with. */
export const IMAGE_PATH_PREFIX = "/images/";

function originOf(apiUrl) {
  try {
    return new URL(apiUrl).origin;
  } catch {
    return null;
  }
}

function samePattern(a, b) {
  return (
    b !== null &&
    a.protocol === b.protocol &&
    a.hostname === b.hostname &&
    a.port === b.port
  );
}
