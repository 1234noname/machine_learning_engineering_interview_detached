import { buildRemotePatterns } from "./app/lib/imageRemotePatterns.mjs";

/**
 * @type {import('next').NextConfig}
 *
 * `images.remotePatterns` is built from the shared host policy in
 * `app/lib/imageRemotePatterns.mjs` — the single source of truth also used by
 * `app/lib/nextConfig.ts` (tested) and `app/lib/imageProxy.ts`. Keeping the
 * policy in one module means the allowlist Next.js enforces can never drift
 * from the proxy's validation. See Issue #064.
 */
const nextConfig = {
  output: "standalone",
  images: {
    remotePatterns: buildRemotePatterns(process.env.AVSA_API_URL),
  },
};

export default nextConfig;
