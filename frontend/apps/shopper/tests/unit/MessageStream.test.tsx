/**
 * Unit tests for MessageStream component.
 *
 * Tests rendering of product cards when props are populated.
 * ProductCard is mocked to isolate MessageStream behaviour.
 */
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import type { components } from "@avsa/shared";

type ChatProductCard = components["schemas"]["ProductCard"];

// Mock next/image for any transitive usage
vi.mock("next/image", () => ({
  default: ({ src, alt }: { src: string; alt: string }) => (
    // eslint-disable-next-line @next/next/no-img-element
    <img src={src} alt={alt} />
  ),
}));

const { default: MessageStream } = await import(
  "../../app/components/MessageStream"
);

function makeCard(id: string, title: string): ChatProductCard {
  return {
    id,
    title,
    price: 100,
    currency: "ZAR",
    image_url: `https://example.com/${id}.jpg`,
    category: "Test",
  };
}

describe("MessageStream", () => {
  it("renders nothing when productCards is empty", () => {
    const { container } = render(
      <MessageStream productCards={[]} />,
    );
    // Should render null — container has no meaningful children
    expect(container.firstChild).toBeNull();
  });

  it("renders a product card for each item in productCards", () => {
    const cards = [makeCard("p-1", "Red Dress"), makeCard("p-2", "Blue Jacket")];
    render(<MessageStream productCards={cards} />);

    // Each card should be rendered with the product-card class
    const renderedCards = document.querySelectorAll(".product-card");
    expect(renderedCards).toHaveLength(2);
  });

  it("renders product card titles", () => {
    const cards = [makeCard("p-1", "Red Dress"), makeCard("p-2", "Blue Jacket")];
    render(<MessageStream productCards={cards} />);

    expect(screen.getByText("Red Dress")).toBeInTheDocument();
    expect(screen.getByText("Blue Jacket")).toBeInTheDocument();
  });

  it("renders the result count heading", () => {
    const cards = [makeCard("p-1", "Red Dress")];
    render(<MessageStream productCards={cards} />);

    expect(screen.getByText("1 result")).toBeInTheDocument();
  });

  it("renders plural results heading for multiple cards", () => {
    const cards = [makeCard("p-1", "Red Dress"), makeCard("p-2", "Blue Jacket")];
    render(<MessageStream productCards={cards} />);

    expect(screen.getByText("2 results")).toBeInTheDocument();
  });
});
