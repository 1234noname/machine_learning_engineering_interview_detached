import type { components } from "@avsa/shared";
import ProductCard from "./ProductCard";
import { safeProxyUrl } from "@/lib/imageProxy";

export type ChatProductCard = components["schemas"]["ProductCard"];

interface Props {
  productCards: ChatProductCard[];
}

export default function MessageStream({ productCards }: Props) {
  if (productCards.length === 0) return null;

  return (
    <section className="message-stream" aria-label="Search results">
      <div className="message-stream__header">
        <span className="message-stream__heading">
          {productCards.length}{" "}
          {productCards.length === 1 ? "result" : "results"}
        </span>
        <div className="message-stream__rule" />
      </div>

      <div className="message-stream__grid" role="list">
        {productCards.map((card) => (
          <div key={card.id} role="listitem">
            <ProductCard
              title={card.title}
              category={card.category}
              price={card.price}
              imageUrl={safeProxyUrl(card.image_url)}
            />
          </div>
        ))}
      </div>
    </section>
  );
}
