defmodule AVSA.CatalogFixture do
  @doc """
  Inserts n rows into catalog.products with random L2-normalised vector(768) embeddings.

  Normalisation is mandatory: pgvector's <=> cosine-distance operator gives correct
  results only for unit vectors. Using unnormalised vectors in fixtures would cause
  integration tests to pass for the wrong reasons (wrong distance ordering).
  """
  def seed(n) do
    for _ <- 1..n do
      raw = Enum.map(1..768, fn _ -> :rand.uniform() end)
      norm = :math.sqrt(Enum.sum(Enum.map(raw, fn x -> x * x end)))
      embedding = Enum.map(raw, fn x -> x / norm end)

      Ecto.Adapters.SQL.query!(
        AVSA.Repo,
        """
          INSERT INTO catalog.products
            (title, category, colour, formality, occasion, price_cents, image_url, embedding)
          VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """,
        [
          "Product #{:erlang.unique_integer([:positive])}",
          "dress",
          "red",
          "casual",
          "everyday",
          1000,
          "https://example.com/img.jpg",
          Pgvector.new(embedding)
        ]
      )
    end

    :ok
  end
end
