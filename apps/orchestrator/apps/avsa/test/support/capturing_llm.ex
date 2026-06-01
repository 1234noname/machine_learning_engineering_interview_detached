defmodule AVSA.LLM.Capturing do
  @moduledoc false
  # A test double for the AVSA.LLM behaviour that BOTH returns a canned response
  # AND records the `(messages, tool_manifest)` it was last called with, so a test
  # can assert *what the LLM was asked to do* — specifically, that the
  # extract_attributes tool schema the LLM receives is scoped to the text-derived
  # attributes (formality/occasion) only, not category/colour.
  #
  # State is `%{response: term(), calls: [ {messages, tool_manifest} ]}` held in a
  # named Agent. Tests `set_response/1`, run the code under test, then `calls/0` to
  # inspect every invocation (most-recent first). `reset/0` clears both.
  #
  # Returns `{:ok, %AVSA.LLM.ToolUse{}}` by default, conforming to the typed
  # `AVSA.LLM` callback contract.
  use Agent

  @behaviour AVSA.LLM

  @default_response %AVSA.LLM.ToolUse{
    name: "extract_attributes",
    id: "capturing-1",
    input: %{
      "category" => "from-llm",
      "colour" => "from-llm",
      "formality" => "casual",
      "occasion" => "everyday"
    }
  }

  def start_link(_opts \\ []) do
    Agent.start_link(fn -> %{response: nil, calls: []} end, name: __MODULE__)
  end

  @doc "Override the response returned by the next call/2 (and subsequent calls)."
  @spec set_response({:ok, AVSA.LLM.ToolUse.t()} | {:error, term()}) :: :ok
  def set_response(response) do
    Agent.update(__MODULE__, fn state -> %{state | response: response} end)
  end

  @doc "Return the list of `{messages, tool_manifest}` tuples captured, most-recent first."
  def calls do
    Agent.get(__MODULE__, fn state -> state.calls end)
  end

  @doc "Clear captured calls and the canned response."
  def reset do
    Agent.update(__MODULE__, fn _ -> %{response: nil, calls: []} end)
  end

  @impl AVSA.LLM
  @spec call([map()], map()) :: {:ok, AVSA.LLM.ToolUse.t()} | {:error, term()}
  def call(messages, tool_manifest) do
    Agent.get_and_update(__MODULE__, fn state ->
      response = state.response || {:ok, @default_response}
      {response, %{state | calls: [{messages, tool_manifest} | state.calls]}}
    end)
  end
end
