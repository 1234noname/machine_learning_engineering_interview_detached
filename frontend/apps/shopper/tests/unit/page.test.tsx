/**
 * Unit test for the home page (app/page.tsx).
 *
 * Network-touching components are mocked here to keep this a true unit test
 * and avoid flakiness from a missing API server.
 */
import { describe, expect, it, vi } from "vitest";
import { render, screen, act } from "@testing-library/react";

// Mock HealthBadge so the unit test has no network dependency.
vi.mock("@/components/HealthBadge", () => ({
  default: () => <div data-testid="health-badge-mock">mock</div>,
}));

// Capture the onError callback passed to ChatInput so tests can invoke it.
let capturedOnError: ((msg: string) => void) | undefined;

// Mock Chat UI components to isolate the page-level layout test.
vi.mock("@/components/ChatInput", () => ({
  default: ({ onError }: { onError?: (msg: string) => void }) => {
    capturedOnError = onError;
    return <div data-testid="chat-input-mock" />;
  },
}));
vi.mock("@/components/MessageStream", () => ({
  default: () => <div data-testid="message-stream-mock" />,
}));
// BrowseGrid fetches /api/catalog on mount — mock it out of the page unit test.
vi.mock("@/components/BrowseGrid", () => ({
  default: () => <div data-testid="browse-grid-mock" />,
}));

// Dynamically import after mocks are registered.
const { default: Page } = await import("../../app/page");

describe("Page", () => {
  it("renders the AVSA Shopper heading", () => {
    render(<Page />);
    expect(
      screen.getByRole("heading", { level: 1, name: "AVSA Shopper" }),
    ).toBeInTheDocument();
  });

  it("renders an accessible error banner when ChatInput triggers onError", async () => {
    render(<Page />);

    // No error banner initially
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();

    // Simulate an error from ChatInput
    await act(async () => {
      capturedOnError?.("Error: POST /chat failed with status 500");
    });

    const alert = screen.getByRole("alert");
    expect(alert).toBeInTheDocument();
    expect(alert).toHaveTextContent("Error: POST /chat failed with status 500");
  });
});
