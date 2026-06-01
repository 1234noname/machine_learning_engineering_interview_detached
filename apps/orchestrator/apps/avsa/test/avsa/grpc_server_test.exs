defmodule AVSA.GrpcServerTest do
  use ExUnit.Case, async: false

  # The full supervision tree (ConversationSupervisor, LLM.Mock, etc.) is started
  # by AVSA.Application in the test env. We swap only the embed step module here.

  setup do
    Application.put_env(:avsa, :embed_step_module, AVSA.StubEmbedStep)
    on_exit(fn -> Application.delete_env(:avsa, :embed_step_module) end)
    :ok
  end

  defp request(opts \\ []) do
    %Avsa.Orchestrator.V1.StartConversationRequest{
      conversation_id: Keyword.get(opts, :conversation_id, Ecto.UUID.generate()),
      image_bytes: Keyword.get(opts, :image_bytes, <<1, 2, 3>>),
      user_text: Keyword.get(opts, :user_text, "what is this?")
    }
  end

  test "run_conversation_turn returns {:ok, response} with results map on happy path" do
    {result, elapsed} = AVSA.GrpcServer.run_conversation_turn(request())

    assert is_integer(elapsed) and elapsed >= 0
    assert {:ok, response} = result
    assert Map.has_key?(response, :results)
  end

  test "run_conversation_turn generates a conversation_id when request has empty string" do
    {result, _elapsed} = AVSA.GrpcServer.run_conversation_turn(request(conversation_id: ""))

    assert {:ok, _response} = result
  end

  test "run_conversation_turn returns {:error, :embed_failed} when embed step fails" do
    Application.put_env(:avsa, :embed_step_module, AVSA.FailingEmbedStep)

    {{:error, :embed_failed}, elapsed} =
      AVSA.GrpcServer.run_conversation_turn(request(image_bytes: <<1, 2, 3>>))

    assert is_integer(elapsed) and elapsed >= 0
  end

  test "run_conversation_turn emits [:avsa, :conversation, :complete] with outcome=success" do
    test_pid = self()
    handler_id = "grpc-server-test-success-#{:erlang.unique_integer()}"

    :telemetry.attach(
      handler_id,
      [:avsa, :conversation, :complete],
      fn _event, measurements, metadata, _config ->
        send(test_pid, {:telemetry, measurements, metadata})
      end,
      nil
    )

    on_exit(fn -> :telemetry.detach(handler_id) end)

    AVSA.GrpcServer.run_conversation_turn(request())

    assert_receive {:telemetry, measurements, metadata}
    assert is_integer(measurements.latency_ms) and measurements.latency_ms >= 0
    assert metadata.outcome == "success"
    assert metadata.modality == "image"
  end

  test "run_conversation_turn emits [:avsa, :conversation, :complete] with outcome=error on failure" do
    Application.put_env(:avsa, :embed_step_module, AVSA.FailingEmbedStep)
    test_pid = self()
    handler_id = "grpc-server-test-error-#{:erlang.unique_integer()}"

    :telemetry.attach(
      handler_id,
      [:avsa, :conversation, :complete],
      fn _event, measurements, metadata, _config ->
        send(test_pid, {:telemetry, measurements, metadata})
      end,
      nil
    )

    on_exit(fn -> :telemetry.detach(handler_id) end)

    AVSA.GrpcServer.run_conversation_turn(request(image_bytes: <<1, 2, 3>>))

    assert_receive {:telemetry, _measurements, metadata}
    assert metadata.outcome == "error"
    assert metadata.modality == "image"
  end

  # ---------------------------------------------------------------------------
  # Graceful :no_tool_use regression guard
  #
  # The {:error, :no_tool_use} branch in events_for_turn returns [] instead of
  # raising. This test wires an LLM double that returns {:error, :no_tool_use}
  # into the planning step and asserts:
  #   (a) events_for_turn does NOT raise, and
  #   (b) it returns [] — a defined graceful outcome, not an empty-stream crash.
  # ---------------------------------------------------------------------------

  # ---------------------------------------------------------------------------
  # image_url in metadata_json
  #
  # Ensures build_product_event encodes image_url into the metadata_json blob
  # so the Python API can sign it at read time via _sign_card_image_url.
  # The StubRetrievalTool returns deterministic ProductResult rows with
  # image_url set, so this test exercises the full grpc_server path without DB.
  # ---------------------------------------------------------------------------

  test "product_result events carry image_url in metadata_json" do
    Application.put_env(:avsa, :retrieval_tool_module, AVSA.StubRetrievalTool)

    on_exit(fn -> Application.delete_env(:avsa, :retrieval_tool_module) end)

    events = AVSA.GrpcServer.events_for_turn(request())

    product_events =
      Enum.filter(events, fn ev ->
        match?(%{payload: {:product_result, _}}, ev)
      end)

    assert length(product_events) > 0, "Expected at least one product_result event"

    Enum.each(product_events, fn %{payload: {:product_result, pr_event}} ->
      meta = Jason.decode!(pr_event.metadata_json)
      assert Map.has_key?(meta, "image_url"),
             "metadata_json must contain 'image_url' key; got: #{pr_event.metadata_json}"
    end)

    # Spot-check the first result matches the stub value exactly.
    [%{payload: {:product_result, first_event}} | _] = product_events
    first_meta = Jason.decode!(first_event.metadata_json)
    assert first_meta["image_url"] == "/images/sundress-001"
  end

  test "events_for_turn: {:error, :no_tool_use} from LLM returns [] without raising" do
    # Wire the capturing double to return {:error, :no_tool_use} so the planning
    # step propagates this error up to events_for_turn.
    start_supervised!(AVSA.LLM.Capturing)
    AVSA.LLM.Capturing.reset()
    AVSA.LLM.Capturing.set_response({:error, :no_tool_use})

    original_llm_module = Application.get_env(:avsa, :llm_module)

    on_exit(fn ->
      Application.put_env(:avsa, :llm_module, original_llm_module)
    end)

    Application.put_env(:avsa, :llm_module, AVSA.LLM.Capturing)

    # Must NOT raise.
    result =
      try do
        AVSA.GrpcServer.events_for_turn(request())
      rescue
        e -> {:raised, e}
      end

    # (a) No exception raised
    refute match?({:raised, _}, result),
           "events_for_turn must not raise on {:error, :no_tool_use}, got: #{inspect(result)}"

    # (b) Returns [] — a defined graceful outcome
    assert result == [],
           "events_for_turn must return [] on {:error, :no_tool_use}, got: #{inspect(result)}"
  end
end
