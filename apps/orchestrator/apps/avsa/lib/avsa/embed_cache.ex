defmodule AVSA.EmbedCache do
  @moduledoc """
  Per-request, content-addressed cache at the L1 embed boundary.

  ## Why this exists

  In the image-native MCP design, both `find_similar` and `extract_attributes`
  take an IMAGE and embed it internally by reaching L1 (the ViT `/embed`
  forward via `AVSA.EmbedStep`). Within a single conversation turn the model
  typically calls BOTH tools on the SAME image — which would run the (expensive)
  ViT forward twice. This cache memoises the embed result for the duration of a
  turn so the forward runs **exactly once per turn**.

  ## Where it lives in the stack

  The cache sits at L1 / the embed layer — strictly *below* the MCP boundary.
  It is reached only by the in-process tool implementations (`AVSA.MCP.Tools`)
  via `with_embed/4`; it is never exposed as an MCP method and its key never
  crosses the MCP wire. The QPS-optimised model+batcher path is *below* this
  cache and untouched: a cache miss calls the same `AVSA.EmbedStep` it always
  did.

  ## Key & scope

  The cache key is `{request_id, modality, sha256(image_bytes)}`:

    * `request_id` — an orchestrator-internal turn token (generated server-side,
      never client-controlled). Scoping by `request_id` means two different
      turns embedding the same bytes do NOT share a cache entry — there is no
      cross-turn leak, and a client cannot probe another turn's cache.
    * `modality` — `:image` or `:text`; the image and text encoders are
      distinct L1 forwards over the same content, so they are cached separately.
    * `sha256(image_bytes)` — content-addressing: identical bytes within the
      same turn collide (the hit we want); different bytes do not.

  Entries are bounded by a TTL (default 60 s) reaped lazily, and a turn's
  entries can be dropped eagerly via `purge_request/2` when the turn ends.

  Errors are NOT cached — a failed embed must be retryable on the next tool
  call within the same turn.
  """

  use GenServer

  require Logger

  @type embed_result :: term()
  @type modality :: :image | :text

  @default_ttl_ms 60_000

  # ── Client API ──────────────────────────────────────────────────────────────

  @doc """
  Start the cache. Registered under `:name` (defaults to `__MODULE__`).
  """
  @spec start_link(keyword()) :: GenServer.on_start()
  def start_link(opts \\ []) do
    name = Keyword.get(opts, :name, __MODULE__)
    GenServer.start_link(__MODULE__, opts, name: name)
  end

  @doc """
  Run `embed_fun` for `image_bytes` under `request_id`, memoising its result for
  the turn so a second call with the same bytes does not re-run it.

  `embed_fun` is a 0-arity function returning the embed result (any term); the
  contract is that a `{:error, _}` result is NOT cached (so it can be retried),
  while any other (success) result is cached for the turn.

  `modality` defaults to `:image`. Pass `:text` for text-encoder forwards.

  Returns the embed result verbatim (cached or freshly computed).
  """
  @spec with_embed(GenServer.server(), String.t(), binary(), (-> embed_result()), modality()) ::
          embed_result()
  def with_embed(server \\ __MODULE__, request_id, image_bytes, embed_fun, modality \\ :image)
      when is_binary(request_id) and is_binary(image_bytes) and is_function(embed_fun, 0) do
    key = key(request_id, modality, image_bytes)

    case GenServer.call(server, {:lookup, key}) do
      {:hit, value} ->
        :telemetry.execute([:avsa, :embed_cache, :hit], %{count: 1}, %{modality: modality})
        value

      :miss ->
        :telemetry.execute([:avsa, :embed_cache, :miss], %{count: 1}, %{modality: modality})
        value = embed_fun.()

        # Never cache a failure — the next tool call in the same turn retries.
        unless match?({:error, _}, value) do
          GenServer.call(server, {:store, request_id, key, value})
        end

        value
    end
  end

  @doc """
  Drop every cache entry for `request_id` (turn teardown). Idempotent.
  """
  @spec purge_request(GenServer.server(), String.t()) :: :ok
  def purge_request(server \\ __MODULE__, request_id) when is_binary(request_id) do
    GenServer.call(server, {:purge, request_id})
  end

  # ── Server callbacks ─────────────────────────────────────────────────────────

  @impl GenServer
  def init(opts) do
    ttl_ms = Keyword.get(opts, :ttl_ms, Application.get_env(:avsa, :embed_cache_ttl_ms, @default_ttl_ms))
    table = :ets.new(:avsa_embed_cache, [:set, :private])
    {:ok, %{table: table, ttl_ms: ttl_ms, by_request: %{}}}
  end

  @impl GenServer
  def handle_call({:lookup, key}, _from, state) do
    now = System.monotonic_time(:millisecond)

    case :ets.lookup(state.table, key) do
      [{^key, value, expires_at}] when expires_at > now ->
        {:reply, {:hit, value}, state}

      [{^key, _value, _expired}] ->
        :ets.delete(state.table, key)
        {:reply, :miss, state}

      [] ->
        {:reply, :miss, state}
    end
  end

  @impl GenServer
  def handle_call({:store, request_id, key, value}, _from, state) do
    expires_at = System.monotonic_time(:millisecond) + state.ttl_ms
    :ets.insert(state.table, {key, value, expires_at})

    by_request =
      Map.update(state.by_request, request_id, MapSet.new([key]), &MapSet.put(&1, key))

    {:reply, :ok, %{state | by_request: by_request}}
  end

  @impl GenServer
  def handle_call({:purge, request_id}, _from, state) do
    keys = Map.get(state.by_request, request_id, MapSet.new())
    Enum.each(keys, &:ets.delete(state.table, &1))
    {:reply, :ok, %{state | by_request: Map.delete(state.by_request, request_id)}}
  end

  # ── Helpers ───────────────────────────────────────────────────────────────────

  @spec key(String.t(), modality(), binary()) :: {String.t(), modality(), binary()}
  defp key(request_id, modality, image_bytes) do
    {request_id, modality, :crypto.hash(:sha256, image_bytes)}
  end
end
