defmodule AVSA.ConversationTurnHistoryTest do
  use ExUnit.Case, async: false

  setup_all do
    unless Process.whereis(AVSA.AttrCapturingRetrievalTool),
      do: Agent.start(fn -> [] end, name: AVSA.AttrCapturingRetrievalTool)

    :ok
  end

  setup do
    Application.put_env(:avsa, :retrieval_tool_module, AVSA.StubRetrievalTool)
    Application.put_env(:avsa, :text_tool_module, AVSA.StubTextTool)

    on_exit(fn ->
      Application.delete_env(:avsa, :retrieval_tool_module)
      Application.delete_env(:avsa, :text_tool_module)
    end)

    :ok
  end

  defp start_conversation(opts \\ []) do
    conversation_id = Keyword.get(opts, :conversation_id, Ecto.UUID.generate())
    max_turns = Keyword.get(opts, :max_context_turns, nil)

    if max_turns != nil do
      Application.put_env(:avsa, :max_context_turns, max_turns)
    end

    {:ok, pid} = AVSA.ConversationSupervisor.start_conversation(conversation_id)

    if max_turns != nil do
      on_exit(fn -> Application.delete_env(:avsa, :max_context_turns) end)
    end

    pid
  end

  defp do_text_turn(pid, text) do
    # No pre-computed embedding — find_similar's text modality embeds the
    # query internally (stubbed via AVSA.StubTextTool).
    GenServer.call(pid, {:start_text, text})
  end

  test "turn history is bounded by max_context_turns" do
    Application.put_env(:avsa, :max_context_turns, 2)

    on_exit(fn -> Application.delete_env(:avsa, :max_context_turns) end)

    pid = start_conversation()

    do_text_turn(pid, "first query")
    do_text_turn(pid, "second query")
    do_text_turn(pid, "third query")

    state = :sys.get_state(pid)
    assert length(state.turns) == 2, "Expected 2 turns (max_context_turns=2), got #{length(state.turns)}"
  end

  test "second turn receives prior_result_ids in attrs" do
    Application.put_env(:avsa, :retrieval_tool_module, AVSA.AttrCapturingRetrievalTool)
    Agent.update(AVSA.AttrCapturingRetrievalTool, fn _ -> [] end)

    pid = start_conversation()

    do_text_turn(pid, "first query")
    do_text_turn(pid, "second query")

    captured = Agent.get(AVSA.AttrCapturingRetrievalTool, & &1)
    assert length(captured) >= 2, "Expected at least 2 retrieval calls"

    second_attrs = Enum.at(captured, 1)
    prior_ids = Map.get(second_attrs, "prior_result_ids", [])
    assert prior_ids != [], "Second turn should pass prior_result_ids in attrs"
  end

  test "first turn has no prior_result_ids in attrs" do
    Application.put_env(:avsa, :retrieval_tool_module, AVSA.AttrCapturingRetrievalTool)
    Agent.update(AVSA.AttrCapturingRetrievalTool, fn _ -> [] end)

    pid = start_conversation()

    do_text_turn(pid, "first query")

    captured = Agent.get(AVSA.AttrCapturingRetrievalTool, & &1)
    assert length(captured) >= 1

    first_attrs = List.first(captured)
    prior_ids = Map.get(first_attrs, "prior_result_ids", [])
    assert prior_ids == [], "First turn must not pass prior_result_ids (none accumulated yet)"
  end

  test "max_context_turns of 0 means no history kept" do
    Application.put_env(:avsa, :max_context_turns, 0)
    on_exit(fn -> Application.delete_env(:avsa, :max_context_turns) end)

    pid = start_conversation()

    do_text_turn(pid, "query one")
    do_text_turn(pid, "query two")

    state = :sys.get_state(pid)
    assert state.turns == [], "max_context_turns=0 should keep no history"
  end
end
