defmodule AVSA.MCP.Tools do
  @moduledoc """
  Image-native MCP tool implementations — the L2 tool layer that is
  driven identically by the internal orchestrator (in-process) and by external
  MCP clients (over HTTP via `AVSA.MCP.Server`).

  ## Image-native, not vector-native

  Both tools take an **image** — `"image_b64"` (inline base64 bytes) or
  `"image_ref"` (a storage key) — resolved to bytes by `AVSA.MCP.ImageResolver`.
  Each tool then **embeds internally** by reaching L1 (the ViT `/embed` forward
  via `AVSA.EmbedStep`) **through the per-request embed cache**
  (`AVSA.EmbedCache`). An external MCP client cannot produce a 768-d AVSA-ViT
  vector, so a tool that took a pre-computed embedding would not be
  agent-callable. There is no `embedding` argument anywhere; the model is
  reached as L1 infrastructure *through* the tools.

  ## Modality

  `find_similar/2` is modality-aware:

    * with an image (`image_b64`/`image_ref`) → embed to a 768-d ViT vector,
      run image kNN (`AVSA.RetrievalTool.call/2`).
    * with `"text"` (no image) → embed to a 512-d CLIP text vector
      (`AVSA.TextTool`), run text kNN (`AVSA.RetrievalTool.call_text/2`).

  The attribute-constraint logic is preserved: the `"attrs"` map (incl. an
  explicit `"colour"`) flows straight into the retrieval call.

  ## The one-forward-per-turn invariant

  `find_similar` and `extract_attributes` are typically both called on the same
  image in one turn. Both go through `AVSA.EmbedCache.with_embed/5` keyed by the
  turn's `request_id` + content hash, so the ViT forward runs exactly once. The
  cache lives at L1, below the MCP boundary — it never leaks across MCP.
  """

  require Logger

  @type tool_args :: %{optional(String.t()) => term()}
  @type opts :: keyword()

  # ── find_similar ──────────────────────────────────────────────────────────────

  @doc """
  Image-native (or text-native) similarity retrieval.

  Arguments (`args`):

    * `"image_b64"` / `"image_ref"` — the image to find similar products to
      (image modality), OR
    * `"text"` — a free-text query (text modality), used when no image is given.
    * `"attrs"` — optional attribute filters (`category`, `colour`, ...). The
      explicit `colour` constraint is honoured by the retrieval tool.

  `opts` must carry `:request_id` (the turn token) so the embed is cached for the
  turn. Returns `{:ok, %{results: [...]}}` (display-shaped product cards) or
  `{:error, {:invalid_argument, msg}}` / `{:error, reason}`.
  """
  @spec find_similar(tool_args(), opts()) ::
          {:ok, %{results: [map()]}} | {:error, {:invalid_argument, String.t()}} | {:error, term()}
  def find_similar(args, opts) when is_map(args) do
    case find_similar_results(args, opts) do
      {:ok, results} -> {:ok, %{results: Enum.map(results, &to_card/1)}}
      {:error, reason} -> {:error, reason}
    end
  end

  @doc """
  The in-process variant of `find_similar/2` for the internal orchestrator.

  Identical retrieval path (image/text modality → embed-inside-tool through the
  per-turn cache → kNN), but returns the raw `AVSA.ProductResult` structs rather
  than display cards. The internal chat flow (`AVSA.Conversation`) needs structs
  so the Verifier (`check_factuality` pattern-matches `%AVSA.ProductResult{}`) and
  `AVSA.GrpcServer.build_product_event/1` (`Ecto.UUID.cast!/1` on the binary id)
  receive an un-lossy result — `to_card/1` stringifies the binary uuid for the
  external wire and is not round-trippable. `find_similar/2` wraps this with
  `to_card/1` for external MCP clients. One tool layer, two output shapes.
  """
  @spec find_similar_results(tool_args(), opts()) ::
          {:ok, [AVSA.ProductResult.t()]}
          | {:error, {:invalid_argument, String.t()}}
          | {:error, term()}
  def find_similar_results(args, opts) when is_map(args) do
    request_id = request_id(opts)
    :telemetry.execute([:avsa, :tool_dispatch, :find_similar], %{count: 1}, %{})

    attrs = normalise_attrs(Map.get(args, "attrs", %{}))

    case modality(args) do
      :image ->
        with {:ok, image_bytes_list} <- AVSA.MCP.ImageResolver.resolve_all(args),
             {:ok, embedding} <- embed_and_pool(image_bytes_list, request_id),
             {:ok, results} <- retrieval_tool_module().call(embedding, attrs) do
          {:ok, results}
        else
          {:error, :no_image} -> invalid_arg_no_input()
          {:error, reason} -> {:error, reason}
        end

      :text ->
        text = Map.get(args, "text")

        with {:ok, text_embedding} <- text_tool_module().call(text),
             {:ok, results} <- retrieval_tool_module().call_text(text_embedding, attrs) do
          {:ok, results}
        else
          {:error, reason} -> {:error, reason}
        end

      :none ->
        invalid_arg_no_input()
    end
  end

  # ── extract_attributes ──────────────────────────────────────────────────────

  @doc """
  Image-native attribute extraction.

  Arguments (`args`):

    * `"image_b64"` / `"image_ref"` — the image to extract attributes from.
      The image is embedded (through the same per-turn cache as `find_similar`)
      so the ViT attribute-head output (category/colour) is sourced from L1 and
      the LLM is asked only for the text-derived attrs.
    * `"user_text"` — the accompanying chat text (may be empty).

  `opts` must carry `:request_id`. Returns `{:ok, %{attrs: map()}}` (the four-key
  attribute map) or `{:error, reason}`. When no image is supplied (text-only),
  the LLM extracts all four attributes.
  """
  @spec extract_attributes(tool_args(), opts()) ::
          {:ok, %{attrs: map()}} | {:error, term()}
  def extract_attributes(args, opts) when is_map(args) do
    request_id = request_id(opts)
    :telemetry.execute([:avsa, :tool_dispatch, :extract_attributes], %{count: 1}, %{})

    user_text = Map.get(args, "user_text", "")
    image_description = Map.get(args, "image_description", user_text)

    vit_attributes =
      case AVSA.MCP.ImageResolver.resolve(args) do
        {:ok, image_bytes} ->
          case embed_image(image_bytes, request_id) do
            {:ok, %{attributes: attributes}} -> attributes
            {:error, _} -> nil
          end

        {:error, _} ->
          nil
      end

    case attribute_tool_module().call(image_description, user_text, vit_attributes) do
      {:ok, attrs} -> {:ok, %{attrs: attrs}}
      {:error, reason} -> {:error, reason}
    end
  end

  # ── helpers ───────────────────────────────────────────────────────────────────

  @spec embed_image(binary(), String.t()) :: {:ok, map()} | {:error, term()}
  defp embed_image(image_bytes, request_id) do
    AVSA.EmbedCache.with_embed(
      AVSA.EmbedCache,
      request_id,
      image_bytes,
      fn -> embed_step_module().call(image_bytes) end,
      :image
    )
  end

  @spec embed_and_pool([binary()], String.t()) :: {:ok, [float()]} | {:error, term()}
  defp embed_and_pool([image_bytes], request_id) do
    case embed_image(image_bytes, request_id) do
      {:ok, %{embedding: embedding}} -> {:ok, embedding}
      {:error, reason} -> {:error, reason}
    end
  end

  defp embed_and_pool(images, request_id) when is_list(images) and images != [] do
    pooled =
      Enum.reduce_while(images, {:ok, []}, fn img, {:ok, acc} ->
        case embed_image(img, request_id) do
          {:ok, %{embedding: embedding}} -> {:cont, {:ok, [embedding | acc]}}
          {:error, reason} -> {:halt, {:error, reason}}
        end
      end)

    case pooled do
      {:ok, embeddings} -> {:ok, mean_pool(Enum.reverse(embeddings))}
      {:error, reason} -> {:error, reason}
    end
  end

  @spec mean_pool([[float()]]) :: [float()]
  defp mean_pool([single]), do: single

  defp mean_pool([first | _] = vectors) do
    n = length(vectors)
    zero = List.duplicate(0.0, length(first))

    vectors
    |> Enum.reduce(zero, fn vec, acc -> Enum.zip_with(acc, vec, &(&1 + &2)) end)
    |> Enum.map(&(&1 / n))
    |> l2_normalise()
  end

  @spec l2_normalise([float()]) :: [float()]
  defp l2_normalise(vec) do
    norm = :math.sqrt(Enum.reduce(vec, 0.0, fn x, acc -> acc + x * x end))
    if norm > 0.0, do: Enum.map(vec, &(&1 / norm)), else: vec
  end

  @spec modality(tool_args()) :: :image | :text | :none
  defp modality(args) do
    image_list = Map.get(args, "image_b64_list")

    cond do
      is_list(image_list) and image_list != [] -> :image
      is_binary(Map.get(args, "image_b64")) -> :image
      is_binary(Map.get(args, "image_ref")) -> :image
      is_binary(Map.get(args, "text")) and Map.get(args, "text") != "" -> :text
      true -> :none
    end
  end

  @spec invalid_arg_no_input() :: {:error, {:invalid_argument, String.t()}}
  defp invalid_arg_no_input do
    {:error,
     {:invalid_argument,
      "find_similar is image-native: supply 'image_b64' or 'image_ref' (image modality) " <>
        "or 'text' (text modality). A pre-computed 'embedding' is not accepted."}}
  end

 @spec to_card(AVSA.ProductResult.t()) :: map()
  defp to_card(%AVSA.ProductResult{} = pr) do
    %{
      # pr.id is a raw 16-byte binary UUID from Postgrex; `to_string/1` on it
      # yields invalid UTF-8 that crashes Jason on the external HTTP wire
      # (Jason.EncodeError). Encode to the canonical 36-char UUID string, as
      # AVSA.GrpcServer.build_product_event/1 does.
      "result_id" => Ecto.UUID.cast!(pr.id),
      "score" => pr.score || 0.0,
      "title" => pr.title,
      "category" => pr.category,
      "price_cents" => pr.price_cents,
      "image_url" => pr.image_url
    }
  end

  @spec normalise_attrs(term()) :: map()
  defp normalise_attrs(attrs) when is_map(attrs), do: attrs
  defp normalise_attrs(_), do: %{}

  @spec request_id(opts()) :: String.t()
  defp request_id(opts) do
    case Keyword.get(opts, :request_id) do
      id when is_binary(id) and id != "" -> id
      _ -> Ecto.UUID.generate()
    end
  end

  @spec embed_step_module() :: module()
  defp embed_step_module, do: Application.get_env(:avsa, :embed_step_module, AVSA.EmbedStep)

  @spec text_tool_module() :: module()
  defp text_tool_module, do: Application.get_env(:avsa, :text_tool_module, AVSA.TextTool)

  @spec retrieval_tool_module() :: module()
  defp retrieval_tool_module,
    do: Application.get_env(:avsa, :retrieval_tool_module, AVSA.RetrievalTool)

  @spec attribute_tool_module() :: module()
  defp attribute_tool_module,
    do: Application.get_env(:avsa, :attribute_tool_module, AVSA.AttributeTool)
end
