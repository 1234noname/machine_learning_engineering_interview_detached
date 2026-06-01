defmodule AVSA.EmbedCacheTest do
  @moduledoc """
  Tests for AVSA.EmbedCache — the per-request content-addressed embed cache that
  is the key seam of the image-native MCP design.

  Within ONE turn, both `find_similar` and `extract_attributes` embed the SAME
  image. The cache lives at the L1 embed boundary so the (expensive) ViT forward
  runs exactly ONCE per turn, not once per tool. These tests pin that invariant:

    * `with_embed/3` runs the embed function on a cold key (cache miss).
    * `with_embed/3` does NOT re-run the embed function on a warm key
      (cache hit) — proving "one ViT forward per turn".
    * The key is content-addressed: the same bytes under the same request_id
      collide (hit); different bytes under the same request_id do not.
    * The cache is request-scoped: the same bytes under a DIFFERENT request_id
      do NOT collide (each turn embeds independently — no cross-turn leak).
    * `purge_request/1` drops a turn's entries so memory is bounded.
  """

  use ExUnit.Case, async: false

  setup do
    # A clean, isolated cache per test. The named server is started by the
    # application; in the unit env we (re)start a fresh one under a unique name
    # to avoid cross-test contamination.
    name = :"embed_cache_#{:erlang.unique_integer([:positive])}"
    {:ok, pid} = AVSA.EmbedCache.start_link(name: name)
    on_exit(fn -> if Process.alive?(pid), do: GenServer.stop(pid) end)
    {:ok, cache: name}
  end

  test "runs the embed function once on a cold key (cache miss)", %{cache: cache} do
    test_pid = self()

    fun = fn ->
      send(test_pid, :embedded)
      {:ok, %{embedding: List.duplicate(0.5, 768), attributes: nil}}
    end

    assert {:ok, %{embedding: emb}} =
             AVSA.EmbedCache.with_embed(cache, "req-1", <<1, 2, 3>>, fun)

    assert length(emb) == 768
    assert_received :embedded
  end

  test "does NOT re-run the embed function on a warm key — one ViT forward per turn",
       %{cache: cache} do
    test_pid = self()
    image = <<10, 20, 30, 40>>

    fun = fn ->
      # Each real forward increments a counter the test observes.
      send(test_pid, :forward)
      {:ok, %{embedding: List.duplicate(0.25, 768), attributes: %{"category" => "dress"}}}
    end

    # Two tools in the SAME turn embed the SAME image.
    assert {:ok, r1} = AVSA.EmbedCache.with_embed(cache, "turn-A", image, fun)
    assert {:ok, r2} = AVSA.EmbedCache.with_embed(cache, "turn-A", image, fun)

    # Identical memoised result.
    assert r1 == r2

    # The forward ran EXACTLY once across the two tool calls.
    assert_received :forward
    refute_received :forward
  end

  test "different image bytes under the same request do not collide", %{cache: cache} do
    counter = :counters.new(1, [])

    fun = fn ->
      :counters.add(counter, 1, 1)
      {:ok, %{embedding: List.duplicate(0.1, 768), attributes: nil}}
    end

    assert {:ok, _} = AVSA.EmbedCache.with_embed(cache, "turn-B", <<1>>, fun)
    assert {:ok, _} = AVSA.EmbedCache.with_embed(cache, "turn-B", <<2>>, fun)

    # Two distinct images => two forwards.
    assert :counters.get(counter, 1) == 2
  end

  test "same bytes under a different request do not collide (request-scoped)", %{cache: cache} do
    counter = :counters.new(1, [])
    image = <<9, 9, 9>>

    fun = fn ->
      :counters.add(counter, 1, 1)
      {:ok, %{embedding: List.duplicate(0.3, 768), attributes: nil}}
    end

    assert {:ok, _} = AVSA.EmbedCache.with_embed(cache, "turn-C", image, fun)
    assert {:ok, _} = AVSA.EmbedCache.with_embed(cache, "turn-D", image, fun)

    # Same bytes, different turns => two forwards (no cross-turn leak).
    assert :counters.get(counter, 1) == 2
  end

  test "an embed error is NOT cached (the next call retries)", %{cache: cache} do
    counter = :counters.new(1, [])

    fun = fn ->
      n = :counters.get(counter, 1)
      :counters.add(counter, 1, 1)
      if n == 0, do: {:error, :boom}, else: {:ok, %{embedding: [], attributes: nil}}
    end

    image = <<7, 7>>
    assert {:error, :boom} = AVSA.EmbedCache.with_embed(cache, "turn-E", image, fun)
    # The error must not poison the cache — the retry runs the forward again.
    assert {:ok, _} = AVSA.EmbedCache.with_embed(cache, "turn-E", image, fun)
    assert :counters.get(counter, 1) == 2
  end

  test "purge_request/2 drops a turn's entries so a later identical call re-embeds",
       %{cache: cache} do
    counter = :counters.new(1, [])

    fun = fn ->
      :counters.add(counter, 1, 1)
      {:ok, %{embedding: List.duplicate(0.5, 768), attributes: nil}}
    end

    image = <<4, 5, 6>>
    assert {:ok, _} = AVSA.EmbedCache.with_embed(cache, "turn-F", image, fun)
    assert :ok = AVSA.EmbedCache.purge_request(cache, "turn-F")
    assert {:ok, _} = AVSA.EmbedCache.with_embed(cache, "turn-F", image, fun)

    # Purge between calls => the forward runs twice.
    assert :counters.get(counter, 1) == 2
  end
end
