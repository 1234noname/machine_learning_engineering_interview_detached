defmodule AVSA.TextToolTest do
  @moduledoc """
  Unit tests for AVSA.TextTool.

  Uses Bypass to stub the model server's /embed_text endpoint.
  The circuit breaker is installed/reset before each test so tests are isolated.
  """

  use ExUnit.Case, async: false

  setup do
    bypass = Bypass.open()

    original_url = Application.get_env(:avsa, :model_url)
    Application.put_env(:avsa, :model_url, "http://localhost:#{bypass.port}")

    # Ensure the text encoder circuit is installed and reset before each test.
    try do
      :fuse.install(:text_encoder_circuit, {{:standard, 5, 10_000}, {:reset, 60_000}})
    rescue
      _ -> :ok
    end

    :fuse.reset(:text_encoder_circuit)

    on_exit(fn ->
      Bypass.down(bypass)

      case original_url do
        nil -> Application.delete_env(:avsa, :model_url)
        url -> Application.put_env(:avsa, :model_url, url)
      end
    end)

    {:ok, bypass: bypass}
  end

  test "returns {:ok, embedding} of length 512 on 200 response", %{bypass: bypass} do
    embedding = List.duplicate(0.1, 512)

    Bypass.expect_once(bypass, "POST", "/embed_text", fn conn ->
      Plug.Conn.resp(conn, 200, Jason.encode!(%{embeddings: [embedding]}))
    end)

    assert {:ok, result} = AVSA.TextTool.call("a red dress")
    assert length(result) == 512
    assert Enum.all?(result, &(&1 == 0.1))
  end

  test "returns {:error, {:http_error, status}} on non-200 response", %{bypass: bypass} do
    Bypass.expect_once(bypass, "POST", "/embed_text", fn conn ->
      Plug.Conn.resp(conn, 500, "internal server error")
    end)

    assert {:error, {:http_error, 500}} = AVSA.TextTool.call("a red dress")
  end

  test "returns {:error, :circuit_open} when circuit is blown" do
    # Blow the circuit by melting it past the threshold.
    # Install a one-shot circuit for isolation within this test.
    :fuse.install(:text_encoder_circuit, {{:standard, 1, 60_000}, {:reset, 3_600_000}})
    :fuse.melt(:text_encoder_circuit)
    # One more melt to trip the breaker (standard = open after threshold+1 melts).
    :fuse.melt(:text_encoder_circuit)

    assert {:error, :circuit_open} = AVSA.TextTool.call("a red dress")
  end

  test "returns {:error, :bad_response} when response JSON is malformed", %{bypass: bypass} do
    Bypass.expect_once(bypass, "POST", "/embed_text", fn conn ->
      Plug.Conn.resp(conn, 200, Jason.encode!(%{unexpected_key: "value"}))
    end)

    assert {:error, :bad_response} = AVSA.TextTool.call("a red dress")
  end

  # ---------------------------------------------------------------------------
  # avsa_text_embed_latency_seconds / avsa_text_embed_error_total metrics —
  # REAL TextTool code path (real Finch request to a real Bypass server, real
  # telemetry emit). These are the events the two declared metrics bind to:
  #   [:avsa, :text_embed, :complete] measurement :latency_ms -> latency histogram
  #   [:avsa, :text_embed, :error]    -> error counter
  # No mock of :telemetry — a real handler is attached around the real call.
  # ---------------------------------------------------------------------------

  test "emits [:avsa, :text_embed, :complete] with latency_ms on a real successful embed",
       %{bypass: bypass} do
    embedding = List.duplicate(0.1, 512)

    Bypass.expect_once(bypass, "POST", "/embed_text", fn conn ->
      Plug.Conn.resp(conn, 200, Jason.encode!(%{embeddings: [embedding]}))
    end)

    test_pid = self()
    handler_id = "text-embed-complete-#{:erlang.unique_integer()}"

    :telemetry.attach(
      handler_id,
      [:avsa, :text_embed, :complete],
      fn event, measurements, metadata, _config ->
        send(test_pid, {:text_embed_event, event, measurements, metadata})
      end,
      nil
    )

    on_exit(fn -> :telemetry.detach(handler_id) end)

    assert {:ok, _result} = AVSA.TextTool.call("a red dress")

    assert_receive {:text_embed_event, [:avsa, :text_embed, :complete], measurements, _metadata}, 500
    assert is_integer(measurements.latency_ms)
    assert measurements.latency_ms >= 0
  end

  test "emits [:avsa, :text_embed, :error] with reason on a real failed embed",
       %{bypass: bypass} do
    Bypass.expect_once(bypass, "POST", "/embed_text", fn conn ->
      Plug.Conn.resp(conn, 500, "internal server error")
    end)

    test_pid = self()
    handler_id = "text-embed-error-#{:erlang.unique_integer()}"

    :telemetry.attach(
      handler_id,
      [:avsa, :text_embed, :error],
      fn event, measurements, metadata, _config ->
        send(test_pid, {:text_embed_error, event, measurements, metadata})
      end,
      nil
    )

    on_exit(fn -> :telemetry.detach(handler_id) end)

    assert {:error, {:http_error, 500}} = AVSA.TextTool.call("a red dress")

    assert_receive {:text_embed_error, [:avsa, :text_embed, :error], _measurements, metadata}, 500
    # reason is a real inspect()'d error term from the real failed request.
    assert is_binary(metadata.reason)
    assert metadata.reason =~ "http_error"
  end

  # ---------------------------------------------------------------------------
  # Circuit-breaker melt observability — avsa_circuit_melt_total{breaker}.
  # REAL test: a real failed /embed_text request melts the real
  # :text_encoder_circuit, emitting [:avsa, :circuit, :melt] observed by a real
  # handler.
  # ---------------------------------------------------------------------------

  test "emits [:avsa, :circuit, :melt] with breaker=text_encoder_circuit on a real failed embed",
       %{bypass: bypass} do
    Bypass.expect_once(bypass, "POST", "/embed_text", fn conn ->
      Plug.Conn.resp(conn, 500, "internal server error")
    end)

    test_pid = self()
    handler_id = "text-circuit-melt-#{:erlang.unique_integer()}"

    :telemetry.attach(
      handler_id,
      [:avsa, :circuit, :melt],
      fn _event, measurements, metadata, _config ->
        send(test_pid, {:circuit_melt, measurements, metadata})
      end,
      nil
    )

    on_exit(fn -> :telemetry.detach(handler_id) end)

    assert {:error, {:http_error, 500}} = AVSA.TextTool.call("a red dress")

    assert_receive {:circuit_melt, %{count: 1}, %{breaker: "text_encoder_circuit"}}, 500
  end
end
