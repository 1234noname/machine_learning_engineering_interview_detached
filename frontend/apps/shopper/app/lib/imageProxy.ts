/**
 * Image-URL validation seam.
 *
 * `proxyUrl` converts a catalog `image_url` into the URL fed to <Image>.
 * When a CDN / Cloudflare Images rewrite lands (post-MVP) the rewrite lives
 * here only.
 *
 * The catalog route emits a read-time-signed RELATIVE `/images/{key}?token=…`
 * proxy path. Relative paths are returned as-is: app/images/[...path]/route.ts
 * proxies them server-side to AVSA_API_URL so the browser never needs to know
 * the API host (AVSA_API_URL is server-only — no NEXT_PUBLIC_ prefix).
 *
 * Absolute URLs (e.g. a direct CDN link stored as image_url) are validated:
 * origin must be the canonical API host and pathname must be under /images/.
 * An absolute URL whose path is NOT /images/** is passed through as a CDN link.
 */
import {
  DEFAULT_API_URL,
  IMAGE_PATH_PREFIX,
  canonicalOrigins,
} from "./imageRemotePatterns.mjs";

export function proxyUrl(imageUrl: string): string {
  // Relative /images/… proxy path: return as-is. The Next.js route at
  // app/images/[...path]/route.ts proxies the request server-side to
  // AVSA_API_URL so the browser fetches from the shopper origin only.
  if (imageUrl.startsWith(IMAGE_PATH_PREFIX)) {
    return imageUrl;
  }

  // Absolute URL — validate host + path.
  const apiOrigin = process.env["AVSA_API_URL"] ?? DEFAULT_API_URL;

  let url: URL;
  try {
    url = new URL(imageUrl, apiOrigin);
  } catch {
    throw new Error(`proxyUrl: not a valid image URL: ${imageUrl}`);
  }

  const allowed = canonicalOrigins(process.env["AVSA_API_URL"]);
  if (!allowed.has(url.origin)) {
    // External absolute CDN URL whose path is NOT /images/** — pass through.
    const isAbsoluteInput =
      imageUrl.startsWith("http://") || imageUrl.startsWith("https://");
    if (isAbsoluteInput && !url.pathname.startsWith(IMAGE_PATH_PREFIX)) {
      return imageUrl;
    }
    throw new Error(
      `proxyUrl: host ${url.origin} is not a canonical image host ` +
        `(allowed: ${[...allowed].join(", ")})`,
    );
  }

  if (!url.pathname.startsWith(IMAGE_PATH_PREFIX)) {
    throw new Error(
      `proxyUrl: pathname ${url.pathname} is not under ${IMAGE_PATH_PREFIX}`,
    );
  }

  return url.toString();
}

/**
 * `proxyUrl` wrapped to never throw: a URL the seam rejects — or an
 * empty/missing value — resolves to `""` so the caller renders
 * ProductCard's placeholder. Used by every ProductCard caller (browse
 * grid + chat results).
 */
export function safeProxyUrl(imageUrl: string): string {
  if (!imageUrl) return "";
  try {
    return proxyUrl(imageUrl);
  } catch {
    return "";
  }
}
