defmodule AVSA.ConversationTest do
  use ExUnit.Case, async: false

  # The image-driven turn drives the image-native MCP tools.
  # `{:start_image, image_arg, user_text}` resolves the image and embeds it
  # INSIDE find_similar / extract_attributes (shared per-turn cache). We stub the
  # embed step + retrieval (no batcher / DB) via the module-config seams the
  # tools read, and pass the image as an inline image_b64 argument.

  @image_arg %{"image_b64" => Base.encode64(<<1, 2, 3>>)}

  setup do
    Application.put_env(:avsa, :embed_step_module, AVSA.StubEmbedStep)
    Application.put_env(:avsa, :retrieval_tool_module, AVSA.StubRetrievalTool)

    on_exit(fn ->
      Application.delete_env(:avsa, :embed_step_module)
      Application.delete_env(:avsa, :retrieval_tool_module)
      Agent.update(AVSA.LLM.Mock, fn _ -> nil end)
    end)

    pid =
      start_supervised!(
        {AVSA.Conversation,
         [
           conversation_id: "test-#{:erlang.unique_integer()}",
           llm_module: AVSA.LLM.Mock
         ]}
      )

    {:ok, pid: pid}
  end

  test "start_image returns {:ok, result} where result is a map with plan, attrs, results", %{
    pid: pid
  } do
    assert {:ok, result} = GenServer.call(pid, {:start_image, @image_arg, "red dress"})
    assert is_map(result)
    assert Map.has_key?(result, :plan)
    assert Map.has_key?(result, :attrs)
    assert Map.has_key?(result, :results)
  end

  test "start_image returns {:error, reason} when mock returns error", %{pid: pid} do
    AVSA.LLM.Mock.set_response({:error, :timeout})
    assert {:error, :timeout} = GenServer.call(pid, {:start_image, @image_arg, "red dress"})
  end

  test "plan is stored in state after successful start", %{pid: pid} do
    {:ok, _result} = GenServer.call(pid, {:start_image, @image_arg, "red dress"})
    assert :sys.get_state(pid).plan != nil
  end

  test "turns accumulate across multiple start calls", %{pid: pid} do
    assert {:ok, _} = GenServer.call(pid, {:start_image, @image_arg, "red dress"})
    assert length(:sys.get_state(pid).turns) == 1

    assert {:ok, _} = GenServer.call(pid, {:start_image, @image_arg, "blue dress"})
    assert length(:sys.get_state(pid).turns) == 2
  end

  test "plan is a typed %AVSA.LLM.ToolUse{} struct in the proposed response", %{pid: pid} do
    # The plan returned by the LLM is a typed struct, not a string-keyed map.
    # Proposed.text is always "" (the ToolUse struct carries no free-text field).
    assert {:ok, result} = GenServer.call(pid, {:start_image, @image_arg, "red dress"})
    assert %AVSA.LLM.ToolUse{} = result.plan
    assert result.plan.name == "find_similar"
    assert result.text == ""
  end

  test "returns {:error, :safety, _} immediately without re-planning when safety check fails", %{
    pid: pid
  } do
    # "how to make a bomb" matches the safety probe pattern; proposed response
    # won't have escalate: true so safety check must fail immediately.
    assert {:error, :safety, _reason} =
             GenServer.call(pid, {:start_image, @image_arg, "how to make a bomb"})
  end
end

defmodule AVSA.ConversationVitOffloadTest do
  use ExUnit.Case, async: false

  # Regression guard — the StartConversation/Conversation path is routed
  # through the image-native MCP tools.
  #
  # The image-driven discovery turn must USE the ViT attribute head: category/
  # colour are sourced from the head (surfaced by the embed step's `attributes`
  # field) and the LLM extract_attributes tool is narrowed (no category/colour
  # requested). In the MCP-native flow the ViT attributes flow from the embed
  # step INSIDE extract_attributes (through the per-turn cache), not as a
  # threaded GenServer argument — so we stub the embed step to return them.

  setup do
    on_exit(fn -> Agent.update(AVSA.LLM.Mock, fn _ -> nil end) end)

    # The embed step (reached inside extract_attributes / find_similar) returns a
    # fixed embedding PLUS the ViT attribute head output.
    Application.put_env(:avsa, :embed_step_module, AVSA.VitAttrEmbedStep)
    Application.put_env(:avsa, :retrieval_tool_module, AVSA.StubRetrievalTool)

    # The Conversation drives AVSA.AttributeTool (the supervised singleton). To
    # assert WHAT the LLM was asked through that path, swap the singleton to the
    # capturing double for the test, then restore the supervised default on exit.
    start_supervised!(AVSA.LLM.Capturing)
    AVSA.LLM.Capturing.reset()

    Supervisor.terminate_child(AVSA.Supervisor, AVSA.AttributeTool)
    {:ok, _attr_pid} = AVSA.AttributeTool.start_link(llm_module: AVSA.LLM.Capturing)

    on_exit(fn ->
      Application.delete_env(:avsa, :embed_step_module)
      Application.delete_env(:avsa, :retrieval_tool_module)

      case Process.whereis(AVSA.AttributeTool) do
        nil -> :ok
        pid -> GenServer.stop(pid)
      end

      Supervisor.restart_child(AVSA.Supervisor, AVSA.AttributeTool)
    end)

    :ok
  end

  test "image-driven start uses ViT category/colour and narrows the LLM tool" do
    # The LLM (capturing double) is asked ONLY for the text-derived attrs and
    # returns just those — proving the offload: if the path did not source
    # category/colour from the ViT head, they would be required of the LLM.
    AVSA.LLM.Capturing.set_response(
      {:ok,
       %AVSA.LLM.ToolUse{
         name: "extract_attributes",
         id: "cap-1",
         input: %{"formality" => "formal", "occasion" => "wedding"}
       }}
    )

    pid =
      start_supervised!(
        {AVSA.Conversation,
         [
           conversation_id: "vit-offload-#{:erlang.unique_integer([:positive])}",
           llm_module: AVSA.LLM.Capturing
         ]}
      )

    image_arg = %{"image_b64" => Base.encode64(<<10, 20, 30>>)}

    assert {:ok, result} =
             GenServer.call(pid, {:start_image, image_arg, "a navy skirt for a wedding"})

    # category/colour SOURCED FROM the ViT head (VitAttrEmbedStep), not the LLM.
    assert result.attrs["category"] == "skirt"
    assert result.attrs["colour"] == "navy"
    # formality/occasion came from the (narrowed) LLM extraction.
    assert result.attrs["formality"] == "formal"
    assert result.attrs["occasion"] == "wedding"

    # The LLM was asked at least once, and the extract_attributes tool it
    # received was narrowed: category/colour are NOT requested output fields.
    extract_call =
      Enum.find(AVSA.LLM.Capturing.calls(), fn {_messages, tool_manifest} ->
        is_map(tool_manifest) and tool_manifest["name"] == "extract_attributes"
      end)

    assert {_messages, tool_manifest} = extract_call
    property_keys = Map.keys(get_in(tool_manifest, ["input_schema", "properties"]) || %{})
    refute "category" in property_keys
    refute "colour" in property_keys
  end

  test "image-driven start with nil ViT attrs falls back to LLM for all four attrs" do
    # Fallback safety: a stub batcher with no attribute head (nil attributes) must
    # keep the legacy behaviour — the LLM extracts all four attrs.
    Application.put_env(:avsa, :embed_step_module, AVSA.StubEmbedStep)

    AVSA.LLM.Capturing.set_response(
      {:ok,
       %AVSA.LLM.ToolUse{
         name: "extract_attributes",
         id: "cap-2",
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
           conversation_id: "vit-fallback-#{:erlang.unique_integer([:positive])}",
           llm_module: AVSA.LLM.Capturing
         ]}
      )

    image_arg = %{"image_b64" => Base.encode64(<<11, 22, 33>>)}

    assert {:ok, result} =
             GenServer.call(pid, {:start_image, image_arg, "a red dress"})

    assert Enum.sort(Map.keys(result.attrs |> Map.delete("prior_result_ids"))) ==
             ["category", "colour", "formality", "occasion"]

    assert result.attrs["category"] == "dress"
    assert result.attrs["colour"] == "red"

    # In the fallback, the extract_attributes tool the LLM received still requests
    # category/colour (full schema).
    extract_call =
      Enum.find(AVSA.LLM.Capturing.calls(), fn {_messages, tool_manifest} ->
        is_map(tool_manifest) and tool_manifest["name"] == "extract_attributes"
      end)

    assert {_messages, tool_manifest} = extract_call
    property_keys = Map.keys(get_in(tool_manifest, ["input_schema", "properties"]) || %{})
    assert "category" in property_keys
    assert "colour" in property_keys
  end
end

defmodule AVSA.ConversationPlanningMessageTest do
  @moduledoc """
  Regression guard for the planning-LLM message-bloat behaviour under the
  MCP-native flow.

  In the MCP-native flow the Conversation no longer receives a raw embedding at
  all — the image is threaded as an opaque image_b64 argument and embedded inside
  the tools — so the planning message structurally cannot contain a 768-dim
  vector. This module pins the positive contract that survives: the planning
  message DOES contain user_text + the find_similar instruction, and does NOT
  contain the base64 image bytes (no image payload leaks into the prompt).
  """

  use ExUnit.Case, async: false

  setup do
    Application.put_env(:avsa, :embed_step_module, AVSA.StubEmbedStep)
    Application.put_env(:avsa, :retrieval_tool_module, AVSA.StubRetrievalTool)

    on_exit(fn ->
      Application.delete_env(:avsa, :embed_step_module)
      Application.delete_env(:avsa, :retrieval_tool_module)
      Agent.update(AVSA.LLM.Mock, fn _ -> nil end)
    end)

    start_supervised!(AVSA.LLM.Capturing)
    AVSA.LLM.Capturing.reset()
    :ok
  end

  @tag :regression
  test "planning message for image turn contains user_text + find_similar and no image payload" do
    AVSA.LLM.Capturing.set_response(
      {:ok, %AVSA.LLM.ToolUse{name: "find_similar", id: "plan-cap-1", input: %{}}}
    )

    user_text = "a red evening gown for a gala"
    # A distinctive base64 image payload; if accidentally interpolated into the
    # planning prompt it would appear verbatim.
    image_b64 = Base.encode64(:crypto.strong_rand_bytes(64))
    image_arg = %{"image_b64" => image_b64}

    pid =
      start_supervised!(
        {AVSA.Conversation,
         [
           conversation_id: "plan-msg-test-#{:erlang.unique_integer([:positive])}",
           llm_module: AVSA.LLM.Capturing
         ]}
      )

    _result = GenServer.call(pid, {:start_image, image_arg, user_text})

    # The planning LLM call is the FIRST real call → last in most-recent-first list.
    all_calls = AVSA.LLM.Capturing.calls()
    assert all_calls != [], "Expected at least one LLM call to have been captured"

    {planning_messages, _tool_manifest} = List.last(all_calls)

    planning_content =
      planning_messages
      |> Enum.filter(fn m -> m["role"] == "user" end)
      |> Enum.map(fn m -> m["content"] end)
      |> Enum.join(" ")

    assert String.contains?(planning_content, user_text),
           "Planning message must contain user_text, got: #{inspect(planning_content)}"

    assert String.contains?(planning_content, "find_similar"),
           "Planning message must reference find_similar, got: #{inspect(planning_content)}"

    refute String.contains?(planning_content, image_b64),
           "Planning message must NOT contain the raw image payload"
  end
end

defmodule AVSA.ConversationSupervisorTest do
  use ExUnit.Case, async: false

  test "start_conversation/2 registers the process in AVSA.ConversationRegistry" do
    conv_id = "supervisor-test-#{:erlang.unique_integer([:positive])}"

    assert {:ok, pid} = AVSA.ConversationSupervisor.start_conversation(conv_id)
    assert is_pid(pid)

    # Process must be findable via the registry
    assert [{^pid, _}] = Registry.lookup(AVSA.ConversationRegistry, conv_id)
  end

  test "via_tuple/1 resolves to the registered process" do
    conv_id = "via-test-#{:erlang.unique_integer([:positive])}"
    {:ok, pid} = AVSA.ConversationSupervisor.start_conversation(conv_id)

    via = AVSA.ConversationSupervisor.via_tuple(conv_id)
    assert GenServer.whereis(via) == pid
  end
end
