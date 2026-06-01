defmodule AVSA.LLM.CircuitBreakerTest do
  use ExUnit.Case, async: false

  @valid_body ~s({"id":"msg-1","type":"message","role":"assistant","content":[{"type":"tool_use","id":"tu-1","name":"find_similar","input":{}}],"model":"claude-haiku-4-5-20251001","stop_reason":"tool_use","usage":{"input_tokens":50,"output_tokens":10,"cache_read_input_tokens":0}})

  setup do
    bypass = Bypass.open()
    Application.put_env(:avsa, :anthropic_base_url, "http://localhost:#{bypass.port}")
    System.put_env("AVSA_ANTHROPIC_API_KEY", "test-key")

    # Ensure the circuit is installed, re-enabled, and fully reset before each test.
    # circuit_enable/1 is needed to recover from circuit_disable/1 (reset/1 alone does not
    # re-enable a manually disabled fuse).
    try do
      :fuse.install(:anthropic_circuit, {{:standard, 3, 10_000}, {:reset, 60_000}})
    rescue
      _ -> :ok
    end

    :fuse.circuit_enable(:anthropic_circuit)
    :fuse.reset(:anthropic_circuit)

    on_exit(fn ->
      Bypass.down(bypass)
      Application.delete_env(:avsa, :anthropic_base_url)
      System.delete_env("AVSA_ANTHROPIC_API_KEY")
    end)

    {:ok, bypass: bypass}
  end

  test "circuit is closed by default — successful call returns {:ok, _}", %{bypass: bypass} do
    Bypass.expect_once(bypass, "POST", "/v1/messages", fn conn ->
      conn
      |> Plug.Conn.put_resp_content_type("application/json")
      |> Plug.Conn.send_resp(200, @valid_body)
    end)

    assert {:ok, _} = AVSA.LLM.Anthropic.call([], %{})
  end

  test "call returns the underlying error when circuit is closed and request fails",
       %{bypass: bypass} do
    Bypass.expect_once(bypass, "POST", "/v1/messages", fn conn ->
      Plug.Conn.send_resp(conn, 500, "internal server error")
    end)

    assert {:error, _reason} = AVSA.LLM.Anthropic.call([], %{})
  end

  test "call returns {:error, :circuit_open} when the circuit is blown", _ctx do
    # Force the circuit open without relying on melt counting timing.
    :fuse.circuit_disable(:anthropic_circuit)

    assert {:error, :circuit_open} = AVSA.LLM.Anthropic.call([], %{})
  end

  test "a failure melts the fuse — circuit eventually opens under sustained errors",
       %{bypass: bypass} do
    # Each 500 response melts the fuse once. After the threshold (3) the circuit opens.
    Bypass.expect(bypass, "POST", "/v1/messages", fn conn ->
      Plug.Conn.send_resp(conn, 500, "internal server error")
    end)

    # Drive failures until the circuit opens (max attempts well above threshold).
    result =
      Enum.reduce_while(1..10, :closed, fn _i, _acc ->
        case AVSA.LLM.Anthropic.call([], %{}) do
          {:error, :circuit_open} -> {:halt, :open}
          {:error, _other} -> {:cont, :closed}
        end
      end)

    assert result == :open, "circuit did not open after repeated failures"
  end

  test "successful call after circuit_enable + reset re-closes the circuit", %{bypass: bypass} do
    :fuse.circuit_disable(:anthropic_circuit)
    assert {:error, :circuit_open} = AVSA.LLM.Anthropic.call([], %{})

    :fuse.circuit_enable(:anthropic_circuit)
    :fuse.reset(:anthropic_circuit)

    Bypass.expect_once(bypass, "POST", "/v1/messages", fn conn ->
      conn
      |> Plug.Conn.put_resp_content_type("application/json")
      |> Plug.Conn.send_resp(200, @valid_body)
    end)

    assert {:ok, _} = AVSA.LLM.Anthropic.call([], %{})
  end
end
