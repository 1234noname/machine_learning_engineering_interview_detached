defmodule AVSA.AttributeToolTest do
  use ExUnit.Case, async: false

  # Offload image-derived attributes (category/colour) from the LLM to the ViT
  # attribute head.
  #
  # Pinned contract for the implementation:
  #   * AVSA.AttributeTool.call/3 — call(image_description, user_text, vit_attributes)
  #     where `vit_attributes` is a string-keyed map carrying at least "category" and
  #     "colour" (the ViT head output, surfaced by EmbedStep), or nil.
  #   * When vit_attributes carries category/colour, the output `attrs` takes
  #     category/colour from the ViT input and formality/occasion from the LLM.
  #     The LLM is asked ONLY for the text-derived attrs (the extract_attributes
  #     tool schema the LLM receives no longer requests category/colour).
  #   * Fallback (call/2, or call/3 with nil): the LLM extracts all four attrs —
  #     existing call/2 behaviour preserved for text turns / MCP / stub paths.
  #   * The output `attrs` map always has exactly: category, colour, formality, occasion.
  #
  # The "LLM only for text-derived attrs" assertion is made by configuring a
  # capturing LLM double (AVSA.LLM.Capturing) and inspecting the tool_manifest it
  # was called with. A test-owned AttributeTool instance is started with that
  # double so the singleton (which uses AVSA.LLM.Mock) is not disturbed.

  setup do
    on_exit(fn -> Agent.update(AVSA.LLM.Mock, fn _ -> nil end) end)
    :ok
  end

  # --- helper: start a test-owned AttributeTool backed by the capturing LLM ---
  defp start_capturing_tool do
    # The capturing double is not part of the app supervision tree; start it here.
    start_supervised!(AVSA.LLM.Capturing)
    AVSA.LLM.Capturing.reset()
    name = :"attr_tool_#{:erlang.unique_integer([:positive])}"

    pid =
      start_supervised!(
        {AVSA.AttributeTool, [llm_module: AVSA.LLM.Capturing, name: name]},
        id: name
      )

    {pid, name}
  end

  describe "call/2 (existing behaviour — fallback when no ViT attrs)" do
    test "call/2 fires telemetry event [:avsa, :orch, :tool, :attribute]" do
      AVSA.LLM.Mock.set_response(
        {:ok,
         %AVSA.LLM.ToolUse{
           name: "extract_attributes",
           id: "mock-ea-2",
           input: %{
             "category" => "dress",
             "colour" => "red",
             "formality" => "casual",
             "occasion" => "everyday"
           }
         }}
      )

      test_pid = self()
      ref = make_ref()

      :telemetry.attach(
        "test-attribute-tool-#{inspect(ref)}",
        [:avsa, :orch, :tool, :attribute, :stop],
        fn _event, _measurements, _metadata, _config ->
          send(test_pid, {:telemetry_fired, ref})
        end,
        nil
      )

      AVSA.AttributeTool.call("a red dress", "red dress")

      assert_receive {:telemetry_fired, ^ref}, 1000

      :telemetry.detach("test-attribute-tool-#{inspect(ref)}")
    end
  end

  describe "call/3 composition (ViT category/colour + LLM formality/occasion)" do
    test "category/colour come from ViT input; formality/occasion from the LLM" do
      {_pid, name} = start_capturing_tool()

      # The LLM (capturing double) returns ONLY text-derived attrs.
      AVSA.LLM.Capturing.set_response(
        {:ok,
         %AVSA.LLM.ToolUse{
           name: "extract_attributes",
           id: "cap-ea-1",
           input: %{
             "formality" => "formal",
             "occasion" => "wedding"
           }
         }}
      )

      vit_attributes = %{
        "category" => "blazer",
        "colour" => "navy",
        "category_confidence" => 0.95,
        "colour_confidence" => 0.88
      }

      assert {:ok, attrs} =
               GenServer.call(name, {:call, "a navy blazer", "for a wedding", vit_attributes})

      # category/colour SOURCED FROM ViT, not the LLM
      assert attrs["category"] == "blazer"
      assert attrs["colour"] == "navy"
      # formality/occasion SOURCED FROM the LLM
      assert attrs["formality"] == "formal"
      assert attrs["occasion"] == "wedding"
    end

    test "the LLM is asked ONLY for text-derived attrs (no category/colour in the tool schema)" do
      {_pid, name} = start_capturing_tool()

      AVSA.LLM.Capturing.set_response(
        {:ok,
         %AVSA.LLM.ToolUse{
           name: "extract_attributes",
           id: "cap-ea-2",
           input: %{"formality" => "formal", "occasion" => "wedding"}
         }}
      )

      vit_attributes = %{"category" => "blazer", "colour" => "navy"}

      assert {:ok, _attrs} =
               GenServer.call(name, {:call, "a navy blazer", "for a wedding", vit_attributes})

      # Inspect what the LLM was actually asked to do.
      assert [{_messages, tool_manifest} | _] = AVSA.LLM.Capturing.calls()

      properties = get_in(tool_manifest, ["input_schema", "properties"]) || %{}

      # The crux: the extract_attributes tool the LLM receives is scoped to the
      # text-derived attrs. The LLM must NOT be the source of category/colour, so
      # those keys must not appear as requested output fields in the tool schema.
      property_keys = Map.keys(properties)
      refute "category" in property_keys
      refute "colour" in property_keys
    end

    test "ViT category/colour survive even when the LLM omits or contradicts them" do
      {_pid, name} = start_capturing_tool()

      # The LLM contradicts the ViT head (and omits colour entirely).
      AVSA.LLM.Capturing.set_response(
        {:ok,
         %AVSA.LLM.ToolUse{
           name: "extract_attributes",
           id: "cap-ea-3",
           input: %{
             "category" => "WRONG-from-llm",
             "formality" => "casual",
             "occasion" => "everyday"
           }
         }}
      )

      vit_attributes = %{"category" => "skirt", "colour" => "green"}

      assert {:ok, attrs} =
               GenServer.call(name, {:call, "a green skirt", "casual", vit_attributes})

      # ViT wins for the image-derived attrs.
      assert attrs["category"] == "skirt"
      assert attrs["colour"] == "green"
    end

    test "does not add an LLM call: extraction invokes the LLM at most once" do
      {_pid, name} = start_capturing_tool()

      AVSA.LLM.Capturing.set_response(
        {:ok,
         %AVSA.LLM.ToolUse{
           name: "extract_attributes",
           id: "cap-ea-4",
           input: %{"formality" => "formal", "occasion" => "wedding"}
         }}
      )

      vit_attributes = %{"category" => "blazer", "colour" => "navy"}

      assert {:ok, _attrs} =
               GenServer.call(name, {:call, "a navy blazer", "for a wedding", vit_attributes})

      # The per-turn LLM work is narrower, not doubled: exactly one LLM call for
      # the attribute extraction (down from one that covered all four attrs).
      assert length(AVSA.LLM.Capturing.calls()) == 1
    end
  end

  # ---------------------------------------------------------------------------
  # Mixed image+text query: an explicit text colour (and category) OVERRIDES the
  # ViT-image-derived attribute. The text "delta" wins over the ViT head.
  # Image-only turns (no explicit text colour) are unchanged.
  #
  # These are hermetic: the capturing/Mock LLM supplies formality/occasion; the
  # ViT attrs are a plain map; the override is pure text parsing in AttributeTool.
  # ---------------------------------------------------------------------------
  describe "call/3 text override ( — \"this but green\")" do
    test "explicit text colour 'green' overrides ViT colour 'red'" do
      {_pid, name} = start_capturing_tool()

      AVSA.LLM.Capturing.set_response(
        {:ok,
         %AVSA.LLM.ToolUse{
           name: "extract_attributes",
           id: "cap-083-1",
           input: %{"formality" => "casual", "occasion" => "everyday"}
         }}
      )

      # ViT head saw a RED dress; the shopper typed "this but green".
      vit_attributes = %{"category" => "dress", "colour" => "red"}

      assert {:ok, attrs} =
               GenServer.call(name, {:call, "a red dress", "this but green", vit_attributes})

      # Text wins for colour.
      assert attrs["colour"] == "green",
             "text 'green' must override ViT 'red'; got #{inspect(attrs["colour"])}"

      # Category is not named in the text, so the ViT category survives.
      assert attrs["category"] == "dress"
    end

    test "explicit text category overrides ViT category (text wins)" do
      {_pid, name} = start_capturing_tool()

      AVSA.LLM.Capturing.set_response(
        {:ok,
         %AVSA.LLM.ToolUse{
           name: "extract_attributes",
           id: "cap-083-2",
           input: %{"formality" => "casual", "occasion" => "everyday"}
         }}
      )

      # ViT saw a dress; the shopper asks for a skirt instead.
      vit_attributes = %{"category" => "dress", "colour" => "red"}

      assert {:ok, attrs} =
               GenServer.call(name, {:call, "a red dress", "but as a skirt", vit_attributes})

      assert attrs["category"] == "skirt",
             "text 'skirt' must override ViT 'dress'; got #{inspect(attrs["category"])}"
    end

    test "image-only turn (no explicit text colour) keeps the ViT colour — no regression" do
      {_pid, name} = start_capturing_tool()

      AVSA.LLM.Capturing.set_response(
        {:ok,
         %AVSA.LLM.ToolUse{
           name: "extract_attributes",
           id: "cap-083-3",
           input: %{"formality" => "casual", "occasion" => "everyday"}
         }}
      )

      vit_attributes = %{"category" => "dress", "colour" => "red"}

      # Text carries no colour-vocab word, so the ViT colour must survive.
      assert {:ok, attrs} =
               GenServer.call(name, {:call, "a red dress", "something for the office", vit_attributes})

      assert attrs["colour"] == "red",
             "with no explicit text colour the ViT colour must be preserved; " <>
               "got #{inspect(attrs["colour"])}"
      assert attrs["category"] == "dress"
    end

    test "text-override colour-match is case-insensitive and normalises to the catalog vocab" do
      {_pid, name} = start_capturing_tool()

      AVSA.LLM.Capturing.set_response(
        {:ok,
         %AVSA.LLM.ToolUse{
           name: "extract_attributes",
           id: "cap-083-4",
           input: %{"formality" => "casual", "occasion" => "everyday"}
         }}
      )

      vit_attributes = %{"category" => "dress", "colour" => "red"}

      assert {:ok, attrs} =
               GenServer.call(name, {:call, "a red dress", "this but GREEN please", vit_attributes})

      # Normalised to the lowercase catalog vocabulary form so it matches the
      # crude `colour` column directly.
      assert attrs["colour"] == "green"
    end
  end

  # ---------------------------------------------------------------------------
  # Observability — avsa_attribute_source_total{attribute, source} and
  # avsa_attribute_llm_calls_total{narrowed}.
  #
  # REAL test: a real telemetry handler is attached around a REAL
  # AVSA.AttributeTool call. The SUT is compose_attrs/2 + the tool-schema
  # selection; the LLM is the AVSA.LLM.Capturing boundary double (acceptable —
  # it is the dependency, not the code under test). We assert the source-split
  # and narrowed events fire with the right labels.
  # ---------------------------------------------------------------------------

  describe "attribute source-split + llm-call observability (real telemetry handler)" do
    defp attach_attr_metric_handler do
      test_pid = self()
      handler_id = "attr-metrics-#{:erlang.unique_integer([:positive])}"

      :telemetry.attach_many(
        handler_id,
        [
          [:avsa, :attribute, :source],
          [:avsa, :attribute, :llm_call]
        ],
        fn event, measurements, metadata, _config ->
          send(test_pid, {:attr_metric, event, measurements, metadata})
        end,
        nil
      )

      on_exit(fn -> :telemetry.detach(handler_id) end)
      :ok
    end

    defp collect_attr_metrics(timeout), do: collect_attr_metrics(timeout, [])

    defp collect_attr_metrics(timeout, acc) do
      receive do
        {:attr_metric, event, measurements, metadata} ->
          collect_attr_metrics(timeout, [{event, measurements, metadata} | acc])
      after
        timeout -> Enum.reverse(acc)
      end
    end

    test "call/3 with ViT attrs emits source=vit for category/colour, source=llm for formality/occasion, narrowed=true" do
      {_pid, name} = start_capturing_tool()
      attach_attr_metric_handler()

      AVSA.LLM.Capturing.set_response(
        {:ok,
         %AVSA.LLM.ToolUse{
           name: "extract_attributes",
           id: "cap-obs-1",
           input: %{"formality" => "formal", "occasion" => "wedding"}
         }}
      )

      vit_attributes = %{
        "category" => "blazer",
        "colour" => "navy",
        "category_confidence" => 0.95,
        "colour_confidence" => 0.88
      }

      assert {:ok, _attrs} =
               GenServer.call(name, {:call, "a navy blazer", "for a wedding", vit_attributes})

      events = collect_attr_metrics(300)

      source_pairs =
        for {[:avsa, :attribute, :source], _m, %{attribute: a, source: s}} <- events, do: {a, s}

      assert {"category", "vit"} in source_pairs
      assert {"colour", "vit"} in source_pairs
      assert {"formality", "llm"} in source_pairs
      assert {"occasion", "llm"} in source_pairs

      # No category/colour should be sourced from the LLM in the offload path.
      refute {"category", "llm"} in source_pairs
      refute {"colour", "llm"} in source_pairs

      llm_calls =
        for {[:avsa, :attribute, :llm_call], _m, meta} <- events, do: meta.narrowed

      assert llm_calls == [true]
    end

    test "call/3 with nil vit_attributes emits source=llm for all four attrs and narrowed=false" do
      {_pid, name} = start_capturing_tool()
      attach_attr_metric_handler()

      AVSA.LLM.Capturing.set_response(
        {:ok,
         %AVSA.LLM.ToolUse{
           name: "extract_attributes",
           id: "cap-obs-2",
           input: %{
             "category" => "dress",
             "colour" => "red",
             "formality" => "casual",
             "occasion" => "everyday"
           }
         }}
      )

      assert {:ok, _attrs} = GenServer.call(name, {:call, "a red dress", "red dress", nil})

      events = collect_attr_metrics(300)

      source_pairs =
        for {[:avsa, :attribute, :source], _m, %{attribute: a, source: s}} <- events, do: {a, s}

      assert {"category", "llm"} in source_pairs
      assert {"colour", "llm"} in source_pairs
      assert {"formality", "llm"} in source_pairs
      assert {"occasion", "llm"} in source_pairs

      # No ViT source in the fallback path.
      refute Enum.any?(source_pairs, fn {_a, s} -> s == "vit" end)

      llm_calls =
        for {[:avsa, :attribute, :llm_call], _m, meta} <- events, do: meta.narrowed

      assert llm_calls == [false]
    end

    test "call/2 (no ViT attrs) emits source=llm for all four and narrowed=false on the real singleton" do
      attach_attr_metric_handler()

      AVSA.LLM.Mock.set_response(
        {:ok,
         %AVSA.LLM.ToolUse{
           name: "extract_attributes",
           id: "mock-obs-1",
           input: %{
             "category" => "dress",
             "colour" => "red",
             "formality" => "casual",
             "occasion" => "everyday"
           }
         }}
      )

      assert {:ok, _attrs} = AVSA.AttributeTool.call("a red dress", "red dress")

      events = collect_attr_metrics(300)

      source_pairs =
        for {[:avsa, :attribute, :source], _m, %{attribute: a, source: s}} <- events, do: {a, s}

      assert {"category", "llm"} in source_pairs
      assert {"colour", "llm"} in source_pairs
      refute Enum.any?(source_pairs, fn {_a, s} -> s == "vit" end)

      llm_calls =
        for {[:avsa, :attribute, :llm_call], _m, meta} <- events, do: meta.narrowed

      assert llm_calls == [false]
    end
  end

  # ---------------------------------------------------------------------------
  # Regression tests — planning message wording in attribute_tool.ex handle_call
  # ---------------------------------------------------------------------------

  describe "regression: planning message wording (issue: old 'Image:' prefix caused text reply instead of tool call)" do
    @tag :regression
    test "AttributeTool LLM message does NOT start with 'Image:' and DOES contain user_text and extraction intent" do
      # This test drives AttributeTool.call/2 (fallback / no ViT path) with the
      # capturing LLM and asserts the message:
      #   (a) does NOT start with "Image:",
      #   (b) DOES contain the user_text, and
      #   (c) DOES contain "Extract the product attributes" (the extraction intent).
      #
      # It will FAIL if the handle_call message reverts to an "Image: ..."
      # prefix that caused Claude to reply with a text description rather than
      # invoking the extract_attributes tool.

      {_pid, name} = start_capturing_tool()

      AVSA.LLM.Capturing.set_response(
        {:ok,
         %AVSA.LLM.ToolUse{
           name: "extract_attributes",
           id: "cap-reg-1",
           input: %{
             "category" => "blouse",
             "colour" => "white",
             "formality" => "casual",
             "occasion" => "everyday"
           }
         }}
      )

      user_text = "a crisp white blouse"
      image_description = "white blouse with button-down collar"

      assert {:ok, _attrs} =
               GenServer.call(name, {:call, image_description, user_text, nil})

      assert [{messages, _manifest} | _] = AVSA.LLM.Capturing.calls()

      # Extract the "user" role message content.
      user_contents =
        for %{"role" => "user", "content" => content} <- messages, do: content

      assert user_contents != [],
             "Expected at least one user-role message; got: #{inspect(messages)}"

      Enum.each(user_contents, fn content ->
        # (a) Must NOT start with "Image:".
        refute String.starts_with?(content, "Image:"),
               "Message must not start with 'Image:' (old wording). Got: #{inspect(content)}"

        # Tighter guard: the word "Image:" must not appear as the leading token
        # even with whitespace variations.
        refute content =~ ~r/^Image:/,
               "Message must not match ^Image: regex. Got: #{inspect(content)}"
      end)

      # (b) At least one user message must contain the user_text.
      assert Enum.any?(user_contents, &String.contains?(&1, user_text)),
             "Expected user_text '#{user_text}' in message content. Got: #{inspect(user_contents)}"

      # (c) At least one user message must carry the extraction intent phrase.
      assert Enum.any?(user_contents, &String.contains?(&1, "Extract the product attributes")),
             "Expected 'Extract the product attributes' in message content. Got: #{inspect(user_contents)}"
    end

    @tag :regression
    test "AttributeTool LLM message with ViT attrs does NOT start with 'Image:' and DOES contain user_text" do
      # Same regression guard for the ViT-narrowed call/3 path: even when ViT
      # attrs are present the message wording must follow the required template.

      {_pid, name} = start_capturing_tool()

      AVSA.LLM.Capturing.set_response(
        {:ok,
         %AVSA.LLM.ToolUse{
           name: "extract_attributes",
           id: "cap-reg-2",
           input: %{"formality" => "smart-casual", "occasion" => "work"}
         }}
      )

      user_text = "tailored navy trousers"
      image_description = "dark navy straight-leg trousers"
      vit_attributes = %{"category" => "trousers", "colour" => "navy"}

      assert {:ok, _attrs} =
               GenServer.call(name, {:call, image_description, user_text, vit_attributes})

      assert [{messages, _manifest} | _] = AVSA.LLM.Capturing.calls()

      user_contents =
        for %{"role" => "user", "content" => content} <- messages, do: content

      assert user_contents != [],
             "Expected at least one user-role message; got: #{inspect(messages)}"

      Enum.each(user_contents, fn content ->
        refute String.starts_with?(content, "Image:"),
               "Message must not start with 'Image:' (old wording). Got: #{inspect(content)}"
      end)

      assert Enum.any?(user_contents, &String.contains?(&1, user_text)),
             "Expected user_text '#{user_text}' in message content. Got: #{inspect(user_contents)}"
    end
  end

  describe "output keys unchanged (downstream contract stable)" do
    test "call/3 with ViT attrs returns exactly category, colour, formality, occasion" do
      {_pid, name} = start_capturing_tool()

      AVSA.LLM.Capturing.set_response(
        {:ok,
         %AVSA.LLM.ToolUse{
           name: "extract_attributes",
           id: "cap-out-1",
           input: %{"formality" => "formal", "occasion" => "wedding"}
         }}
      )

      vit_attributes = %{
        "category" => "blazer",
        "colour" => "navy",
        "category_confidence" => 0.95,
        "colour_confidence" => 0.88
      }

      assert {:ok, attrs} =
               GenServer.call(name, {:call, "a navy blazer", "for a wedding", vit_attributes})

      # Exactly the four schema keys — confidences and any extra ViT fields are NOT
      # leaked into the downstream attrs map (retrieval/verifier contract stable).
      assert Enum.sort(Map.keys(attrs)) == ["category", "colour", "formality", "occasion"]
    end
  end
end
