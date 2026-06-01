defmodule AVSA.LLM.AnthropicTest do
  use ExUnit.Case, async: false

  setup do
    bypass = Bypass.open()
    Application.put_env(:avsa, :anthropic_base_url, "http://localhost:#{bypass.port}")
    on_exit(fn -> Application.delete_env(:avsa, :anthropic_base_url) end)
    System.put_env("AVSA_ANTHROPIC_API_KEY", "test-key")
    on_exit(fn -> System.delete_env("AVSA_ANTHROPIC_API_KEY") end)

    # Reset circuit breaker — CircuitBreakerTest may leave it disabled or blown
    # depending on which test ran last; reset ensures a clean slate here.
    try do
      :fuse.install(:anthropic_circuit, {{:standard, 3, 10_000}, {:reset, 60_000}})
    rescue
      _ -> :ok
    end

    :fuse.circuit_enable(:anthropic_circuit)
    :fuse.reset(:anthropic_circuit)

    {:ok, bypass: bypass}
  end

  @valid_response_body ~s({"id":"msg-1","type":"message","role":"assistant","content":[{"type":"tool_use","id":"tu-1","name":"find_similar","input":{}}],"model":"claude-haiku-4-5-20251001","stop_reason":"tool_use","usage":{"input_tokens":50,"output_tokens":10,"cache_read_input_tokens":0}})

  # ---------------------------------------------------------------------------
  # (i) Empty-manifest %{} → @find_similar_tool sent (never tools: [%{}])
  # ---------------------------------------------------------------------------

  test "empty-manifest %{} sends find_similar tool schema (not tools:[%{}])", %{bypass: bypass} do
    # Capture the request body to verify the tool schema sent to Anthropic.
    test_pid = self()

    Bypass.expect_once(bypass, "POST", "/v1/messages", fn conn ->
      {:ok, body, conn} = Plug.Conn.read_body(conn)
      send(test_pid, {:request_body, body})

      conn
      |> Plug.Conn.put_resp_content_type("application/json")
      |> Plug.Conn.send_resp(200, @valid_response_body)
    end)

    assert {:ok, _} = AVSA.LLM.Anthropic.call([], %{})

    assert_receive {:request_body, raw_body}
    body = Jason.decode!(raw_body)

    # tools must be a list with exactly one entry — not [%{}]
    assert [tool] = body["tools"]
    assert tool["name"] == "find_similar", "expected find_similar tool, got: #{inspect(tool)}"
    assert is_map(tool["input_schema"]), "tool must have an input_schema"
    assert is_map(tool["input_schema"]["properties"])

    # The tool must carry a real name — the 400 "tools.0.custom.name: Field required"
    # error is triggered precisely when this is absent or empty.
    assert tool["name"] != "" and tool["name"] != nil
  end

  # ---------------------------------------------------------------------------
  # (ii) Envelope unwrapping: call/2 returns the bare tool_use block
  #      matching AVSA.LLM.Mock's @default_response shape.
  # ---------------------------------------------------------------------------

  test "call/2 unwraps Anthropic envelope — returns %AVSA.LLM.ToolUse{} matching Mock shape",
       %{bypass: bypass} do
    # Response body with a rich tool_use block mirroring what the real API returns.
    attrs = %{
      "category" => "dress",
      "colour" => "red",
      "formality" => "formal",
      "occasion" => "evening"
    }

    response_body =
      Jason.encode!(%{
        "id" => "msg-2",
        "type" => "message",
        "role" => "assistant",
        "content" => [
          %{
            "type" => "tool_use",
            "id" => "tu-2",
            "name" => "find_similar",
            "input" => %{"attrs" => attrs}
          }
        ],
        "model" => "claude-haiku-4-5-20251001",
        "stop_reason" => "tool_use",
        "usage" => %{"input_tokens" => 30, "output_tokens" => 20, "cache_read_input_tokens" => 0}
      })

    Bypass.expect_once(bypass, "POST", "/v1/messages", fn conn ->
      conn
      |> Plug.Conn.put_resp_content_type("application/json")
      |> Plug.Conn.send_resp(200, response_body)
    end)

    assert {:ok, result} = AVSA.LLM.Anthropic.call([], %{})

    # Must be a typed struct — not the full envelope and not a raw string-keyed map
    assert %AVSA.LLM.ToolUse{} = result,
           "expected %AVSA.LLM.ToolUse{}, got: #{inspect(result)}"

    assert result.name == "find_similar"
    assert result.id == "tu-2"
    assert result.input["attrs"] == attrs

    # Struct fields: name, input, id — no envelope-level keys (content/usage must not exist)
    struct_keys = Map.keys(result) -- [:__struct__]
    refute :content in struct_keys,
           "envelope key :content must not appear in the unwrapped result"

    refute :usage in struct_keys,
           "envelope key :usage must not appear in the unwrapped result"
  end

  # ---------------------------------------------------------------------------
  # (iii-a) tool_choice "any" IS sent when tool_manifest is %{}
  # ---------------------------------------------------------------------------

  test "empty-manifest %{} includes tool_choice: {type: any} in request", %{bypass: bypass} do
    test_pid = self()

    Bypass.expect_once(bypass, "POST", "/v1/messages", fn conn ->
      {:ok, body, conn} = Plug.Conn.read_body(conn)
      send(test_pid, {:request_body, body})

      conn
      |> Plug.Conn.put_resp_content_type("application/json")
      |> Plug.Conn.send_resp(200, @valid_response_body)
    end)

    AVSA.LLM.Anthropic.call([], %{})

    assert_receive {:request_body, raw_body}
    body = Jason.decode!(raw_body)

    assert body["tool_choice"] == %{"type" => "any"},
           "tool_choice must be {type: any} for planning-step calls, got: #{inspect(body["tool_choice"])}"
  end

  # ---------------------------------------------------------------------------
  # (iii-b) tool_choice IS forced to the specific tool when a real manifest is given
  #
  # All current AVSA callers require a tool_use block, so tool_choice is always
  # forced. Without forcing tool_choice the model could return a text/end_turn
  # response, producing {:error, :no_tool_use}.
  # ---------------------------------------------------------------------------

  test "real tool manifest forces tool_choice to the specific tool name", %{bypass: bypass} do
    test_pid = self()

    extract_tool = %{
      "name" => "extract_attributes",
      "description" => "Extract attributes",
      "input_schema" => %{
        "type" => "object",
        "properties" => %{
          "formality" => %{"type" => "string"},
          "occasion" => %{"type" => "string"}
        },
        "required" => ["formality", "occasion"]
      }
    }

    # Response that matches an extract_attributes tool call
    response_body =
      Jason.encode!(%{
        "id" => "msg-3",
        "type" => "message",
        "role" => "assistant",
        "content" => [
          %{
            "type" => "tool_use",
            "id" => "tu-3",
            "name" => "extract_attributes",
            "input" => %{"formality" => "casual", "occasion" => "everyday"}
          }
        ],
        "model" => "claude-haiku-4-5-20251001",
        "stop_reason" => "tool_use",
        "usage" => %{"input_tokens" => 20, "output_tokens" => 10, "cache_read_input_tokens" => 0}
      })

    Bypass.expect_once(bypass, "POST", "/v1/messages", fn conn ->
      {:ok, body, conn} = Plug.Conn.read_body(conn)
      send(test_pid, {:request_body, body})

      conn
      |> Plug.Conn.put_resp_content_type("application/json")
      |> Plug.Conn.send_resp(200, response_body)
    end)

    assert {:ok, result} = AVSA.LLM.Anthropic.call([], extract_tool)

    assert_receive {:request_body, raw_body}
    body = Jason.decode!(raw_body)

    # tool_choice MUST be present and force the specific tool — this is what
    # prevents the LLM from returning a text/end_turn response.
    assert Map.has_key?(body, "tool_choice"),
           "tool_choice must be sent for real-tool calls to force a tool_use block"

    assert body["tool_choice"] == %{"type" => "tool", "name" => "extract_attributes"},
           "tool_choice must name the specific tool, got: #{inspect(body["tool_choice"])}"

    # The sent tool must be the real tool, not the find_similar fallback
    assert [tool] = body["tools"]
    assert tool["name"] == "extract_attributes"

    # Unwrapping still works for non-planning calls — returns typed struct
    assert %AVSA.LLM.ToolUse{name: "extract_attributes"} = result
  end

  # ---------------------------------------------------------------------------
  # (iv) No-tool-use text response returns {:error, :no_tool_use}
  #      (not the raw envelope, which no consumer can handle)
  # ---------------------------------------------------------------------------

  test "text-only Anthropic response (no tool_use block) returns {:error, :no_tool_use}",
       %{bypass: bypass} do
    # Anthropic can return stop_reason=end_turn with only text content when
    # tool_choice is not forced. Returning the raw envelope would silently
    # mis-parse in AVSA.AttributeTool.extract_attrs/1 (no pattern matches).
    text_response_body =
      Jason.encode!(%{
        "id" => "msg-4",
        "type" => "message",
        "role" => "assistant",
        "content" => [
          %{"type" => "text", "text" => "I cannot find similar products for this query."}
        ],
        "model" => "claude-haiku-4-5-20251001",
        "stop_reason" => "end_turn",
        "usage" => %{"input_tokens" => 10, "output_tokens" => 15, "cache_read_input_tokens" => 0}
      })

    Bypass.expect_once(bypass, "POST", "/v1/messages", fn conn ->
      conn
      |> Plug.Conn.put_resp_content_type("application/json")
      |> Plug.Conn.send_resp(200, text_response_body)
    end)

    # Even though tool_choice is forced for real manifests, the Bypass server
    # returns a text-only response regardless. This verifies the envelope-unwrapping
    # logic: when the provider ignores tool_choice and returns text, call/2 must
    # return {:error, :no_tool_use} rather than leaking the raw envelope.
    real_tool = %{
      "name" => "extract_attributes",
      "description" => "Extract",
      "input_schema" => %{"type" => "object", "properties" => %{}, "required" => []}
    }

    result = AVSA.LLM.Anthropic.call([], real_tool)

    assert result == {:error, :no_tool_use},
           "expected {:error, :no_tool_use} for text-only response, got: #{inspect(result)}"
  end

  # ---------------------------------------------------------------------------
  # (v) Force-tool assertion for the AttributeTool call site
  #
  # Drives AVSA.AttributeTool backed by AVSA.LLM.Anthropic (not the Mock) and
  # uses Bypass to capture the HTTP request. Asserts tool_choice forces
  # "extract_attributes" specifically — regression guard ensuring that for real
  # manifests Claude is constrained to a tool_use block rather than answering
  # with text (which would produce {:error, :no_tool_use}).
  # ---------------------------------------------------------------------------

  test "AttributeTool call site: Anthropic request includes tool_choice forcing extract_attributes",
       %{bypass: bypass} do
    test_pid = self()

    # Response matching an extract_attributes tool_use block
    response_body =
      Jason.encode!(%{
        "id" => "msg-attr-1",
        "type" => "message",
        "role" => "assistant",
        "content" => [
          %{
            "type" => "tool_use",
            "id" => "tu-attr-1",
            "name" => "extract_attributes",
            "input" => %{
              "category" => "dress",
              "colour" => "red",
              "formality" => "casual",
              "occasion" => "everyday"
            }
          }
        ],
        "model" => "claude-haiku-4-5-20251001",
        "stop_reason" => "tool_use",
        "usage" => %{"input_tokens" => 25, "output_tokens" => 12, "cache_read_input_tokens" => 0}
      })

    Bypass.expect_once(bypass, "POST", "/v1/messages", fn conn ->
      {:ok, body, conn} = Plug.Conn.read_body(conn)
      send(test_pid, {:request_body, body})

      conn
      |> Plug.Conn.put_resp_content_type("application/json")
      |> Plug.Conn.send_resp(200, response_body)
    end)

    # Start a test-owned AttributeTool backed by the REAL Anthropic module (not
    # the Mock) so the HTTP request is made to Bypass. The anthropic_base_url is
    # already pointing at bypass from the setup block.
    name = :"attr_tool_bypass_#{:erlang.unique_integer([:positive])}"

    start_supervised!(
      {AVSA.AttributeTool, [llm_module: AVSA.LLM.Anthropic, name: name]},
      id: name
    )

    assert {:ok, attrs} = GenServer.call(name, {:call, "a red dress", "red dress", nil})
    assert attrs["category"] == "dress"

    assert_receive {:request_body, raw_body}
    body = Jason.decode!(raw_body)

    # The core assertion: tool_choice must be present and force extract_attributes.
    assert Map.has_key?(body, "tool_choice"),
           "tool_choice must be present in the AttributeTool→Anthropic HTTP request"

    assert body["tool_choice"] == %{"type" => "tool", "name" => "extract_attributes"},
           "tool_choice must force extract_attributes specifically, got: #{inspect(body["tool_choice"])}"

    # The tool schema must be the real extract_attributes (not find_similar)
    assert [tool] = body["tools"]
    assert tool["name"] == "extract_attributes"
  end
end
