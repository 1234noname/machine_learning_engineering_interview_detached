/**
 * Unit tests for ProductCard component.
 *
 * ProductCard is presentational only (the interactive detail-view branch was
 * removed with the detail panel) — these tests render the static variant the
 * app actually uses. next/image is mocked to avoid Next.js internals in vitest.
 */
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

// Mock next/image since it requires Next.js internals.
vi.mock("next/image", () => ({
  default: ({ src, alt }: { src: string; alt: string }) => (
    // eslint-disable-next-line @next/next/no-img-element
    <img src={src} alt={alt} />
  ),
}));

const { default: ProductCard } = await import(
  "../../app/components/ProductCard"
);

const defaultProps = {
  title: "Red Dress",
  category: "Dresses",
  price: 499.99,
  imageUrl: "https://example.com/red-dress.jpg",
};

describe("ProductCard", () => {
  it("renders with all required props without crashing", () => {
    const { container } = render(<ProductCard {...defaultProps} />);
    expect(container).toBeDefined();
  });

  it("renders the product title", () => {
    render(<ProductCard {...defaultProps} />);
    expect(screen.getByText("Red Dress")).toBeInTheDocument();
  });

  it("renders the category badge", () => {
    render(<ProductCard {...defaultProps} />);
    expect(screen.getByText("Dresses")).toBeInTheDocument();
  });

  it("renders the price formatted as ZAR currency", () => {
    render(<ProductCard {...defaultProps} />);
    // Intl.NumberFormat with ZAR produces "R 499,99" or "ZAR 499.99" depending
    // on locale; just check the digits are present.
    const priceEl = screen.getByTestId("product-price");
    expect(priceEl.textContent).toMatch(/499/);
  });

  it("renders an image with correct alt text", () => {
    render(<ProductCard {...defaultProps} />);
    const img = screen.getByRole("img", { name: "Red Dress" });
    expect(img).toBeInTheDocument();
  });

  it("has className 'product-card' on the root element", () => {
    const { container } = render(<ProductCard {...defaultProps} />);
    const root = container.firstChild as HTMLElement;
    expect(root.className).toContain("product-card");
  });
});
