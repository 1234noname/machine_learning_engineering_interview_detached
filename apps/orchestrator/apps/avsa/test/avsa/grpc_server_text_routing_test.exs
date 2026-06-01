defmodule AVSA.GrpcServerTextRoutingTest do
  @moduledoc """
  Tests for modality-based routing in AVSA.GrpcServer.

  When image_bytes is empty the server must:
    - call AVSA.TextTool (not EmbedStep) to get a text embedding
    - call RetrievalTool.call_text/2 (not call/2) for vector search
    - emit [:avsa, :conversation, :complete] telemetry tagged for the
      text-only modality

  When image_bytes is non-empty the existing image path must still work.
  """

  use ExUnit.Case, async: false

  setup_all do
    # Start tracking agents once for all tests in this module (named, persist across tests).
    unless Process.whereis(AVSA.TrackingTextTool),
      do: Agent.start(fn -> false end, name: AVSA.TrackingTextTool)

    unless Process.whereis(AVSA.TrackingEmbedStep),
      do: Agent.start(fn -> false end, name: AVSA.TrackingEmbedStep)

    :ok
  end

  setup do
    Application.put_env(:avsa, :embed_step_module, AVSA.StubEmbedStep)
    Application.put_env(:avsa, :text_tool_module, AVSA.StubTextTool)
    Application.put_env(:avsa, :retrieval_tool_module, AVSA.StubRetrievalTool)

    on_exit(fn ->
      Application.delete_env(:avsa, :embed_step_module)
      Application.delete_env(:avsa, :text_tool_module)
      Application.delete_env(:avsa, :retrieval_tool_module)
    end)

    :ok
  end

  defp image_request(opts \\ []) do
    %Avsa.Orchestrator.V1.StartConversationRequest{
      conversation_id: Keyword.get(opts, :conversation_id, Ecto.UUID.generate()),
      image_bytes: Keyword.get(opts, :image_bytes, <<1, 2, 3>>),
      user_text: Keyword.get(opts, :user_text, "what is this?")
    }
  end

  defp text_request(opts \\ []) do
    %Avsa.Orchestrator.V1.StartConversationRequest{
      conversation_id: Keyword.get(opts, :conversation_id, Ecto.UUID.generate()),
      image_bytes: "",
      user_text: Keyword.get(opts, :user_text, "summer floral dress")
    }
  end

  test "text-only request (empty image_bytes) routes via TextTool, not EmbedStep" do
    # Use a tracking stub that records which module was called
    Application.put_env(:avsa, :text_tool_module, AVSA.TrackingTextTool)
    Application.put_env(:avsa, :embed_step_module, AVSA.TrackingEmbedStep)

    AVSA.TrackingTextTool.reset()
    AVSA.TrackingEmbedStep.reset()

    {result, _elapsed} = AVSA.GrpcServer.run_conversation_turn(text_request())

    assert {:ok, _response} = result
    assert AVSA.TrackingTextTool.called?(), "TextTool should have been called for text-only request"

    refute AVSA.TrackingEmbedStep.called?(),
           "EmbedStep should NOT be called for text-only request"
  end

  test "image request routes via EmbedStep, not TextTool" do
    Application.put_env(:avsa, :text_tool_module, AVSA.TrackingTextTool)
    Application.put_env(:avsa, :embed_step_module, AVSA.TrackingEmbedStep)

    AVSA.TrackingTextTool.reset()
    AVSA.TrackingEmbedStep.reset()

    {result, _elapsed} = AVSA.GrpcServer.run_conversation_turn(image_request())

    assert {:ok, _response} = result
    assert AVSA.TrackingEmbedStep.called?(), "EmbedStep should have been called for image request"

    refute AVSA.TrackingTextTool.called?(),
           "TextTool should NOT be called for image-only request"
  end

  test "text-only turn emits modality=text telemetry tag" do
    test_pid = self()
    handler_id = "grpc-text-routing-text-#{:erlang.unique_integer()}"

    :telemetry.attach(
      handler_id,
      [:avsa, :conversation, :complete],
      fn _event, measurements, metadata, _config ->
        send(test_pid, {:telemetry, measurements, metadata})
      end,
      nil
    )

    on_exit(fn -> :telemetry.detach(handler_id) end)

    AVSA.GrpcServer.run_conversation_turn(text_request())

    assert_receive {:telemetry, _measurements, metadata}
    assert metadata.modality == "text",
           "Expected modality=text for text-only turn, got: #{inspect(metadata.modality)}"
  end
end
