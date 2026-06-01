defmodule AVSA.RetrievalToolPriorIdsTest do
  @moduledoc """
  Integration tests for the prior_result_ids exclusion filter in AVSA.RetrievalTool.

  Verifies that the `"prior_result_ids"` key in attrs is correctly applied as a SQL
  exclusion clause, preventing the same products from being returned on consecutive
  conversation turns.

  Both the image embedding path (call/2) and the text embedding path (call_text/2)
  are covered.
  """

  use ExUnit.Case, async: false

  @moduletag :integration
  @moduletag start_repo: true

  setup do
    # Shared Sandbox transaction so the AVSA.RetrievalTool GenServer (call/2 &
    # call_text/2) shares this test's connection, with raised hnsw.ef_search for
    # deterministic recall. See AVSA.RepoTestHelper.checkout_shared!/0.
    AVSA.RepoTestHelper.checkout_shared!()
  end

  # Generate a random L2-normalised 768-dim vector.
  defp unit_vec_768 do
    raw = Enum.map(1..768, fn _ -> :rand.uniform() end)
    norm = :math.sqrt(Enum.sum(Enum.map(raw, fn x -> x * x end)))
    Enum.map(raw, fn x -> x / norm end)
  end

  # Generate a random L2-normalised 512-dim vector.
  defp unit_vec_512 do
    raw = Enum.map(1..512, fn _ -> :rand.uniform() end)
    norm = :math.sqrt(Enum.sum(Enum.map(raw, fn x -> x * x end)))
    Enum.map(raw, fn x -> x / norm end)
  end

  # Insert a product with both embedding and text_embedding. Returns the UUID string.
  defp insert_product(embedding_768, text_embedding_512) do
    {:ok, %{rows: [[id]]}} =
      Ecto.Adapters.SQL.query(
        AVSA.Repo,
        """
        INSERT INTO catalog.products
          (title, category, colour, formality, occasion, price_cents, image_url, embedding, text_embedding)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        RETURNING id
        """,
        [
          "PriorIds Product #{:erlang.unique_integer([:positive])}",
          "dress",
          "blue",
          "casual",
          "everyday",
          1999,
          "https://example.invalid/prior-ids.jpg",
          Pgvector.new(embedding_768),
          Pgvector.new(text_embedding_512)
        ]
      )

    id
  end

  test "call/2 with prior_result_ids excludes turn-1 results from turn-2" do
    Ecto.Adapters.SQL.query!(AVSA.Repo, "DELETE FROM catalog.products", [])

    # Seed 20 products with distinct embeddings so we have enough to fill both turns.
    products =
      for _ <- 1..20 do
        emb = unit_vec_768()
        text_emb = unit_vec_512()
        id = insert_product(emb, text_emb)
        {id, emb}
      end

    # Use the first product's own embedding as the query — guarantees it ranks first.
    {_target_id, query_embedding} = List.first(products)

    # Turn 1: no exclusions.
    {:ok, turn1_results} = AVSA.RetrievalTool.call(query_embedding, %{})
    turn1_ids = Enum.map(turn1_results, & &1.id)

    assert length(turn1_ids) > 0, "Turn 1 should return at least one result"

    # Turn 2: exclude everything seen in turn 1.
    {:ok, turn2_results} =
      AVSA.RetrievalTool.call(query_embedding, %{"prior_result_ids" => turn1_ids})

    turn2_ids = Enum.map(turn2_results, & &1.id)

    overlap =
      MapSet.intersection(MapSet.new(turn1_ids), MapSet.new(turn2_ids))

    assert MapSet.size(overlap) <= 1,
           "Turn 2 should not return results already seen in turn 1; " <>
             "overlap: #{inspect(MapSet.to_list(overlap))}"
  end

  test "call_text/2 with prior_result_ids excludes turn-1 results from turn-2" do
    Ecto.Adapters.SQL.query!(AVSA.Repo, "DELETE FROM catalog.products", [])

    # Seed 20 products with distinct text embeddings.
    products =
      for _ <- 1..20 do
        emb = unit_vec_768()
        text_emb = unit_vec_512()
        id = insert_product(emb, text_emb)
        {id, text_emb}
      end

    # Use the first product's own text embedding as the query — guarantees it ranks first.
    {_target_id, query_text_embedding} = List.first(products)

    # Turn 1: no exclusions.
    {:ok, turn1_results} = AVSA.RetrievalTool.call_text(query_text_embedding, %{})
    turn1_ids = Enum.map(turn1_results, & &1.id)

    assert length(turn1_ids) > 0, "Turn 1 (text path) should return at least one result"

    # Turn 2: exclude everything seen in turn 1.
    {:ok, turn2_results} =
      AVSA.RetrievalTool.call_text(query_text_embedding, %{"prior_result_ids" => turn1_ids})

    turn2_ids = Enum.map(turn2_results, & &1.id)

    overlap =
      MapSet.intersection(MapSet.new(turn1_ids), MapSet.new(turn2_ids))

    assert MapSet.size(overlap) <= 1,
           "Turn 2 (text path) should not return results already seen in turn 1; " <>
             "overlap: #{inspect(MapSet.to_list(overlap))}"
  end

  test "call/2 with empty prior_result_ids returns same results as no-attrs call" do
    Ecto.Adapters.SQL.query!(AVSA.Repo, "DELETE FROM catalog.products", [])

    AVSA.CatalogFixture.seed(10)

    query_embedding = unit_vec_768()

    {:ok, results_no_attrs} = AVSA.RetrievalTool.call(query_embedding, %{})
    {:ok, results_empty_prior} = AVSA.RetrievalTool.call(query_embedding, %{"prior_result_ids" => []})

    ids_no_attrs = Enum.map(results_no_attrs, & &1.id)
    ids_empty_prior = Enum.map(results_empty_prior, & &1.id)

    assert ids_no_attrs == ids_empty_prior,
           "Empty prior_result_ids should behave identically to no prior_result_ids"
  end
end
