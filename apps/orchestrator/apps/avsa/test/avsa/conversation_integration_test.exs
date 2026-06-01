defmodule AVSA.ConversationIntegrationTest do
  use ExUnit.Case, async: false

  @moduletag :integration

  # The chat path routes through the image-native MCP tools: the image reaches
  # the tools as an inline image_b64 argument, the embed happens INSIDE the
  # tools (through the per-turn AVSA.EmbedCache), and kNN retrieval runs
  # against the REAL seeded catalog via AVSA.RetrievalTool.
  #
  # The embed step (L1 ViT forward) is stubbed by a COUNTING stub so we can both
  # (a) run without a live batcher and (b) prove the one-forward-per-turn
  # invariant. Retrieval and the Verifier hit the real DB on the shared Sandbox
  # connection.

  setup do
    # Shared Sandbox transaction. The full dispatch loop runs DB queries through
    # TWO long-lived named app GenServers — AVSA.RetrievalTool (kNN retrieval)
    # and AVSA.Verifier (catalog_resolvability + factuality) — reached via the
    # dynamically supervised Conversation server (which itself never touches the
    # Repo). Shared mode routes all of them onto this test's connection. See
    # AVSA.RepoTestHelper.checkout_shared!/0.
    AVSA.RepoTestHelper.checkout_shared!()

    # Embed INSIDE the tools is served by a counting stub (no live batcher); kNN
    # + verifier stay real (DB). The stub returns a fixed 768-d vector that the
    # kNN orders the seeded catalog against.
    original_embed = Application.get_env(:avsa, :embed_step_module)
    Application.put_env(:avsa, :embed_step_module, AVSA.MCP.CountingEmbedStep)
    AVSA.MCP.CountingEmbedStep.start()

    on_exit(fn ->
      restore(:embed_step_module, original_embed)
      Agent.update(AVSA.LLM.Mock, fn _ -> nil end)
    end)

    :ok
  end

  defp restore(key, nil), do: Application.delete_env(:avsa, key)
  defp restore(key, val), do: Application.put_env(:avsa, key, val)

  defp image_arg, do: %{"image_b64" => Base.encode64(<<7, 7, 7>>)}

  test "image-native chat path: seeded catalog + find_similar returns >= 1 ProductResult" do
    AVSA.CatalogFixture.seed(100)

    # Mock returns extract_attributes for the AttributeTool step; the planning
    # step pops the default find_similar response first.
    AVSA.LLM.Mock.set_response(
      {:ok,
       %AVSA.LLM.ToolUse{
         name: "extract_attributes",
         id: "int-ea-1",
         input: %{
           "category" => "dress",
           "colour" => "red",
           "formality" => "casual",
           "occasion" => "everyday"
         }
       }}
    )

    pid =
      start_supervised!(
        {AVSA.Conversation,
         [
           conversation_id: "int-test-#{:erlang.unique_integer([:positive])}",
           llm_module: AVSA.LLM.Mock
         ]}
      )

    assert {:ok, result} = GenServer.call(pid, {:start_image, image_arg(), "red dress"})
    assert is_list(result.results)
    assert length(result.results) >= 1
    # Real DB-sourced structs (NOT display cards) flow to the Verifier + gRPC.
    assert %AVSA.ProductResult{} = hd(result.results)
  end

  test "one ViT forward per turn: a single chat turn embeds the image exactly once" do
    AVSA.CatalogFixture.seed(100)

    AVSA.LLM.Mock.set_response(
      {:ok,
       %AVSA.LLM.ToolUse{
         name: "extract_attributes",
         id: "int-once-1",
         input: %{
           "category" => "dress",
           "colour" => "red",
           "formality" => "casual",
           "occasion" => "everyday"
         }
       }}
    )

    # A single turn runs BOTH image-native tools on the same image:
    # extract_attributes (ViT attribute head) then find_similar (kNN). The
    # per-turn embed cache must collapse them to ONE ViT forward. We drive the
    # tools with the SAME request_id the GrpcServer would mint per turn.
    AVSA.MCP.CountingEmbedStep.start()
    request_id = "int-turn-#{:erlang.unique_integer([:positive])}"

    pid =
      start_supervised!(
        {AVSA.Conversation,
         [
           conversation_id: "int-once-#{:erlang.unique_integer([:positive])}",
           llm_module: AVSA.LLM.Mock
         ]}
      )

    assert {:ok, result} =
             GenServer.call(pid, {:start_image, image_arg(), "red dress", request_id})

    assert length(result.results) >= 1

    # The (expensive) ViT forward ran EXACTLY once for the turn that called both
    # extract_attributes and find_similar on the same image — the cache seam.
    assert AVSA.MCP.CountingEmbedStep.count() == 1

    AVSA.EmbedCache.purge_request(request_id)
  end

  @tag :integration
  test "long-conversation continuity: 10 sequential turns preserve conversation_id and return well-formed results" do
    # Seed products so retrieval returns results.
    AVSA.CatalogFixture.seed(50)

    conversation_id = "int-long-conv-#{:erlang.unique_integer([:positive])}"

    pid =
      start_supervised!(
        {AVSA.Conversation,
         [
           conversation_id: conversation_id,
           llm_module: AVSA.LLM.Mock
         ]}
      )

    results =
      Enum.map(1..10, fn turn_n ->
        # Reset mock before each turn so the attribute-extraction step gets a
        # valid extract_attributes response (planning pops the default first).
        AVSA.LLM.Mock.set_response(
          {:ok,
           %AVSA.LLM.ToolUse{
             name: "extract_attributes",
             id: "int-ea-turn-#{turn_n}",
             input: %{
               "category" => "dress",
               "colour" => "blue",
               "formality" => "casual",
               "occasion" => "everyday"
             }
           }}
        )

        result = GenServer.call(pid, {:start_image, image_arg(), "blue dress turn #{turn_n}"})

        # Every turn must succeed.
        assert {:ok, _} = result, "turn #{turn_n} must return {:ok, _}"
        result
      end)

    # Assert conversation_id is consistent: all 10 turns used the same GenServer.
    assert Process.alive?(pid)

    state = :sys.get_state(pid)
    assert state.conversation_id == conversation_id

    # Turn history is bounded to max_context_turns.
    assert length(state.turns) == state.max_context_turns

    # Assert the final turn result is well-formed.
    {:ok, final_result} = List.last(results)
    assert Map.has_key?(final_result, :plan)
    assert Map.has_key?(final_result, :attrs)
    assert Map.has_key?(final_result, :results)
    assert is_list(final_result.results)
    assert length(final_result.results) >= 1
  end
end
