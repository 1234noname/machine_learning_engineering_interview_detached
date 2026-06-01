defmodule AVSA.AttributeTool do
  @moduledoc """
  GenServer that extracts structured product attributes from an image description
  and user text by calling the LLM with the extract_attributes tool.
  """

  use GenServer

  require Logger

  @text_attr_keys ["formality", "occasion"]
  @image_attr_keys ["category", "colour"]
  @all_attr_keys @image_attr_keys ++ @text_attr_keys

  @colour_vocab ~w(black white navy red green beige grey gray blue pink brown
                   yellow purple orange gold silver)

  @category_vocab ~w(dress skirt blouse shirt top trousers pants jeans jacket
                     blazer coat sweater cardigan shorts suit gown)

  @call_timeout_ms 120_000

  # Client API

  def start_link(opts \\ []) do
    llm_module =
      Keyword.get(opts, :llm_module, Application.get_env(:avsa, :llm_module, AVSA.LLM.Anthropic))

    name = Keyword.get(opts, :name, __MODULE__)

    GenServer.start_link(__MODULE__, %{llm_module: llm_module}, name: name)
  end

  @doc """
  Extract attributes from `image_description` and `user_text`.

  Fallback path: the LLM extracts all four attributes (text turn / MCP / stub).
  """
  def call(image_description, user_text) do
    GenServer.call(
      __MODULE__,
      {:call, image_description, user_text, nil},
      @call_timeout_ms
    )
  end

  @doc """
  Extract attributes, composing ViT-derived `category`/`colour` with
  LLM-derived `formality`/`occasion`.

  `vit_attributes` is a string-keyed map carrying at least `"category"` and
  `"colour"` (the ViT attribute head output surfaced by `AVSA.EmbedStep`), or
  `nil`. When present, `category`/`colour` are taken from `vit_attributes` and
  the LLM is asked ONLY for the text-derived attrs (`formality`/`occasion`) via
  a narrowed `extract_attributes` tool — the ViT values survive even if the LLM
  omits or contradicts them. When `nil`, behaviour is identical to `call/2`
  (the LLM extracts all four attrs). The LLM is invoked at most once.

  Accepts an optional server name as the first argument is NOT supported here;
  the singleton is targeted. Tests start an isolated instance and call via
  `GenServer.call(name, {:call, image_description, user_text, vit_attributes})`.
  """
  def call(image_description, user_text, vit_attributes) do
    GenServer.call(
      __MODULE__,
      {:call, image_description, user_text, vit_attributes},
      @call_timeout_ms
    )
  end

  # Server callbacks

  @impl GenServer
  def init(state) do
    {:ok, state}
  end

  @impl GenServer
  def handle_call({:call, image_description, user_text, vit_attributes}, _from, state) do
    result =
      :telemetry.span(
        [:avsa, :orch, :tool, :attribute],
        %{},
        fn ->
          messages = [
            %{
              "role" => "user",
              "content" =>
                "Product search query: #{user_text}. Description hint: #{image_description}. " <>
                  "Extract the product attributes."
            }
          ]

          tool = extract_attributes_tool(vit_attributes)

          :telemetry.execute(
            [:avsa, :attribute, :llm_call],
            %{count: 1},
            %{narrowed: is_map(vit_attributes)}
          )

          outcome =
            case state.llm_module.call(messages, tool) do
              {:ok, %AVSA.LLM.ToolUse{} = tool_use} ->
                llm_attrs = extract_attrs(tool_use)

                composed = compose_attrs(llm_attrs, vit_attributes)

                {:ok, apply_text_overrides(composed, user_text)}

              {:error, reason} ->
                {:error, reason}
            end

          {outcome, %{}}
        end
      )

    {:reply, result, state}
  end

  defp compose_attrs(llm_attrs, vit_attributes) when is_map(vit_attributes) do
    base =
      Map.new(@text_attr_keys, fn key -> {key, Map.get(llm_attrs, key)} end)

    image =
      Map.new(@image_attr_keys, fn key -> {key, Map.get(vit_attributes, key)} end)

    Enum.each(@image_attr_keys, &emit_source(&1, "vit"))
    Enum.each(@text_attr_keys, &emit_source(&1, "llm"))

    Map.merge(base, image)
  end

  defp compose_attrs(llm_attrs, _vit_attributes) do
    Enum.each(@all_attr_keys, &emit_source(&1, "llm"))

    Map.new(@all_attr_keys, fn key -> {key, Map.get(llm_attrs, key)} end)
  end

  defp apply_text_overrides(attrs, user_text) when is_binary(user_text) do
    words = tokenise(user_text)

    attrs
    |> maybe_override("colour", first_match(words, @colour_vocab))
    |> maybe_override("category", first_match(words, @category_vocab))
  end

  defp apply_text_overrides(attrs, _user_text), do: attrs

  defp maybe_override(attrs, _key, nil), do: attrs

  defp maybe_override(attrs, key, value) do
    emit_source(key, "text")
    Map.put(attrs, key, value)
  end

  defp tokenise(text) do
    text
    |> String.downcase()
    |> String.split(~r/[^a-z0-9]+/, trim: true)
  end

  defp first_match(words, vocab) do
    word_set = MapSet.new(words)
    Enum.find(vocab, fn term -> MapSet.member?(word_set, term) end)
  end

  defp emit_source(attribute, source) do
    :telemetry.execute(
      [:avsa, :attribute, :source],
      %{count: 1},
      %{attribute: attribute, source: source}
    )
  end

  defp extract_attrs(%AVSA.LLM.ToolUse{name: "extract_attributes", input: input}), do: input

  defp extract_attrs(%AVSA.LLM.ToolUse{name: "find_similar", input: %{"attrs" => attrs}}),
    do: attrs

  defp extract_attrs(%AVSA.LLM.ToolUse{input: input}) when is_map(input), do: input

  defp extract_attributes_tool(vit_attributes) when is_map(vit_attributes) do
    %{
      "name" => "extract_attributes",
      "description" =>
        "Extract the text-derived attributes (formality, occasion) from the " <>
          "user's text. Do NOT infer category or colour — those are supplied " <>
          "by the image attribute head.",
      "input_schema" => %{
        "type" => "object",
        "properties" => %{
          "formality" => %{"type" => "string"},
          "occasion" => %{"type" => "string"}
        },
        "required" => ["formality", "occasion"]
      }
    }
  end

  defp extract_attributes_tool(_vit_attributes) do
    %{
      "name" => "extract_attributes",
      "description" => "Extract structured attributes from image description and user text",
      "input_schema" => %{
        "type" => "object",
        "properties" => %{
          "category" => %{"type" => "string"},
          "colour" => %{"type" => "string"},
          "formality" => %{"type" => "string"},
          "occasion" => %{"type" => "string"}
        },
        "required" => ["category", "colour", "formality", "occasion"]
      }
    }
  end
end
