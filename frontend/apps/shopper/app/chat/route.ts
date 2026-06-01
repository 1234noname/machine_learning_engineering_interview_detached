import type { NextRequest } from "next/server";
import { chatRequestsTotal, observeChatDuration } from "../lib/metrics";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

/**
 * POST /chat — SSE proxy to the AVSA Python API.
 *
 * Forwards the browser's multipart FormData (image + text) to
 * $AVSA_API_URL/chat and streams server-sent events back to the client.
 * Forwards X-Resume-Conversation-Id upstream so multi-turn resume works (the
 * API carries prior_result_ids across turns of the same conversation, so a
 * follow-up turn excludes already-shown results), and surfaces the response's
 * X-Conversation-Id back to the browser. The API is never directly visible to
 * clients.
 *
 * This route is what makes `fetch("/chat", ...)` in the client components work
 * in all deployment configurations without CORS or cross-origin issues.
 */
export async function POST(request: NextRequest): Promise<Response> {
  const t0 = performance.now();
  const apiUrl = process.env.AVSA_API_URL ?? "http://localhost:8080";

  let formData: FormData;
  try {
    formData = await request.formData();
  } catch {
    chatRequestsTotal("bad_request");
    observeChatDuration((performance.now() - t0) / 1000);
    return new Response("Bad request: invalid multipart body", { status: 400 });
  }

  const upstreamHeaders: Record<string, string> = {};
  // Forward the resume header so a follow-up turn resumes the SAME conversation
  // (the API carries prior_result_ids → the turn excludes already-shown results).
  // Resume is keyed ONLY off X-Resume-Conversation-Id; the API deliberately
  // ignores a client X-Conversation-Id as a session-fixation guard, so
  // there is no point forwarding that one.
  const resumeConversationId = request.headers.get("x-resume-conversation-id");
  if (resumeConversationId) {
    upstreamHeaders["x-resume-conversation-id"] = resumeConversationId;
  }
  const forwardedFor = request.headers.get("x-forwarded-for");
  if (forwardedFor) upstreamHeaders["x-forwarded-for"] = forwardedFor;

  let upstream: Response;
  try {
    upstream = await fetch(`${apiUrl}/chat`, {
      method: "POST",
      body: formData,
      headers: upstreamHeaders,
    });
  } catch {
    chatRequestsTotal("upstream_error");
    observeChatDuration((performance.now() - t0) / 1000);
    return new Response("Service unavailable", { status: 503 });
  }

  const responseHeaders = new Headers({
    "cache-control": "no-cache, no-store",
    "x-accel-buffering": "no",
  });

  const ct = upstream.headers.get("content-type");
  if (ct) responseHeaders.set("content-type", ct);

  const upstreamConvId = upstream.headers.get("x-conversation-id");
  if (upstreamConvId) responseHeaders.set("x-conversation-id", upstreamConvId);

  const outcome = upstream.ok ? "ok" : "upstream_error";
  chatRequestsTotal(outcome);
  observeChatDuration((performance.now() - t0) / 1000);

  return new Response(upstream.body, {
    status: upstream.status,
    headers: responseHeaders,
  });
}
