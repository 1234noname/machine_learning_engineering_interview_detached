defmodule AVSA.ConversationResumeTest do
  use ExUnit.Case, async: false

  setup do
    Application.put_env(:avsa, :retrieval_tool_module, AVSA.StubRetrievalTool)
    Application.put_env(:avsa, :text_tool_module, AVSA.StubTextTool)

    on_exit(fn ->
      Application.delete_env(:avsa, :retrieval_tool_module)
      Application.delete_env(:avsa, :text_tool_module)
    end)

    :ok
  end

  test "start_conversation returns {:ok, pid} for an already-registered conversation" do
    conversation_id = Ecto.UUID.generate()

    {:ok, pid1} = AVSA.ConversationSupervisor.start_conversation(conversation_id)
    {:ok, pid2} = AVSA.ConversationSupervisor.start_conversation(conversation_id)

    assert pid1 == pid2, "Resume must return the same pid, not start a new process"
    assert Process.alive?(pid1)
  end

  test "start_conversation with different ids starts distinct processes" do
    id1 = Ecto.UUID.generate()
    id2 = Ecto.UUID.generate()

    {:ok, pid1} = AVSA.ConversationSupervisor.start_conversation(id1)
    {:ok, pid2} = AVSA.ConversationSupervisor.start_conversation(id2)

    refute pid1 == pid2
  end

  test "run_conversation_turn resumes existing conversation on second call" do
    conversation_id = Ecto.UUID.generate()

    request = %Avsa.Orchestrator.V1.StartConversationRequest{
      conversation_id: conversation_id,
      image_bytes: "",
      user_text: "first query"
    }

    {result1, _elapsed} = AVSA.GrpcServer.run_conversation_turn(request)
    assert {:ok, _} = result1

    request2 = %Avsa.Orchestrator.V1.StartConversationRequest{
      conversation_id: conversation_id,
      image_bytes: "",
      user_text: "second query"
    }

    {result2, _elapsed} = AVSA.GrpcServer.run_conversation_turn(request2)
    assert {:ok, response2} = result2
    assert Map.has_key?(response2, :results)
  end
end
