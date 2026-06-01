defmodule AVSA.LLM.Anthropic do
  @moduledoc """
  Real LLM implementation backed by the Anthropic Messages API.

  Reads AVSA_ANTHROPIC_API_KEY from the environment at call time.
  Wrapped in a :fuse circuit breaker (:anthropic_circuit) that opens after
  3 failures in 10 seconds and resets after 60 seconds.

  ## tool_manifest convention

  Callers pass the tool that the LLM must call:

  - `%{}` (empty map) — planning-step convention used by `AVSA.Conversation`.
    The module substitutes `@find_similar_tool` and forces `tool_choice: {"type": "any"}`
    so the LLM always produces a tool_use block. This avoids the Anthropic 400
    "tools.0.custom.name: Field required" error that `tools: [%{}]` triggers.

  - A real tool map carrying a `"name"` key (e.g. `extract_attributes`) — sent
    as-is with `tool_choice: {"type": "tool", "name": <tool_name>}` so the LLM
    is forced to call that specific tool. All current AVSA callers require a
    tool_use block; permitting a text/end_turn response causes
    `{:error, :no_tool_use}` which propagates to an empty gRPC stream.
    No caller legitimately wants a free-text response from this module.

  ## Response shape

  The Anthropic API returns a full message envelope:

      %{"content" => [%{"type" => "tool_use", ...}], "usage" => ...}

  `call/2` unwraps the first tool_use content block and returns a typed
  `%AVSA.LLM.ToolUse{}` struct so callers receive the same shape as
  `AVSA.LLM.Mock` — both implement the `AVSA.LLM` callback which returns
  `{:ok, AVSA.LLM.ToolUse.t()}`.
  If no tool_use block is found (text-only stop), `{:error, :no_tool_use}` is
  returned rather than leaking the raw envelope (which no consumer can handle).
  """

  @behaviour AVSA.LLM

  require Logger

  @model "claude-haiku-4-5-20251001"

  @find_similar_tool %{
    "name" => "find_similar",
    "description" =>
      "Search for visually similar fashion products. Call this tool with the " <>
        "user's attributes to find matching catalog items.",
    "input_schema" => %{
      "type" => "object",
      "properties" => %{
        "attrs" => %{
          "type" => "object",
          "description" => "Structured search attributes",
          "properties" => %{
            "category" => %{"type" => "string"},
            "colour" => %{"type" => "string"},
            "formality" => %{"type" => "string"},
            "occasion" => %{"type" => "string"}
          },
          "required" => ["category", "colour", "formality", "occasion"]
        }
      },
      "required" => ["attrs"]
    }
  }

  @impl AVSA.LLM
  @spec call([map()], map()) :: {:ok, AVSA.LLM.ToolUse.t()} | {:error, term()}
  def call(messages, tool_manifest) do
    case :fuse.ask(:anthropic_circuit, :sync) do
      :ok -> do_call(messages, tool_manifest)
      :blown -> {:error, :circuit_open}
    end
  end

  defp do_call(messages, tool_manifest) do
    api_key = System.fetch_env!("AVSA_ANTHROPIC_API_KEY")
    base_url = Application.get_env(:avsa, :anthropic_base_url, "https://api.anthropic.com")

    client = ElixirAnthropic.new(api_key: api_key, base_url: base_url)

    {effective_tool, extra_params} =
      case tool_manifest do
        empty when empty == %{} ->
          {@find_similar_tool, %{tool_choice: %{"type" => "any"}}}

        %{"name" => tool_name} = tm ->
          {tm, %{tool_choice: %{"type" => "tool", "name" => tool_name}}}
      end

    params =
      Map.merge(
        %{
          model: @model,
          max_tokens: 1024,
          tools: [effective_tool],
          messages: messages
        },
        extra_params
      )

    result =
      try do
        ElixirAnthropic.messages(client, params)
      rescue
        e -> {:error, e}
      end

    if match?({:error, _}, result) do
      :fuse.melt(:anthropic_circuit)
      :telemetry.execute([:avsa, :circuit, :melt], %{count: 1}, %{breaker: "anthropic_circuit"})
    end

    case result do
      {:ok, response} ->
        case extract_first_tool_use(response) do
          {:ok, tool_use_block} -> {:ok, tool_use_block}
          {:error, :no_tool_use} = err -> err
        end

      {:error, _} = error ->
        error
    end
  end

  # Extract the first tool_use content block from the full Anthropic response
  # and wrap it as a typed `%AVSA.LLM.ToolUse{}` struct.
  #
  # Returns `{:ok, %AVSA.LLM.ToolUse{}}` when a tool_use block is found.
  # Returns `{:error, :no_tool_use}` when no tool_use block is present (text stop).
  @spec extract_first_tool_use(map()) :: {:ok, AVSA.LLM.ToolUse.t()} | {:error, :no_tool_use}
  defp extract_first_tool_use(%{"content" => content}) when is_list(content) do
    case Enum.find(content, fn item -> Map.get(item, "type") == "tool_use" end) do
      nil ->
        {:error, :no_tool_use}

      %{"name" => name, "id" => id, "input" => input} ->
        {:ok, %AVSA.LLM.ToolUse{name: name, id: id, input: input}}

      %{"name" => name, "input" => input} ->
        {:ok, %AVSA.LLM.ToolUse{name: name, id: "unknown", input: input}}
    end
  end

  defp extract_first_tool_use(_response), do: {:error, :no_tool_use}

end
