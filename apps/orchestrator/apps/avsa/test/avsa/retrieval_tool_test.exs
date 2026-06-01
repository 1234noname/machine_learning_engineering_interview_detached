defmodule AVSA.RetrievalToolTest do
  @moduledoc """
  Unit tests for AVSA.RetrievalTool — no Repo, always run in CI.

  These tests verify behaviour that is independent of the database:
  the Repo-absent fallback, and telemetry emission on every call.
  """

  use ExUnit.Case, async: false

  test "returns {:ok, []} when Repo is not started" do
    # RetrievalTool detects a missing Repo via Process.whereis/1 and returns
    # an empty result set rather than crashing. This matters in unit test
    # environments where no database connection is available.
    #
    # NOTE: this test only holds in the default CI config where start_repo
    # is suppressed (test.exs). If a prior test started the Repo this will
    # pass for the wrong reason — the integration test suite covers that path.
    case Process.whereis(AVSA.Repo) do
      nil ->
        assert {:ok, []} = AVSA.RetrievalTool.call([], %{})

      _pid ->
        # Repo is started in this run (integration env); skip rather than fail.
        # The integration tests below exercise the live-Repo path properly.
        :ok
    end
  end

  test "fires [:avsa, :orch, :tool, :retrieval, :stop] telemetry on every call" do
    # Telemetry is how Prometheus/Grafana observes retrieval latency.
    # Verifying the event fires in the unit environment (no Repo) ensures the
    # span will appear in dashboards regardless of DB availability.
    test_pid = self()
    ref = make_ref()
    handler_id = "test-retrieval-telemetry-#{inspect(ref)}"

    :telemetry.attach(
      handler_id,
      [:avsa, :orch, :tool, :retrieval, :stop],
      fn _event, _measurements, _metadata, _config ->
        send(test_pid, {:telemetry_fired, ref})
      end,
      nil
    )

    AVSA.RetrievalTool.call([], %{})

    assert_receive {:telemetry_fired, ^ref},
                   1_000,
                   "[:avsa, :orch, :tool, :retrieval, :stop] was not emitted"

    :telemetry.detach(handler_id)
  end

  # ---------------------------------------------------------------------------
  # Colour-constrained kNN query construction.
  #
  # Hermetic: asserts the SQL + params the tool builds, WITHOUT a DB. The
  # builder is pure (build_knn_query/3), so we can prove:
  #   * when attrs carry a colour, the SQL applies a parameterised colour
  #     filter (no string interpolation) and still ORDERs BY the image
  #     embedding distance (style preserved within the constraint);
  #   * when attrs carry no colour, the query is the original pure-kNN SQL
  #     (image-only — no regression);
  #   * the colour parameter is normalised (case-insensitive match).
  # ---------------------------------------------------------------------------
  describe "build_knn_query/3 ( colour constraint)" do
    @vec [0.1, 0.2, 0.3]

    test "with a colour the SQL filters on colour and the colour is a bound param" do
      {sql, params} =
        AVSA.RetrievalTool.build_knn_query(@vec, [], %{"colour" => "green"})

      # Parameterised colour filter — the literal "green" must NOT be interpolated
      # into the SQL string (forbidden); it must arrive as a bound parameter.
      assert sql =~ ~r/colour/i, "expected a colour filter in the SQL; got: #{sql}"
      refute sql =~ "green", "colour value must be a bound param, not interpolated: #{sql}"
      assert "green" in params, "expected the colour value among the bound params: #{inspect(params)}"

      # Style preserved: still ordered by the image-embedding distance.
      assert sql =~ "ORDER BY embedding <=>",
             "kNN ordering on embedding must be preserved inside the colour filter: #{sql}"
    end

    test "without a colour the SQL is the original pure-kNN query (image-only regression)" do
      {sql, params} = AVSA.RetrievalTool.build_knn_query(@vec, [], %{})

      refute sql =~ ~r/\bcolour\b/i,
             "image-only query must NOT add a colour filter; got: #{sql}"

      assert sql =~ "ORDER BY embedding <=>"
      # Only the embedding + prior-ids params, no colour param.
      assert length(params) == 2, "expected exactly [embedding, prior_ids]; got #{inspect(params)}"
    end

    test "a blank/nil colour is treated as no constraint (no filter added)" do
      {sql_nil, _} = AVSA.RetrievalTool.build_knn_query(@vec, [], %{"colour" => nil})
      {sql_blank, _} = AVSA.RetrievalTool.build_knn_query(@vec, [], %{"colour" => ""})

      refute sql_nil =~ ~r/\bcolour\b/i, "nil colour must not add a filter: #{sql_nil}"
      refute sql_blank =~ ~r/\bcolour\b/i, "blank colour must not add a filter: #{sql_blank}"
    end

    test "colour match is case-insensitive (lower() applied on both sides)" do
      {sql, params} =
        AVSA.RetrievalTool.build_knn_query(@vec, [], %{"colour" => "Green"})

      assert sql =~ ~r/lower\(\s*colour\s*\)/i,
             "expected case-insensitive colour match via lower(colour): #{sql}"

      assert "Green" in params or "green" in params,
             "colour value must be bound (any case); got #{inspect(params)}"
    end
  end

  # ---------------------------------------------------------------------------
  # Observability — avsa_retrieval_results + avsa_retrieval_empty_total.
  # REAL test: a real RetrievalTool.call with the Repo absent returns {:ok, []},
  # which is a successful zero-row query — the results distribution records 0 and
  # the empty counter increments. A real handler observes the real emit.
  # ---------------------------------------------------------------------------

  test "Repo-absent {:ok, []} path emits results=0 and increments empty counter" do
    # Only meaningful when the Repo is genuinely absent (default CI config).
    case Process.whereis(AVSA.Repo) do
      nil ->
        test_pid = self()
        handler_id = "retrieval-empty-#{:erlang.unique_integer([:positive])}"

        :telemetry.attach_many(
          handler_id,
          [
            [:avsa, :retrieval, :results],
            [:avsa, :retrieval, :empty]
          ],
          fn event, measurements, _metadata, _config ->
            send(test_pid, {:retrieval_metric, event, measurements})
          end,
          nil
        )

        on_exit(fn -> :telemetry.detach(handler_id) end)

        assert {:ok, []} = AVSA.RetrievalTool.call([], %{})

        assert_receive {:retrieval_metric, [:avsa, :retrieval, :results], %{count: 0}}, 1_000
        assert_receive {:retrieval_metric, [:avsa, :retrieval, :empty], %{count: 1}}, 1_000

      _pid ->
        # Repo started (integration env) — the Repo-absent path is not exercised.
        :ok
    end
  end
end

defmodule AVSA.RetrievalToolIntegrationTest do
  @moduledoc """
  Integration tests for AVSA.RetrievalTool — require Postgres + pgvector.

  Verifies that the GenServer correctly queries the catalog and returns
  results with the correct shape, ordering, and self-similarity property.

  The self-similarity test (nearest_neighbour_of_own_embedding_ranks_first) is
  the critical correctness gate: if the kNN operator, index type, or embedding
  format is wrong, a product's own embedding will not rank first — a silent
  but catastrophic bug in the similarity-search flow.
  """

  use ExUnit.Case, async: false

  @moduletag :integration

  setup do
    # Shared Sandbox transaction (rolled back at teardown) so the named
    # AVSA.RetrievalTool GenServer that runs the kNN query sees this test's
    # seeded rows, plus a raised hnsw.ef_search for deterministic recall. See
    # AVSA.RepoTestHelper.checkout_shared!/0 for the full rationale.
    AVSA.RepoTestHelper.checkout_shared!()
  end

  # Produce a random L2-normalised 768-dim vector suitable for pgvector queries.
  defp unit_vec do
    raw = Enum.map(1..768, fn _ -> :rand.uniform() end)
    norm = :math.sqrt(Enum.sum(Enum.map(raw, fn x -> x * x end)))
    Enum.map(raw, fn x -> x / norm end)
  end

  # Insert a product with an explicit colour and a given embedding. Returns the id.
  defp insert_product(colour, embedding) do
    {:ok, %{rows: [[id]]}} =
      Ecto.Adapters.SQL.query(
        AVSA.Repo,
        """
        INSERT INTO catalog.products
          (title, category, colour, formality, occasion, price_cents, image_url, embedding)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        RETURNING id
        """,
        [
          "#{colour} dress #{:erlang.unique_integer([:positive])}",
          "dress",
          colour,
          "casual",
          "everyday",
          2500,
          "https://example.invalid/#{colour}.jpg",
          Pgvector.new(embedding)
        ]
      )

    # RETURNING id from a raw query yields a 16-byte binary UUID; the production
    # ProductResult.id is a string (Ecto.UUID.load), so return a string here too.
    {:ok, id} = Ecto.UUID.load(id)
    id
  end

  test "returns at least one result from a seeded catalog" do
    AVSA.CatalogFixture.seed(20)
    assert {:ok, results} = AVSA.RetrievalTool.call(unit_vec(), %{})
    assert length(results) >= 1
  end

  test "every result is a fully-populated ProductResult struct" do
    AVSA.CatalogFixture.seed(10)

    {:ok, [first | _]} = AVSA.RetrievalTool.call(unit_vec(), %{})

    assert %AVSA.ProductResult{} = first
    assert is_binary(first.title) and byte_size(first.title) > 0
    assert is_integer(first.price_cents) and first.price_cents > 0

    assert is_float(first.score) and first.score >= 0.0 and first.score <= 2.0,
           "cosine distance must be in [0, 2]; got #{first.score}"
  end

  test "image_url is populated in every ProductResult from the DB" do
    AVSA.CatalogFixture.seed(5)

    {:ok, results} = AVSA.RetrievalTool.call(unit_vec(), %{})
    assert length(results) >= 1

    Enum.each(results, fn pr ->
      assert is_binary(pr.image_url) and byte_size(pr.image_url) > 0,
             "expected image_url to be a non-empty string; got #{inspect(pr.image_url)}"
    end)
  end

  test "results are ordered by ascending cosine distance" do
    # ORDER BY embedding <=> query must be honoured. If it isn't, the first
    # product cards shown to the user are not the most visually similar ones.
    AVSA.CatalogFixture.seed(30)

    {:ok, results} = AVSA.RetrievalTool.call(unit_vec(), %{})
    scores = Enum.map(results, & &1.score)

    assert scores == Enum.sort(scores),
           "results must arrive in ascending cosine-distance order; got: #{inspect(scores)}"
  end

  test "nearest neighbour of a product's own embedding ranks first with distance ≈ 0" do
    # The self-similarity test: insert a product with a known L2-normalised
    # embedding, then query with exactly that embedding. It must come back first
    # with cosine distance ≈ 0. A failure here means the kNN operator, index
    # access method, or embedding format is wrong.
    Ecto.Adapters.SQL.query!(AVSA.Repo, "DELETE FROM catalog.products", [])

    AVSA.CatalogFixture.seed(10)

    own_embedding = unit_vec()

    {:ok, %{rows: [[target_id]]}} =
      Ecto.Adapters.SQL.query(
        AVSA.Repo,
        """
        INSERT INTO catalog.products
          (title, category, colour, formality, occasion, price_cents, image_url, embedding)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        RETURNING id
        """,
        [
          "Self-Similarity Target",
          "dress",
          "red",
          "casual",
          "everyday",
          2500,
          "https://example.invalid/self-sim.jpg",
          Pgvector.new(own_embedding)
        ]
      )

    # RETURNING id is a 16-byte binary; compare against the string ProductResult.id.
    {:ok, target_id} = Ecto.UUID.load(target_id)

    {:ok, [nearest | _]} = AVSA.RetrievalTool.call(own_embedding, %{})

    assert nearest.id == target_id,
           "nearest neighbour of a product's own embedding must be itself; " <>
             "got id=#{inspect(nearest.id)} (expected #{inspect(target_id)})"

    assert nearest.score < 1.0e-4,
           "cosine distance from a vector to itself must be ≈ 0; got #{nearest.score}"
  end

  test "returns at most 20 results (SQL LIMIT is enforced)" do
    Ecto.Adapters.SQL.query!(AVSA.Repo, "DELETE FROM catalog.products", [])
    AVSA.CatalogFixture.seed(50)

    {:ok, results} = AVSA.RetrievalTool.call(unit_vec(), %{})

    assert length(results) == 20,
           "RetrievalTool must return at most 20 results (SQL LIMIT 20); got #{length(results)}"
  end

  # ---------------------------------------------------------------------------
  # Colour-constrained retrieval.
  # ---------------------------------------------------------------------------

  test "a 'green' colour constraint returns ONLY green items, ordered by visual similarity" do
    # The core behavioural test: "similar styles in green," not red dresses
    # (the image's colour) and not arbitrary green items. We seed red AND green
    # products; query with a green product's own embedding under colour=green and
    # assert (a) every result is green, and (b) ascending cosine-distance order.
    Ecto.Adapters.SQL.query!(AVSA.Repo, "DELETE FROM catalog.products", [])

    # 10 red distractors (the image's colour) — must be excluded by the filter.
    for _ <- 1..10, do: insert_product("red", unit_vec())

    # 6 green products; the query embedding is one green item's own vector.
    green_query = unit_vec()
    _green_target = insert_product("green", green_query)
    for _ <- 1..5, do: insert_product("green", unit_vec())

    {:ok, results} = AVSA.RetrievalTool.call(green_query, %{"colour" => "green"})

    assert length(results) >= 1, "expected at least one green result"

    # (a) Constraint honoured: NO red items leak through.
    colours =
      results
      |> Enum.map(& &1.id)
      |> then(fn ids ->
        {:ok, %{rows: rows}} =
          Ecto.Adapters.SQL.query(
            AVSA.Repo,
            "SELECT colour FROM catalog.products WHERE id::text = ANY($1::text[])",
            [ids]
          )

        List.flatten(rows)
      end)

    assert Enum.all?(colours, &(&1 == "green")),
           "every result must be green; got colours #{inspect(colours)}"

    # (b) Style preserved: ascending cosine distance within the constraint.
    scores = Enum.map(results, & &1.score)

    assert scores == Enum.sort(scores),
           "constrained results must arrive in ascending cosine-distance order; got #{inspect(scores)}"
  end

  test "colour match is case-insensitive ('Green' text matches lowercase 'green' column)" do
    Ecto.Adapters.SQL.query!(AVSA.Repo, "DELETE FROM catalog.products", [])
    for _ <- 1..5, do: insert_product("green", unit_vec())

    {:ok, results} = AVSA.RetrievalTool.call(unit_vec(), %{"colour" => "Green"})

    assert length(results) >= 1,
           "a case-mismatched colour ('Green' vs column 'green') must still match"
  end

  test "narrow-constraint fallback: an unmatched colour falls back to best-effort, never empty (TR4)" do
    # A colour with NO matching rows must not return an empty result set; the
    # tool relaxes the hard filter to best-effort (unconstrained kNN) so the
    # shopper always sees similar styles.
    Ecto.Adapters.SQL.query!(AVSA.Repo, "DELETE FROM catalog.products", [])
    for _ <- 1..10, do: insert_product("red", unit_vec())

    {:ok, results} = AVSA.RetrievalTool.call(unit_vec(), %{"colour" => "chartreuse"})

    assert length(results) >= 1,
           "a narrow/unmatched colour constraint must fall back to best-effort, not return empty"
  end

  test "image-only retrieval (no colour in attrs) is unchanged — regression" do
    # No colour constraint → identical behaviour to plain kNN. Seed mixed colours
    # and assert results come from across the catalog (not colour-filtered).
    Ecto.Adapters.SQL.query!(AVSA.Repo, "DELETE FROM catalog.products", [])
    for _ <- 1..10, do: insert_product("red", unit_vec())
    for _ <- 1..10, do: insert_product("green", unit_vec())

    {:ok, results} = AVSA.RetrievalTool.call(unit_vec(), %{})

    scores = Enum.map(results, & &1.score)

    assert scores == Enum.sort(scores),
           "image-only results must remain ascending-distance ordered (no regression)"

    assert length(results) == 20,
           "image-only kNN must still return up to LIMIT 20 across all colours; got #{length(results)}"
  end
end

defmodule AVSA.RetrievalToolTextTest do
  @moduledoc """
  Unit tests for AVSA.RetrievalTool.call_text/2 — no Repo, always run in CI.
  """

  use ExUnit.Case, async: false

  test "call_text/2 returns {:ok, []} when Repo is not started" do
    case Process.whereis(AVSA.Repo) do
      nil ->
        assert {:ok, []} = AVSA.RetrievalTool.call_text(List.duplicate(0.0, 512), %{})

      _pid ->
        # Repo started — integration env, skip.
        :ok
    end
  end
end

defmodule AVSA.RetrievalToolTextIntegrationTest do
  @moduledoc """
  Integration tests for AVSA.RetrievalTool.call_text/2 — require Postgres + pgvector.

  Mirrors the structure of AVSA.RetrievalToolIntegrationTest but exercises the
  text_embedding column and @knn_text_sql query path.
  """

  use ExUnit.Case, async: false

  @moduletag :integration

  setup do
    # Shared Sandbox transaction for the call_text/2 kNN path (text_embedding
    # HNSW index). See AVSA.RepoTestHelper.checkout_shared!/0.
    AVSA.RepoTestHelper.checkout_shared!()
  end

  # Produce a random L2-normalised 512-dim vector for text embedding queries.
  defp unit_vec_512 do
    raw = Enum.map(1..512, fn _ -> :rand.uniform() end)
    norm = :math.sqrt(Enum.sum(Enum.map(raw, fn x -> x * x end)))
    Enum.map(raw, fn x -> x / norm end)
  end

  defp insert_product_with_text_embedding(text_embedding) do
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
          "Text Product #{:erlang.unique_integer([:positive])}",
          "dress",
          "blue",
          "casual",
          "everyday",
          1500,
          "https://example.invalid/text.jpg",
          # `embedding` (image, 768-dim) is NOT NULL. This test exercises the
          # text-retrieval path, so a fixed dummy image embedding satisfies the
          # constraint without affecting the text_embedding kNN query.
          Pgvector.new(List.duplicate(0.1, 768)),
          Pgvector.new(text_embedding)
        ]
      )

    # Match the production string-UUID contract (see insert_product/2).
    {:ok, id} = Ecto.UUID.load(id)
    id
  end

  test "call_text/2 returns at least 1 result from a seeded catalog" do
    Ecto.Adapters.SQL.query!(AVSA.Repo, "DELETE FROM catalog.products", [])

    # Seed 10 products with text embeddings.
    for _ <- 1..10, do: insert_product_with_text_embedding(unit_vec_512())

    assert {:ok, results} = AVSA.RetrievalTool.call_text(unit_vec_512(), %{})
    assert length(results) >= 1
  end

  test "nearest neighbour of a product's own text_embedding ranks first (distance ≈ 0)" do
    Ecto.Adapters.SQL.query!(AVSA.Repo, "DELETE FROM catalog.products", [])

    # Seed noise rows.
    for _ <- 1..10, do: insert_product_with_text_embedding(unit_vec_512())

    own_embedding = unit_vec_512()
    target_id = insert_product_with_text_embedding(own_embedding)

    {:ok, [nearest | _]} = AVSA.RetrievalTool.call_text(own_embedding, %{})

    assert nearest.id == target_id,
           "nearest text neighbour of a product's own embedding must be itself; " <>
             "got id=#{inspect(nearest.id)} (expected #{inspect(target_id)})"

    assert nearest.score < 1.0e-4,
           "cosine distance from a vector to itself must be ≈ 0; got #{nearest.score}"
  end
end
