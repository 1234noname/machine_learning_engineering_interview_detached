/**
 * SSE frame parser — shared utility used by ChatInput.
 *
 * Consume a ReadableStream of SSE frames, calling onEvent for each parsed
 * JSON data line. Malformed frames are silently skipped; the stream continues.
 *
 * Exported so unit tests can drive it directly without a React component.
 */

export interface SseEvent {
  type: string;
  [key: string]: unknown;
}

export async function consumeSse(
  body: ReadableStream<Uint8Array>,
  onEvent: (event: SseEvent) => void,
): Promise<void> {
  const decoder = new TextDecoder();
  const reader = body.getReader();
  let buffer = "";

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";
      for (const line of lines) {
        if (line.startsWith("data: ")) {
          try {
            const event = JSON.parse(line.slice(6)) as SseEvent;
            onEvent(event);
          } catch {
            // Malformed SSE frame — skip and continue; don't crash the stream.
          }
        }
      }
    }
  } finally {
    reader.releaseLock();
  }
}
