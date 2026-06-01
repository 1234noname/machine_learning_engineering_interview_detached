import Image from "next/image";

interface Props {
  title: string;
  category: string;
  price: number;
  imageUrl: string;
}

const zarFormatter = new Intl.NumberFormat("en-ZA", {
  style: "currency",
  currency: "ZAR",
});

function ImagePlaceholder() {
  return (
    <div className="product-card__placeholder">
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth={1}
        aria-hidden="true"
      >
        <rect x="3" y="3" width="18" height="18" rx="2" />
        <circle cx="8.5" cy="8.5" r="1.5" />
        <path strokeLinecap="round" d="m21 15-5-5L5 21" />
      </svg>
    </div>
  );
}

export default function ProductCard({
  title,
  category,
  price,
  imageUrl,
}: Props) {
  // Presentational only — the product detail side-panel was removed in the UX
  // overhaul, so result/browse cards never become interactive buttons.
  return (
    <article className="product-card product-card--static" aria-label={title}>
      <div className="product-card__image-wrap">
        {imageUrl ? (
          <Image
            src={imageUrl}
            alt={title}
            fill
            style={{ objectFit: "cover" }}
            unoptimized
          />
        ) : (
          <ImagePlaceholder />
        )}
      </div>
      <div className="product-card__body">
        <p className="product-card__title">{title || "Unnamed product"}</p>
        {category && (
          <span className="product-card__category">{category}</span>
        )}
        <p className="product-card__price" data-testid="product-price">
          {zarFormatter.format(price)}
        </p>
      </div>
    </article>
  );
}
