defmodule ElixirAnthropic.MessagesTest do
  use ExUnit.Case, async: false

  setup do
    bypass = Bypass.open()
    {:ok, bypass: bypass}
  end

  test "messages/2 with valid params POSTs to /v1/messages and returns {:ok, map} on 200",
       %{bypass: bypass} do
    Bypass.expect_once(bypass, "POST", "/v1/messages", fn conn ->
      Plug.Conn.resp(
        conn,
        200,
        Jason.encode!(%{
          "id" => "msg_123",
          "type" => "message",
          "role" => "assistant",
          "content" => [%{"type" => "text", "text" => "Hello!"}],
          "model" => "claude-haiku-4-5-20251001",
          "stop_reason" => "end_turn",
          "usage" => %{"input_tokens" => 10, "output_tokens" => 5}
        })
      )
    end)

    client =
      ElixirAnthropic.new(
        api_key: "test-key",
        base_url: "http://localhost:#{bypass.port}"
      )

    assert {:ok, %{"content" => _}} =
             ElixirAnthropic.messages(client, %{
               model: "claude-haiku-4-5-20251001",
               max_tokens: 1024,
               messages: [%{role: "user", content: "Hello!"}]
             })
  end

  test "telemetry event [:elixir_anthropic, :request, :stop] fires on successful request",
       %{bypass: bypass} do
    Bypass.expect_once(bypass, "POST", "/v1/messages", fn conn ->
      Plug.Conn.resp(
        conn,
        200,
        Jason.encode!(%{
          "content" => [%{"type" => "text", "text" => "Hi"}],
          "model" => "claude-haiku-4-5-20251001"
        })
      )
    end)

    test_pid = self()
    handler_id = "test-telemetry-#{System.unique_integer()}"

    :telemetry.attach(
      handler_id,
      [:elixir_anthropic, :request, :stop],
      fn event, measurements, metadata, _config ->
        send(test_pid, {:telemetry_event, event, measurements, metadata})
      end,
      nil
    )

    on_exit(fn -> :telemetry.detach(handler_id) end)

    client =
      ElixirAnthropic.new(
        api_key: "test-key",
        base_url: "http://localhost:#{bypass.port}"
      )

    ElixirAnthropic.messages(client, %{
      model: "claude-haiku-4-5-20251001",
      max_tokens: 1024,
      messages: [%{role: "user", content: "Hello!"}]
    })

    assert_receive {:telemetry_event, [:elixir_anthropic, :request, :stop], measurements,
                    metadata},
                   1000

    assert is_integer(measurements[:duration])
    assert metadata[:model] == "claude-haiku-4-5-20251001"
    assert metadata[:status] == 200
  end
end
