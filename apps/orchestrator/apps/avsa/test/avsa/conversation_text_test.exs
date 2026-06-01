defmodule AVSA.ConversationTextTest do
  @moduledoc """
  Tests for the text-only conversation path in AVSA.Conversation.

  Verifies that `{:start_text, user_text}` drives the image-native MCP tools'
  TEXT modality: extract_attributes (LLM, no image) then find_similar's text
  encoder + `RetrievalTool.call_text/2`. There is no pre-computed embedding any
  more — the text query is embedded INSIDE find_similar. The encoder + retrieval
  are stubbed (no model server / DB) via the module-config seams the tools read.
  """

  use ExUnit.Case, async: false

  setup do
    Application.put_env(:avsa, :text_tool_module, AVSA.StubTextTool)
    Application.put_env(:avsa, :retrieval_tool_module, AVSA.StubRetrievalTool)

    on_exit(fn ->
      Application.delete_env(:avsa, :text_tool_module)
      Application.delete_env(:avsa, :retrieval_tool_module)
      Agent.update(AVSA.LLM.Mock, fn _ -> nil end)
    end)

    pid =
      start_supervised!(
        {AVSA.Conversation,
         [
           conversation_id: "text-test-#{:erlang.unique_integer()}",
           llm_module: AVSA.LLM.Mock
         ]}
      )

    {:ok, pid: pid}
  end

  test "start_text call returns {:ok, result} with results from find_similar text modality", %{
    pid: pid
  } do
    assert {:ok, result} = GenServer.call(pid, {:start_text, "summer floral dress"})
    assert is_map(result)
    assert Map.has_key?(result, :plan)
    assert Map.has_key?(result, :attrs)
    assert Map.has_key?(result, :results)
    # The text modality returns real catalog-shaped structs (via the stub).
    assert %AVSA.ProductResult{} = hd(result.results)
  end

  test "start_text accumulates turns like the image path", %{pid: pid} do
    assert {:ok, _} = GenServer.call(pid, {:start_text, "summer dress"})
    assert length(:sys.get_state(pid).turns) == 1

    assert {:ok, _} = GenServer.call(pid, {:start_text, "floral skirt"})
    assert length(:sys.get_state(pid).turns) == 2
  end

  test "start_text returns {:error, reason} when LLM mock returns error", %{pid: pid} do
    AVSA.LLM.Mock.set_response({:error, :timeout})

    assert {:error, :timeout} =
             GenServer.call(pid, {:start_text, "summer dress"})
  end
end
