/**
 * Unit tests for ChatInput component.
 *
 * Tests the form submission, file-size validation, and SSE dispatch contract.
 * fetch is replaced with a vitest stub to avoid real network calls.
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeFile(name: string, sizeBytes: number): File {
  const buf = new Uint8Array(sizeBytes);
  return new File([buf], name, { type: "image/jpeg" });
}

// ---------------------------------------------------------------------------
// Setup — replace global fetch with a spy
// ---------------------------------------------------------------------------

const mockFetch = vi.fn<typeof fetch>();
vi.stubGlobal("fetch", mockFetch);

beforeEach(() => {
  mockFetch.mockReset();
});

const { default: ChatInput } = await import("../../app/components/ChatInput");

describe("ChatInput", () => {
  it("renders without error", () => {
    const { container } = render(
      <ChatInput onProductCard={vi.fn()} />,
    );
    // The component should render something (not crash)
    expect(container).toBeDefined();
  });

  it("renders a file input with the correct accept types", () => {
    render(<ChatInput onProductCard={vi.fn()} />);
    const fileInput = screen.getByLabelText(/upload image/i);
    expect(fileInput).toBeInTheDocument();
    expect(fileInput).toHaveAttribute(
      "accept",
      expect.stringContaining("image/jpeg"),
    );
  });

  it("renders a text input for the query", () => {
    render(<ChatInput onProductCard={vi.fn()} />);
    const textInput = screen.getByRole("textbox");
    expect(textInput).toBeInTheDocument();
  });

  it("renders a submit button", () => {
    render(<ChatInput onProductCard={vi.fn()} />);
    const submitButton = screen.getByRole("button", { name: /search/i });
    expect(submitButton).toBeInTheDocument();
  });

  it("shows error message for file > 10 MB and does NOT call fetch", async () => {
    const user = userEvent.setup();

    render(<ChatInput onProductCard={vi.fn()} />);

    const fileInput = screen.getByLabelText(/upload image/i);
    const oversizedFile = makeFile("big.jpg", 11 * 1024 * 1024); // 11 MB

    await user.upload(fileInput, oversizedFile);

    // Error message must appear
    expect(
      screen.getByText(/image must be under 10 mb/i),
    ).toBeInTheDocument();

    // fetch must NOT have been called
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it("does NOT show a size error for a file under 10 MB", async () => {
    const user = userEvent.setup();

    mockFetch.mockResolvedValueOnce(
      new Response(new ReadableStream({ start() {} }), { status: 200 }),
    );

    render(<ChatInput onProductCard={vi.fn()} />);

    const fileInput = screen.getByLabelText(/upload image/i);
    const smallFile = makeFile("small.jpg", 1 * 1024 * 1024); // 1 MB

    await user.upload(fileInput, smallFile);

    expect(screen.queryByText(/image must be under 10 mb/i)).not.toBeInTheDocument();
  });

  // ---------------------------------------------------------------------------
  // onError prop tests
  // ---------------------------------------------------------------------------

  it("calls onError when the fetch response is not ok", async () => {
    const user = userEvent.setup();
    const onError = vi.fn();

    mockFetch.mockResolvedValueOnce(
      new Response(null, { status: 500 }),
    );

    render(<ChatInput onProductCard={vi.fn()} onError={onError} />);

    const fileInput = screen.getByLabelText(/upload image/i);
    const file = makeFile("photo.jpg", 1 * 1024 * 1024);
    await user.upload(fileInput, file);

    const submitButton = screen.getByRole("button", { name: /search/i });
    await user.click(submitButton);

    await waitFor(() =>
      expect(onError).toHaveBeenCalledWith(
        expect.stringContaining("Error:"),
      ),
    );
  });

  // ---------------------------------------------------------------------------
  // onSessionId prop tests
  // ---------------------------------------------------------------------------

  it("calls onSessionId with X-Conversation-Id header value after a successful chat", async () => {
    const user = userEvent.setup();
    const onSessionId = vi.fn();

    mockFetch.mockResolvedValueOnce(
      new Response(new ReadableStream({ start(c) { c.close(); } }), {
        status: 200,
        headers: { "X-Conversation-Id": "conv-chat-456" },
      }),
    );

    render(
      <ChatInput onProductCard={vi.fn()} onSessionId={onSessionId} />,
    );

    const fileInput = screen.getByLabelText(/upload image/i);
    const file = makeFile("photo.jpg", 1 * 1024 * 1024);
    await user.upload(fileInput, file);

    const submitButton = screen.getByRole("button", { name: /search/i });
    await user.click(submitButton);

    await waitFor(() =>
      expect(onSessionId).toHaveBeenCalledWith("conv-chat-456"),
    );
  });

  it("does NOT call onSessionId when X-Conversation-Id header is absent", async () => {
    const user = userEvent.setup();
    const onSessionId = vi.fn();

    mockFetch.mockResolvedValueOnce(
      new Response(new ReadableStream({ start(c) { c.close(); } }), {
        status: 200,
      }),
    );

    render(
      <ChatInput onProductCard={vi.fn()} onSessionId={onSessionId} />,
    );

    const fileInput = screen.getByLabelText(/upload image/i);
    const file = makeFile("photo.jpg", 1 * 1024 * 1024);
    await user.upload(fileInput, file);

    const submitButton = screen.getByRole("button", { name: /search/i });
    await user.click(submitButton);

    // Wait for streaming to complete
    await waitFor(() => expect(mockFetch).toHaveBeenCalledOnce());

    expect(onSessionId).not.toHaveBeenCalled();
  });
});
