defmodule AVSA.LLM.ConformanceTest do
  @moduledoc """
  Cross-implementation conformance test: asserts that BOTH `AVSA.LLM.Mock` and
  `AVSA.LLM.Anthropic` return the same typed `%AVSA.LLM.ToolUse{}` shape for the
  same input, as required by the `AVSA.LLM` callback.

  This is the regression guard that makes future shape-drift between implementations
  a test failure rather than a silent production bug.

  - `AVSA.LLM.Mock`: called directly; returns the struct from its @default_response.
  - `AVSA.LLM.Anthropic`: driven via Bypass returning a realistic Anthropic JSON envelope;
    the impl must unwrap the envelope and build the same struct shape.

  Both paths assert:
    - result is `{:ok, %AVSA.LLM.ToolUse{}}`
    - `:name` is a non-empty string
    - `:id` is a non-empty string
    - `:input` is a non-empty map
    - No string-keyed map is returned
  """

  use ExUnit.Case, async: false

  # ---------------------------------------------------------------------------
  # Shared Anthropic Bypass envelope for a find_similar tool_use response.
  # Mirrors exactly what the real Anthropic API returns so the unwrapping logic
  # in extract_first_tool_use/1 is exercised end-to-end.
  # ---------------------------------------------------------------------------

  @anthropic_attrs %{
    "category" => "dress",
    "colour" => "red",
    "formality" => "formal",
    "occasion" => "evening"
  }

  @anthropic_envelope Jason.encode!(%{
                        "id" => "msg-conf-1",
                        "type" => "message",
                        "role" => "assistant",
                        "content" => [
                          %{
                            "type" => "tool_use",
                            "id" => "tu-conf-1",
                            "name" => "find_similar",
                            "input" => %{"attrs" => %{
                              "category" => "dress",
                              "colour" => "red",
                              "formality" => "formal",
                              "occasion" => "evening"
                            }}
                          }
                        ],
                        "model" => "claude-haiku-4-5-20251001",
                        "stop_reason" => "tool_use",
                        "usage" => %{
                          "input_tokens" => 42,
                          "output_tokens" => 18,
                          "cache_read_input_tokens" => 0
                        }
                      })

  # ---------------------------------------------------------------------------
  # Setup for Anthropic: open Bypass, point the module at it, set a dummy key.
  # ---------------------------------------------------------------------------

  setup do
    on_exit(fn -> Agent.update(AVSA.LLM.Mock, fn _ -> nil end) end)

    bypass = Bypass.open()
    Application.put_env(:avsa, :anthropic_base_url, "http://localhost:#{bypass.port}")
    on_exit(fn -> Application.delete_env(:avsa, :anthropic_base_url) end)
    System.put_env("AVSA_ANTHROPIC_API_KEY", "test-conformance-key")
    on_exit(fn -> System.delete_env("AVSA_ANTHROPIC_API_KEY") end)

    # Ensure the circuit breaker is healthy so a previous blown state does not
    # affect conformance assertions.
    try do
      :fuse.install(:anthropic_circuit, {{:standard, 3, 10_000}, {:reset, 60_000}})
    rescue
      _ -> :ok
    end

    :fuse.circuit_enable(:anthropic_circuit)
    :fuse.reset(:anthropic_circuit)

    {:ok, bypass: bypass}
  end

  # ---------------------------------------------------------------------------
  # Helper: assert a value is a well-formed ToolUse result (both impls must pass)
  # ---------------------------------------------------------------------------

  defp assert_tool_use_shape({:ok, %AVSA.LLM.ToolUse{name: name, id: id, input: input}}) do
    assert is_binary(name) and name != "",
           "ToolUse.name must be a non-empty string, got: #{inspect(name)}"

    assert is_binary(id) and id != "",
           "ToolUse.id must be a non-empty string, got: #{inspect(id)}"

    assert is_map(input) and map_size(input) > 0,
           "ToolUse.input must be a non-empty map, got: #{inspect(input)}"
  end

  defp assert_tool_use_shape(other) do
    flunk(
      "Expected {:ok, %AVSA.LLM.ToolUse{}}, got: #{inspect(other)}. " <>
        "This is a shape-drift bug: one implementation returned a different type."
    )
  end

  # ---------------------------------------------------------------------------
  # Conformance: Mock returns the typed struct
  # ---------------------------------------------------------------------------

  test "AVSA.LLM.Mock returns {:ok, %AVSA.LLM.ToolUse{}} with find_similar" do
    result = AVSA.LLM.Mock.call([], %{})

    assert_tool_use_shape(result)

    {:ok, tool_use} = result
    assert tool_use.name == "find_similar",
           "Mock default should return find_similar tool"
  end

  # ---------------------------------------------------------------------------
  # Conformance: Anthropic returns the typed struct after envelope unwrapping
  # ---------------------------------------------------------------------------

  test "AVSA.LLM.Anthropic returns {:ok, %AVSA.LLM.ToolUse{}} with find_similar (via Bypass)",
       %{bypass: bypass} do
    Bypass.expect_once(bypass, "POST", "/v1/messages", fn conn ->
      conn
      |> Plug.Conn.put_resp_content_type("application/json")
      |> Plug.Conn.send_resp(200, @anthropic_envelope)
    end)

    result = AVSA.LLM.Anthropic.call([], %{})

    assert_tool_use_shape(result)

    {:ok, tool_use} = result
    assert tool_use.name == "find_similar",
           "Anthropic should return find_similar tool from Bypass envelope"

    assert tool_use.id == "tu-conf-1",
           "Anthropic should preserve the tool_use id from the envelope"

    assert tool_use.input["attrs"] == @anthropic_attrs,
           "Anthropic should preserve the nested attrs from the envelope"
  end

  # ---------------------------------------------------------------------------
  # Conformance: Both impls return structurally identical type (same struct module)
  # ---------------------------------------------------------------------------

  test "both Mock and Anthropic return the same struct module (no drift)", %{bypass: bypass} do
    # Mock result
    mock_result = AVSA.LLM.Mock.call([], %{})

    # Anthropic result (via Bypass)
    Bypass.expect_once(bypass, "POST", "/v1/messages", fn conn ->
      conn
      |> Plug.Conn.put_resp_content_type("application/json")
      |> Plug.Conn.send_resp(200, @anthropic_envelope)
    end)

    anthropic_result = AVSA.LLM.Anthropic.call([], %{})

    # Both must be {:ok, struct}
    assert {:ok, %AVSA.LLM.ToolUse{} = mock_tool_use} = mock_result,
           "Mock must return {:ok, %AVSA.LLM.ToolUse{}}, got: #{inspect(mock_result)}"

    assert {:ok, %AVSA.LLM.ToolUse{} = anthropic_tool_use} = anthropic_result,
           "Anthropic must return {:ok, %AVSA.LLM.ToolUse{}}, got: #{inspect(anthropic_result)}"

    # Both must have the same struct module
    assert mock_tool_use.__struct__ == anthropic_tool_use.__struct__,
           "Mock and Anthropic returned different struct modules: " <>
             "#{mock_tool_use.__struct__} vs #{anthropic_tool_use.__struct__}"

    # Both must have the same atom-keyed fields (not string-keyed maps)
    mock_keys = mock_tool_use |> Map.keys() |> MapSet.new()
    anthropic_keys = anthropic_tool_use |> Map.keys() |> MapSet.new()

    assert mock_keys == anthropic_keys,
           "Field sets differ between Mock and Anthropic ToolUse structs: " <>
             "Mock=#{inspect(MapSet.to_list(mock_keys))}, Anthropic=#{inspect(MapSet.to_list(anthropic_keys))}"

    # Neither must return a plain string-keyed map
    refute is_map(mock_tool_use) and Map.has_key?(mock_tool_use, "type"),
           "Mock returned a string-keyed map instead of a struct (drift regression)"

    refute is_map(anthropic_tool_use) and Map.has_key?(anthropic_tool_use, "type"),
           "Anthropic returned a string-keyed map instead of a struct (drift regression)"
  end
end
