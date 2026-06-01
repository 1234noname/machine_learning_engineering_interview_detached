/**
 * Unit tests for the SSE frame parser (lib/consumeSse).
 *
 * The parser was exercised only transitively via ChatInput.test before — this
 * suite tests it directly so the chunk-boundary buffering (a single SSE frame
 * arriving across two read() chunks) is pinned independently of the React
 * component. Malformed frames must be silently skipped, not crash the stream.
 */
import { describe, expect, it } from "vitest";
import { consumeSse, type SseEvent } from "../../app/lib/consumeSse";

function streamFromChunks(chunks: string[]): ReadableStream<Uint8Array> {
  const enc = new TextEncoder();
  return new ReadableStream({
    start(c) {
      for (const chunk of chunks) c.enqueue(enc.encode(chunk));
      c.close();
    },
  });
}

describe("consumeSse", () => {
  it("parses each data: JSON frame into one onEvent call", async () => {
    const events: SseEvent[] = [];
    await consumeSse(
      streamFromChunks([
        `data: ${JSON.stringify({ type: "a", n: 1 })}\n`,
        `data: ${JSON.stringify({ type: "b", n: 2 })}\n`,
      ]),
      (e) => events.push(e),
    );
    expect(events).toEqual([
      { type: "a", n: 1 },
      { type: "b", n: 2 },
    ]);
  });

  it("buffers a frame split across two chunks (the read-boundary contract)", async () => {
    // The whole reason the parser keeps a string buffer: an SSE frame may
    // arrive across multiple TextDecoder reads. If the buffer were dropped on
    // chunk boundary the second half would orphan and the frame would never
    // fire — this test pins that.
    const payload = JSON.stringify({ type: "product_card", card: { id: "x" } });
    const full = `data: ${payload}\n`;
    const cut = Math.floor(full.length / 2);
    const events: SseEvent[] = [];
    await consumeSse(
      streamFromChunks([full.slice(0, cut), full.slice(cut)]),
      (e) => events.push(e),
    );
    expect(events).toEqual([{ type: "product_card", card: { id: "x" } }]);
  });

  it("skips a malformed JSON frame without crashing the stream", async () => {
    const events: SseEvent[] = [];
    await consumeSse(
      streamFromChunks([
        `data: {not valid json\n`,
        `data: ${JSON.stringify({ type: "ok" })}\n`,
      ]),
      (e) => events.push(e),
    );
    // The malformed frame is silently skipped; the well-formed one still fires.
    expect(events).toEqual([{ type: "ok" }]);
  });

  it("ignores non-data lines (comments, event:, blank)", async () => {
    const events: SseEvent[] = [];
    await consumeSse(
      streamFromChunks([
        `: comment line\n`,
        `event: ping\n`,
        `\n`,
        `data: ${JSON.stringify({ type: "real" })}\n`,
      ]),
      (e) => events.push(e),
    );
    expect(events).toEqual([{ type: "real" }]);
  });

  it("returns cleanly on an empty stream", async () => {
    const events: SseEvent[] = [];
    await consumeSse(streamFromChunks([]), (e) => events.push(e));
    expect(events).toEqual([]);
  });
});
