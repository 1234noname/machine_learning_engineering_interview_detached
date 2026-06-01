defmodule AVSA.EmbedStepTest do
  use ExUnit.Case, async: false

  # EmbedStep ↔ batcher contract.
  #
  # The batcher's caller-facing /embed route (crates/batcher/src/routes/embed.rs) is
  # SINGULAR:
  #   request:  {"image_bytes": "<base64>"}
  #   response: {"embedding": [f32; 768],
  #              "attributes": {category, colour, category_confidence, colour_confidence}}
  #
  # These tests pin that contract and the EmbedStep return shape:
  #   {:ok, %{embedding: [float()], attributes: map() | nil}}
  # where `attributes` is the ViT attribute map, or nil when the batcher omits it
  # (e.g. a stub without the attribute head) — graceful, no crash.

  setup do
    bypass = Bypass.open()

    # Override batcher_url to point at the bypass server for this test
    original_url = Application.get_env(:avsa, :batcher_url)
    Application.put_env(:avsa, :batcher_url, "http://localhost:#{bypass.port}")

    # Ensure the circuit breaker is installed and reset to a closed state
    # before each test (the circuit is installed by AVSA.Application.start/2,
    # but may be blown from a previous test run).
    try do
      :fuse.install(:batcher_circuit, {{:standard, 5, 10_000}, {:reset, 60_000}})
    rescue
      _ -> :ok
    end

    :fuse.reset(:batcher_circuit)

    on_exit(fn ->
      Bypass.down(bypass)

      case original_url do
        nil -> Application.delete_env(:avsa, :batcher_url)
        url -> Application.put_env(:avsa, :batcher_url, url)
      end
    end)

    {:ok, bypass: bypass}
  end

  test "POSTs the singular {\"image_bytes\": <base64>} request shape", %{bypass: bypass} do
    embedding = List.duplicate(0.1, 768)
    test_pid = self()

    Bypass.expect_once(bypass, "POST", "/embed", fn conn ->
      {:ok, raw_body, conn} = Plug.Conn.read_body(conn)
      send(test_pid, {:request_body, raw_body})

      Plug.Conn.resp(
        conn,
        200,
        Jason.encode!(%{
          embedding: embedding,
          attributes: %{
            category: "dress",
            colour: "red",
            category_confidence: 0.9,
            colour_confidence: 0.8
          }
        })
      )
    end)

    assert {:ok, _result} = AVSA.EmbedStep.call(<<1, 2, 3>>)

    assert_receive {:request_body, raw_body}, 1000
    decoded = Jason.decode!(raw_body)

    # Singular key, no "images" plural array.
    assert Map.has_key?(decoded, "image_bytes")
    refute Map.has_key?(decoded, "images")
    assert decoded["image_bytes"] == Base.encode64(<<1, 2, 3>>)
  end

  test "decodes singular embedding and attributes on 200 response", %{bypass: bypass} do
    embedding = List.duplicate(0.1, 768)

    Bypass.expect_once(bypass, "POST", "/embed", fn conn ->
      Plug.Conn.resp(
        conn,
        200,
        Jason.encode!(%{
          embedding: embedding,
          attributes: %{
            category: "dress",
            colour: "red",
            category_confidence: 0.91,
            colour_confidence: 0.77
          }
        })
      )
    end)

    assert {:ok, %{embedding: result_embedding, attributes: attributes}} =
             AVSA.EmbedStep.call(<<1, 2, 3>>)

    assert length(result_embedding) == 768
    assert Enum.all?(result_embedding, &(&1 == 0.1))

    assert attributes["category"] == "dress"
    assert attributes["colour"] == "red"
    assert attributes["category_confidence"] == 0.91
    assert attributes["colour_confidence"] == 0.77
  end

  test "returns embedding with nil attributes when batcher omits attributes (stub)", %{
    bypass: bypass
  } do
    # A stub batcher without the ViT attribute head returns only the embedding.
    # EmbedStep must degrade gracefully: embedding present, attributes nil — no crash.
    embedding = List.duplicate(0.2, 768)

    Bypass.expect_once(bypass, "POST", "/embed", fn conn ->
      Plug.Conn.resp(conn, 200, Jason.encode!(%{embedding: embedding}))
    end)

    assert {:ok, %{embedding: result_embedding, attributes: attributes}} =
             AVSA.EmbedStep.call(<<1, 2, 3>>)

    assert length(result_embedding) == 768
    assert attributes in [nil, %{}]
  end

  test "returns error on 500 response", %{bypass: bypass} do
    Bypass.expect_once(bypass, "POST", "/embed", fn conn ->
      Plug.Conn.resp(conn, 500, "internal server error")
    end)

    assert {:error, {:http_error, 500}} = AVSA.EmbedStep.call(<<1, 2, 3>>)
  end

  test "returns error when response has unexpected shape", %{bypass: bypass} do
    Bypass.expect_once(bypass, "POST", "/embed", fn conn ->
      Plug.Conn.resp(conn, 200, Jason.encode!(%{unexpected_key: "value"}))
    end)

    assert {:error, :bad_response} = AVSA.EmbedStep.call(<<1, 2, 3>>)
  end

  test "returns circuit_open when fuse is REALLY blown (real melt + :fuse.ask)" do
    # REAL blown-fuse path (mirrors text_tool_test.exs): install a one-shot
    # circuit and melt it past threshold so :fuse.ask reports :blown, then assert
    # EmbedStep translates that to {:error, :circuit_open} without making a
    # request.
    :fuse.install(:batcher_circuit, {{:standard, 1, 60_000}, {:reset, 3_600_000}})
    :fuse.melt(:batcher_circuit)
    # One more melt to trip the breaker (standard = open after threshold+1 melts).
    :fuse.melt(:batcher_circuit)

    assert :blown == :fuse.ask(:batcher_circuit, :sync)
    assert {:error, :circuit_open} = AVSA.EmbedStep.call(<<1, 2, 3>>)
  end

  # ---------------------------------------------------------------------------
  # Observability — avsa_attribute_prediction_total{attribute, label}
  # + avsa_attribute_confidence{attribute}, emitted by EmbedStep when the batcher
  # surfaces attributes. REAL test: real EmbedStep.call → real Finch request to a
  # real Bypass batcher returning attributes → real telemetry emit, observed by a
  # real handler.
  # ---------------------------------------------------------------------------

  test "emits prediction + confidence metrics with the right labels when batcher returns attributes",
       %{bypass: bypass} do
    embedding = List.duplicate(0.1, 768)

    Bypass.expect_once(bypass, "POST", "/embed", fn conn ->
      Plug.Conn.resp(
        conn,
        200,
        Jason.encode!(%{
          embedding: embedding,
          attributes: %{
            category: "dress",
            colour: "red",
            category_confidence: 0.91,
            colour_confidence: 0.77
          }
        })
      )
    end)

    test_pid = self()
    handler_id = "embed-attr-metrics-#{:erlang.unique_integer([:positive])}"

    :telemetry.attach_many(
      handler_id,
      [
        [:avsa, :attribute, :prediction],
        [:avsa, :attribute, :confidence]
      ],
      fn event, measurements, metadata, _config ->
        send(test_pid, {:embed_metric, event, measurements, metadata})
      end,
      nil
    )

    on_exit(fn -> :telemetry.detach(handler_id) end)

    assert {:ok, _result} = AVSA.EmbedStep.call(<<1, 2, 3>>)

    events = collect_embed_metrics(300)

    predictions =
      for {[:avsa, :attribute, :prediction], _m, %{attribute: a, label: l}} <- events, do: {a, l}

    assert {"category", "dress"} in predictions
    assert {"colour", "red"} in predictions

    confidences =
      for {[:avsa, :attribute, :confidence], %{confidence: c}, %{attribute: a}} <- events,
          do: {a, c}

    assert {"category", 0.91} in confidences
    assert {"colour", 0.77} in confidences
  end

  test "does not emit attribute metrics when batcher omits attributes (stub)", %{bypass: bypass} do
    embedding = List.duplicate(0.2, 768)

    Bypass.expect_once(bypass, "POST", "/embed", fn conn ->
      Plug.Conn.resp(conn, 200, Jason.encode!(%{embedding: embedding}))
    end)

    test_pid = self()
    handler_id = "embed-attr-none-#{:erlang.unique_integer([:positive])}"

    :telemetry.attach_many(
      handler_id,
      [
        [:avsa, :attribute, :prediction],
        [:avsa, :attribute, :confidence]
      ],
      fn event, _measurements, _metadata, _config ->
        send(test_pid, {:embed_metric_fired, event})
      end,
      nil
    )

    on_exit(fn -> :telemetry.detach(handler_id) end)

    assert {:ok, _result} = AVSA.EmbedStep.call(<<1, 2, 3>>)

    refute_receive {:embed_metric_fired, _}, 200
  end

  # ---------------------------------------------------------------------------
  # Circuit-breaker melt observability — avsa_circuit_melt_total{breaker}.
  # REAL test: a real failed request (Bypass returns 500) melts the real
  # :batcher_circuit, which emits the melt event observed by a real handler.
  # ---------------------------------------------------------------------------

  test "emits [:avsa, :circuit, :melt] with breaker=batcher_circuit on a real failed request",
       %{bypass: bypass} do
    Bypass.expect_once(bypass, "POST", "/embed", fn conn ->
      Plug.Conn.resp(conn, 500, "internal server error")
    end)

    test_pid = self()
    handler_id = "embed-circuit-melt-#{:erlang.unique_integer([:positive])}"

    :telemetry.attach(
      handler_id,
      [:avsa, :circuit, :melt],
      fn _event, measurements, metadata, _config ->
        send(test_pid, {:circuit_melt, measurements, metadata})
      end,
      nil
    )

    on_exit(fn -> :telemetry.detach(handler_id) end)

    assert {:error, {:http_error, 500}} = AVSA.EmbedStep.call(<<1, 2, 3>>)

    assert_receive {:circuit_melt, %{count: 1}, %{breaker: "batcher_circuit"}}, 500
  end

  defp collect_embed_metrics(timeout), do: collect_embed_metrics(timeout, [])

  defp collect_embed_metrics(timeout, acc) do
    receive do
      {:embed_metric, event, measurements, metadata} ->
        collect_embed_metrics(timeout, [{event, measurements, metadata} | acc])
    after
      timeout -> Enum.reverse(acc)
    end
  end
end
