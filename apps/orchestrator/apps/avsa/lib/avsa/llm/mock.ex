defmodule AVSA.LLM.Mock do
  @moduledoc """
  Agent-backed test double for the AVSA.LLM behaviour.

  Started as part of the AVSA.Application supervision tree.
  Tests can call `set_response/1` to override the next response,
  and `call/2` pops (or defaults) the stored response.

  Returns `{:ok, %AVSA.LLM.ToolUse{}}` by default, matching the typed contract
  enforced by the `AVSA.LLM` behaviour. Tests that set a custom response via
  `set_response/1` must also pass a proper tagged tuple
  (e.g. `{:ok, %AVSA.LLM.ToolUse{...}}` or `{:error, reason}`).
  """

  use Agent

  @behaviour AVSA.LLM

  @default_response %AVSA.LLM.ToolUse{
    name: "find_similar",
    id: "mock-1",
    input: %{
      "embedding" => [],
      "attrs" => %{
        "category" => "test",
        "colour" => "blue",
        "formality" => "casual",
        "occasion" => "everyday"
      }
    }
  }

  def start_link(_opts \\ []) do
    Agent.start_link(fn -> nil end, name: __MODULE__)
  end

  @doc "Override the next response returned by call/2."
  @spec set_response({:ok, AVSA.LLM.ToolUse.t()} | {:error, term()}) :: :ok
  def set_response(response) do
    Agent.update(__MODULE__, fn _state -> response end)
  end

  @impl AVSA.LLM
  @spec call([map()], map()) :: {:ok, AVSA.LLM.ToolUse.t()} | {:error, term()}
  def call(_messages, _tool_manifest) do
    Agent.get_and_update(__MODULE__, fn
      nil -> {{:ok, @default_response}, nil}
      response -> {response, nil}
    end)
  end
end
