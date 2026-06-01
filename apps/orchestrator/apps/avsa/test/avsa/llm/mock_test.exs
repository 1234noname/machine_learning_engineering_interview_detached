defmodule AVSA.LLM.MockTest do
  use ExUnit.Case, async: false

  setup do
    on_exit(fn -> Agent.update(AVSA.LLM.Mock, fn _ -> nil end) end)
    :ok
  end

  # The default-response *shape* (typed %ToolUse{}, not a string-keyed map) is
  # pinned by conformance_test.exs alongside the Anthropic impl — the canonical
  # drift guard. Here we only cover the double's own contract used by other
  # tests: set_response/1 round-trips both ok and error tuples.

  test "call/2 returns custom response after set_response/1" do
    custom = %AVSA.LLM.ToolUse{name: "extract_attributes", id: "custom-1", input: %{"x" => true}}
    AVSA.LLM.Mock.set_response({:ok, custom})
    assert {:ok, ^custom} = AVSA.LLM.Mock.call([], %{})
  end

  test "call/2 returns error after set_response with error tuple" do
    AVSA.LLM.Mock.set_response({:error, :mock_error})
    assert {:error, :mock_error} = AVSA.LLM.Mock.call([], %{})
  end
end
