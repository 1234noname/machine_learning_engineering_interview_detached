defmodule AVSA.RetrievalTool do
  @moduledoc """
  GenServer that performs kNN vector search against catalog.products using pgvector.
  """

  use GenServer

  require Logger

  @knn_sql """
  SELECT id, title, category, price_cents, image_url, (embedding <=> $1) AS score
  FROM catalog.products
  WHERE id != ALL($2::uuid[])
  ORDER BY embedding <=> $1
  LIMIT 20
  """

  @knn_colour_sql """
  SELECT id, title, category, price_cents, image_url, (embedding <=> $1) AS score
  FROM catalog.products
  WHERE id != ALL($2::uuid[])
  AND lower(colour) = lower($3)
  ORDER BY embedding <=> $1
  LIMIT 20
  """

  @knn_text_sql """
  SELECT id, title, category, price_cents, image_url, (text_embedding <=> $1) AS score
  FROM catalog.products
  WHERE text_embedding IS NOT NULL
  AND id != ALL($2::uuid[])
  ORDER BY text_embedding <=> $1
  LIMIT 20
  """

  # Client API

  def start_link(_opts \\ []) do
    GenServer.start_link(__MODULE__, %{}, name: __MODULE__)
  end

  @doc """
  Build the parameterised image-kNN query for the given embedding, prior-id
  exclusion list, and merged attrs.

  Returns `{sql, params}`:

    * When `attrs` carries a non-blank `"colour"`, the colour-constrained query
      (`@knn_colour_sql`) is returned with the colour bound as `$3`
      (case-insensitive, never interpolated) and the kNN `ORDER BY` on the image
      embedding preserved (style within the constraint).
    * Otherwise the unconstrained pure-kNN query (`@knn_sql`) is returned — the
      image-only path is byte-for-byte unchanged.

  Pure and side-effect free so it is hermetically testable without a DB.
  """
  @spec build_knn_query([float()], [String.t()], map()) :: {String.t(), [term()]}
  def build_knn_query(embedding, prior_ids, attrs) do
    case constraint_colour(attrs) do
      nil ->
        {@knn_sql, [Pgvector.new(embedding), prior_ids]}

      colour ->
        {@knn_colour_sql, [Pgvector.new(embedding), prior_ids, colour]}
    end
  end

  defp constraint_colour(attrs) do
    case Map.get(attrs, "colour") do
      colour when is_binary(colour) ->
        case String.trim(colour) do
          "" -> nil
          trimmed -> trimmed
        end

      _ ->
        nil
    end
  end

  @doc """
  Perform kNN retrieval.

  - `embedding` — list of 768 floats
  - `attrs` — attribute map:
    * `"prior_result_ids"` (list of UUID strings) is applied as a SQL exclusion
      filter (`WHERE id != ALL($2::uuid[])`) to avoid repeating results seen in
      earlier conversation turns.
    * `"colour"` — when present and non-blank, the kNN candidate set is
      constrained to that colour (`lower(colour) = lower($3)`, parameterised),
      so results are visually-similar styles WITHIN the constraint. If the
      constraint matches zero in-style rows the filter is relaxed to best-effort
      pure-kNN — a colour constraint never returns an empty set on its own.
      Absent/blank colour = unconstrained image-only kNN.
  """
  def call(embedding, attrs) do
    GenServer.call(__MODULE__, {:call, embedding, attrs})
  end

  @doc """
  Perform text-kNN retrieval over text_embedding.

  - `text_embedding` — list of 512 floats
  - `attrs` — attribute map; `"prior_result_ids"` (list of UUID strings) is applied as
    a SQL exclusion filter to avoid repeating results seen in earlier conversation turns.
  """
  def call_text(text_embedding, attrs) do
    GenServer.call(__MODULE__, {:call_text, text_embedding, attrs})
  end

  # Server callbacks

  @impl GenServer
  def init(state) do
    {:ok, state}
  end

  @impl GenServer
  def handle_call({:call, embedding, attrs}, _from, state) do
    result =
      :telemetry.span(
        [:avsa, :orch, :tool, :retrieval],
        %{},
        fn ->
          outcome = run_query(embedding, attrs)

          result_count =
            case outcome do
              {:ok, results} -> length(results)
              _ -> 0
            end

          emit_retrieval_metrics(outcome, result_count)

          {outcome, %{result_count: result_count}}
        end
      )

    {:reply, result, state}
  end

  @impl GenServer
  def handle_call({:call_text, text_embedding, attrs}, _from, state) do
    result =
      :telemetry.span(
        [:avsa, :orch, :tool, :retrieval_text],
        %{},
        fn ->
          outcome = run_text_query(text_embedding, attrs)

          result_count =
            case outcome do
              {:ok, results} -> length(results)
              _ -> 0
            end

          emit_retrieval_metrics(outcome, result_count)

          {outcome, %{result_count: result_count}}
        end
      )

    {:reply, result, state}
  end

  defp emit_retrieval_metrics({:ok, _results}, result_count) do
    :telemetry.execute(
      [:avsa, :retrieval, :results],
      %{count: result_count},
      %{}
    )

    if result_count == 0 do
      :telemetry.execute([:avsa, :retrieval, :empty], %{count: 1}, %{})
    end

    :ok
  end

  defp emit_retrieval_metrics(_outcome, _result_count), do: :ok

  defp run_query(embedding, attrs) do
    prior_ids = attrs |> Map.get("prior_result_ids", []) |> dump_prior_ids()

    if repo_started?() do
      run_db_query(embedding, prior_ids, attrs)
    else
      Logger.debug("RetrievalTool: AVSA.Repo not started, returning empty results")
      {:ok, []}
    end
  end

  defp run_db_query(embedding, prior_ids, attrs) do
    {sql, params} = build_knn_query(embedding, prior_ids, attrs)

    case run_knn_sql(sql, params) do
      {:ok, []} when sql == @knn_colour_sql ->
        # Narrow colour constraint → relax to unconstrained best-effort.
        Logger.debug("RetrievalTool: colour constraint returned 0 rows, relaxing to pure kNN")
        :telemetry.execute([:avsa, :retrieval, :constraint_relaxed], %{count: 1}, %{})
        run_knn_sql(@knn_sql, [Pgvector.new(embedding), prior_ids])

      other ->
        other
    end
  end

  defp run_knn_sql(sql, params) do
    start_time = :erlang.monotonic_time()

    query_result = Ecto.Adapters.SQL.query(AVSA.Repo, sql, params)

    elapsed_native = :erlang.monotonic_time() - start_time
    elapsed_ms = :erlang.convert_time_unit(elapsed_native, :native, :millisecond)

    budget_ms = Application.get_env(:avsa, :retrieval_knn_ms, 150)

    if elapsed_ms > budget_ms do
      Logger.warning("RetrievalTool kNN exceeded #{elapsed_ms}ms (budget: #{budget_ms}ms)")
    end

    case query_result do
      {:ok, %{rows: rows}} ->
        {:ok, rows_to_results(rows)}

      {:error, reason} ->
        {:error, reason}
    end
  end

  defp rows_to_results(rows) do
    Enum.map(rows, fn [id, title, category, price_cents, image_url, score] ->
      {:ok, id_str} = Ecto.UUID.load(id)

      %AVSA.ProductResult{
        id: id_str,
        title: title,
        category: category,
        price_cents: price_cents,
        image_url: image_url,
        score: score
      }
    end)
  end

  defp run_text_query(text_embedding, attrs) do
    prior_ids = attrs |> Map.get("prior_result_ids", []) |> dump_prior_ids()

    if repo_started?() do
      run_text_db_query(text_embedding, prior_ids)
    else
      Logger.debug("RetrievalTool: AVSA.Repo not started, returning empty text results")
      {:ok, []}
    end
  end

  defp run_text_db_query(text_embedding, prior_ids) do
    start_time = :erlang.monotonic_time()

    query_result =
      Ecto.Adapters.SQL.query(AVSA.Repo, @knn_text_sql, [Pgvector.new(text_embedding), prior_ids])

    elapsed_native = :erlang.monotonic_time() - start_time
    elapsed_ms = :erlang.convert_time_unit(elapsed_native, :native, :millisecond)

    budget_ms = Application.get_env(:avsa, :retrieval_knn_ms, 150)

    if elapsed_ms > budget_ms do
      Logger.warning(
        "RetrievalTool text-kNN exceeded #{elapsed_ms}ms (budget: #{budget_ms}ms)"
      )
    end

    case query_result do
      {:ok, %{rows: rows}} ->
        results =
          Enum.map(rows, fn [id, title, category, price_cents, image_url, score] ->
            {:ok, id_str} = Ecto.UUID.load(id)

            %AVSA.ProductResult{
              id: id_str,
              title: title,
              category: category,
              price_cents: price_cents,
              image_url: image_url,
              score: score
            }
          end)

        {:ok, results}

      {:error, reason} ->
        {:error, reason}
    end
  end

  defp repo_started? do
    case Process.whereis(AVSA.Repo) do
      nil -> false
      _pid -> true
    end
  end

  defp dump_prior_ids(ids) do
    Enum.flat_map(ids, fn id ->
      case Ecto.UUID.dump(id) do
        {:ok, binary} -> [binary]
        :error -> []
      end
    end)
  end
end
