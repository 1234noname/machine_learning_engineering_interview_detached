defmodule ElixirAnthropic.ErrorTest do
  use ExUnit.Case, async: true

  setup do
    bypass = Bypass.open()
    {:ok, bypass: bypass}
  end

  test "messages/2 returns {:error, %Error{type: :api_error, status: 429}} on 429 response",
       %{bypass: bypass} do
    Bypass.expect_once(bypass, "POST", "/v1/messages", fn conn ->
      Plug.Conn.resp(
        conn,
        429,
        Jason.encode!(%{
          "type" => "error",
          "error" => %{"type" => "rate_limit_error", "message" => "Rate limit exceeded"}
        })
      )
    end)

    client =
      ElixirAnthropic.new(
        api_key: "test-key",
        base_url: "http://localhost:#{bypass.port}"
      )

    assert {:error, %ElixirAnthropic.Error{type: :api_error, status: 429}} =
             ElixirAnthropic.messages(client, %{
               model: "claude-haiku-4-5-20251001",
               max_tokens: 1024,
               messages: [%{role: "user", content: "Hello!"}]
             })
  end

  test "messages/2 returns {:error, %Error{type: :invalid_params}} when :model key is missing" do
    client = ElixirAnthropic.new(api_key: "test-key")

    assert {:error, %ElixirAnthropic.Error{type: :invalid_params}} =
             ElixirAnthropic.messages(client, %{
               messages: [%{role: "user", content: "Hello!"}]
             })
  end
end
