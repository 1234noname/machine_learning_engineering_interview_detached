defmodule AVSA.ProductResult do
  @moduledoc """
  Struct representing a single product result returned by the retrieval tool.

  Fields:
    * `:id`          — UUID string for the product row.
    * `:title`       — Product display name.
    * `:category`    — Product category (e.g. "dress", "jacket").
    * `:price_cents` — Integer price in cents.
    * `:score`       — Cosine distance from the query embedding (lower = more similar).
    * `:image_url`   — Raw tokenless path (e.g. `/images/{key}`) stored in the DB;
                       the Python API signs it at read time via `_sign_card_image_url`.
                       May be `nil` when the row pre-dates image seeding.
  """

  @type t :: %__MODULE__{
          id: String.t() | nil,
          title: String.t() | nil,
          category: String.t() | nil,
          price_cents: integer() | nil,
          score: float() | nil,
          image_url: String.t() | nil
        }

  defstruct [:id, :title, :category, :price_cents, :score, :image_url]
end
