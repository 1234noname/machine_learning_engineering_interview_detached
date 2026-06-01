"use client";

import { useEffect, useRef, useState } from "react";
import type { components } from "@avsa/shared";
import { consumeSse } from "@/lib/consumeSse";

export type ChatProductCard = components["schemas"]["ProductCard"];

const MAX_FILE_SIZE = 10 * 1024 * 1024; // 10 MB
const ACCEPTED_TYPES = "image/jpeg,image/png,image/webp,image/heic";

interface Props {
  onProductCard: (card: ChatProductCard) => void;
  /**
   * Called at submit time, before the SSE stream starts, so the page can clear
   * the previous result set — a new search (changed text or images) replaces
   * the results rather than appending to them.
   */
  onSearchStart?: () => void;
  /** Called when a network or stream error occurs during chat submission. */
  onError?: (message: string) => void;
  /**
   * Called once per chat submission with the X-Conversation-Id response header.
   */
  onSessionId?: (id: string) => void;
  /** When provided, sent as X-Resume-Conversation-Id to resume a conversation. */
  resumeConversationId?: string | null;
}

/** One staged upload: the file plus an object URL for its thumbnail preview. */
interface UploadItem {
  id: string;
  file: File;
  url: string;
}

function UploadIcon({ className }: { className?: string }) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      fill="none"
      viewBox="0 0 24 24"
      strokeWidth={1.5}
      stroke="currentColor"
      className={className}
      aria-hidden="true"
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M3 16.5v2.25A2.25 2.25 0 0 0 5.25 21h13.5A2.25 2.25 0 0 0 21 18.75V16.5m-13.5-9L12 3m0 0 4.5 4.5M12 3v13.5"
      />
    </svg>
  );
}

function ArrowIcon() {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 20 20"
      fill="currentColor"
      width={16}
      height={16}
      aria-hidden="true"
    >
      <path
        fillRule="evenodd"
        d="M3 10a.75.75 0 0 1 .75-.75h10.638L10.23 5.29a.75.75 0 1 1 1.04-1.08l5.5 5.25a.75.75 0 0 1 0 1.08l-5.5 5.25a.75.75 0 1 1-1.04-1.08l4.158-3.96H3.75A.75.75 0 0 1 3 10Z"
        clipRule="evenodd"
      />
    </svg>
  );
}

let _uid = 0;
function nextId(): string {
  _uid += 1;
  return `upload-${_uid}-${Date.now()}`;
}

export default function ChatInput({
  onProductCard,
  onSearchStart,
  onError,
  onSessionId,
  resumeConversationId,
}: Props) {
  const [text, setText] = useState("");
  const [items, setItems] = useState<UploadItem[]>([]);
  const [sizeError, setSizeError] = useState<string | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  const [isStreaming, setIsStreaming] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const abortRef = useRef<AbortController | null>(null);

  // Revoke all object URLs on unmount so previews don't leak.
  useEffect(() => {
    return () => {
      setItems((current) => {
        current.forEach((it) => URL.revokeObjectURL(it.url));
        return current;
      });
    };
  }, []);

  function addFiles(candidates: File[]) {
    const accepted: UploadItem[] = [];
    let rejected = false;
    for (const file of candidates) {
      if (file.size > MAX_FILE_SIZE) {
        rejected = true;
        continue;
      }
      accepted.push({ id: nextId(), file, url: URL.createObjectURL(file) });
    }
    setSizeError(rejected ? "Each image must be under 10 MB" : null);
    if (accepted.length > 0) {
      setItems((prev) => [...prev, ...accepted]);
    }
  }

  function removeItem(id: string) {
    setItems((prev) => {
      const target = prev.find((it) => it.id === id);
      if (target) URL.revokeObjectURL(target.url);
      return prev.filter((it) => it.id !== id);
    });
  }

  function handleFileChange(e: React.ChangeEvent<HTMLInputElement>) {
    const picked = e.target.files;
    if (picked && picked.length > 0) addFiles(Array.from(picked));
    // Reset so selecting the same file again still fires onChange.
    e.target.value = "";
  }

  function handleDragOver(e: React.DragEvent<HTMLDivElement>) {
    e.preventDefault();
    setIsDragging(true);
  }

  function handleDragLeave() {
    setIsDragging(false);
  }

  function handleDrop(e: React.DragEvent<HTMLDivElement>) {
    e.preventDefault();
    setIsDragging(false);
    const dropped = Array.from(e.dataTransfer.files);
    if (dropped.length > 0) addFiles(dropped);
  }

  async function handlePaste(e: React.ClipboardEvent<HTMLTextAreaElement>) {
    const pasted = e.clipboardData.getData("text");
    if (pasted.startsWith("http")) {
      e.preventDefault();
      try {
        const res = await fetch(pasted);
        const blob = await res.blob();
        const filename = pasted.split("/").pop() ?? "pasted-image";
        addFiles([new File([blob], filename, { type: blob.type })]);
      } catch {
        // If the URL fetch fails, fall through to normal paste behaviour.
      }
    }
  }

  async function handleSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (items.length === 0 && !text) return;

    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    const body = new FormData();
    // Send every staged image under the repeated `image` field; the API combines
    // them (mean-pooled embedding) into one query.
    for (const it of items) body.append("image", it.file);
    body.append("text", text);

    const fetchHeaders: Record<string, string> = { Accept: "text/event-stream" };
    if (resumeConversationId) {
      fetchHeaders["X-Resume-Conversation-Id"] = resumeConversationId;
    }

    onSearchStart?.();
    setIsStreaming(true);

    try {
      const res = await fetch("/chat", {
        method: "POST",
        body,
        headers: fetchHeaders,
        signal: controller.signal,
      });

      if (!res.ok || !res.body) {
        throw new Error(`POST /chat failed with status ${res.status}`);
      }

      const conversationId = res.headers.get("X-Conversation-Id") ?? "";
      if (conversationId && onSessionId) {
        onSessionId(conversationId);
      }

      await consumeSse(res.body, (event) => {
        if (event.type === "product_card") {
          const card = event["card"] as ChatProductCard | undefined;
          if (card) onProductCard(card);
        }
      });
    } catch (err: unknown) {
      if (err instanceof Error && err.name !== "AbortError") {
        onError?.(`Error: ${err.message}`);
      }
    } finally {
      setIsStreaming(false);
    }
  }

  const hasImages = items.length > 0;

  return (
    <form onSubmit={(e) => void handleSubmit(e)} aria-label="Image search form">
      {/* Hidden file input (multiple) */}
      <label htmlFor="chat-image-upload" className="sr-only">
        Upload images
      </label>
      <input
        id="chat-image-upload"
        ref={fileInputRef}
        type="file"
        accept={ACCEPTED_TYPES}
        multiple
        onChange={handleFileChange}
        aria-label="Upload images"
        className="sr-only"
        tabIndex={-1}
      />

      {/* Drop zone / thumbnail tray */}
      <div
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        onDrop={handleDrop}
        className={`chat-input__dropzone${isDragging ? " chat-input__dropzone--dragging" : ""}${hasImages ? " chat-input__dropzone--has-images" : ""}`}
        aria-label="Image upload area"
      >
        {hasImages ? (
          <div className="chat-input__thumbs" role="list" aria-label="Selected images">
            {items.map((it) => (
              <div
                key={it.id}
                className="chat-input__thumb"
                role="listitem"
                data-testid="upload-thumb"
              >
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img
                  src={it.url}
                  alt={it.file.name}
                  className="chat-input__thumb-img"
                />
                <button
                  type="button"
                  className="chat-input__thumb-remove"
                  aria-label={`Remove ${it.file.name}`}
                  onClick={() => removeItem(it.id)}
                >
                  ✕
                </button>
              </div>
            ))}
            <button
              type="button"
              className="chat-input__thumb-add"
              aria-label="Add another image"
              onClick={() => fileInputRef.current?.click()}
            >
              <UploadIcon className="chat-input__upload-icon" />
              <span>Add</span>
            </button>
          </div>
        ) : (
          <button
            type="button"
            className="chat-input__dropzone-trigger"
            onClick={() => fileInputRef.current?.click()}
            aria-label="Drop photos here, or click to browse"
          >
            <UploadIcon className="chat-input__upload-icon" />
            <span className="chat-input__dropzone-label">
              <span className="chat-input__dropzone-primary">
                Drop photos here, or click to browse
              </span>
              <span className="chat-input__dropzone-secondary">
                Add one or more · JPEG · PNG · WebP · HEIC · max 10 MB each
              </span>
            </span>
          </button>
        )}
      </div>

      {/* Size error */}
      {sizeError && (
        <p role="alert" className="chat-input__error">
          {sizeError}
        </p>
      )}

      {/* Text query + submit */}
      <div className="chat-input__bottom">
        <label htmlFor="chat-text-input" className="sr-only">
          Search query
        </label>
        <textarea
          id="chat-text-input"
          value={text}
          onChange={(e) => setText(e.target.value)}
          onPaste={(e) => void handlePaste(e)}
          placeholder="Describe what you're looking for (optional)"
          className="chat-input__text"
          rows={2}
          aria-label="Search query"
        />
        <button
          type="submit"
          disabled={(items.length === 0 && !text) || isStreaming}
          aria-disabled={(items.length === 0 && !text) || isStreaming}
          className="chat-input__submit"
        >
          {isStreaming ? (
            "Searching…"
          ) : (
            <>
              Search
              <ArrowIcon />
            </>
          )}
        </button>
      </div>
    </form>
  );
}
